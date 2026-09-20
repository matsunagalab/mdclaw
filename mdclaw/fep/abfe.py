"""Absolute binding free energy of one ligand (double decoupling).

One job holds both legs, as for a mutation ddG::

    prep (complex) -> solv -> topo [build_decoupled_system] -> min -> eq
                                  -> topo [add_boresch_restraint] -> fep ... -> analyze_fep        (complex leg)
      `-> prep [extract_ligand] -> solv -> topo [build_decoupled_system] -> min -> eq
                                  -> fep ... -> analyze_fep                                          (solvent leg)
    analyze [estimate_binding_dg] over the two analyze_fep nodes

Both legs switch the ligand off (charges first, then soft-core sterics; see
:mod:`mdclaw.fep.decouple`). The complex leg first switches a Boresch
restraint on, chosen from the equilibrated complex
(:mod:`mdclaw.fep.boresch`); its release at the 1 M standard state is
analytic::

    dG_bind = dG(solvent leg) - dG(complex leg) + dG(standard state -> restraint)

The complex topology from ``build_decoupled_system`` carries no lambda
protocol on purpose: ``run_fep`` cannot be pointed at an unrestrained complex.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from mdclaw._common import create_unique_subdir
from mdclaw._tool_meta import node_tool
from mdclaw.fep.boresch import (
    DEFAULT_K_ANGLE,
    DEFAULT_K_DISTANCE,
    RESTRAINT_PARAMETER,
    BoreschError,
    BoreschRestraint,
    bonded_pairs,
    boresch_force,
    select_boresch_restraint,
    standard_state_restraint_free_energy,
)
from mdclaw.fep.decouple import COUPLED, decouple_ligand, validate_decoupling
from mdclaw.fep.hybrid import DEFAULT_SOFTCORE_ALPHA, FEP_PARAMETERS, HybridBuildError
from mdclaw.fep.protocol import PROTOCOL_SCHEMA_VERSION, ProtocolError

logger = logging.getLogger(__name__)

WORKING_DIR = Path("outputs").resolve()
ABFE_KIND = "abfe_decouple"
LEG_COMPLEX, LEG_SOLVENT = "complex", "solvent"
BINDING_ANALYSIS = "abfe_binding"
_KJ_PER_KCAL = 4.184
_KB = 0.008314462618

DEFAULT_ELEC_LAMBDAS = (1.0, 0.75, 0.5, 0.25, 0.0)
DEFAULT_STERICS_LAMBDAS = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.25, 0.2, 0.15, 0.1, 0.05, 0.0)
DEFAULT_RESTRAINT_LAMBDAS = (0.0, 0.02, 0.08, 0.2, 0.5, 1.0)

_SOLVENT_RESIDUES = frozenset({"HOH", "WAT", "TIP3", "TIP", "SOL", "H2O", "OPC", "TP3", "SPC"})


class AbfeError(RuntimeError):
    def __init__(self, code: str, message: str, extra: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.extra = extra or {}


# --------------------------------------------------------------------------- #
# Lambda protocol                                                               #
# --------------------------------------------------------------------------- #

def _parse_lambdas(value: Any, default: tuple, name: str, *, start: float, end: float) -> list[float]:
    if value is None or value == "":
        return list(default)
    try:
        items = json.loads(value) if isinstance(value, str) and value.strip().startswith("[") else (
            [v for v in value.split(",") if v.strip()] if isinstance(value, str) else list(value))
        out = [float(v) for v in items]
    except (TypeError, ValueError) as exc:
        raise ProtocolError(code="fep_protocol_invalid", message=f"{name}: cannot read {value!r} as a list of numbers") from exc
    step = 1 if end > start else -1
    if len(out) < 2 or abs(out[0] - start) > 1e-9 or abs(out[-1] - end) > 1e-9:
        raise ProtocolError(code="fep_protocol_invalid", message=f"{name} must run from {start:g} to {end:g}, got {out}")
    if any((b - a) * step <= 0 for a, b in zip(out, out[1:])) or any(not 0.0 <= v <= 1.0 for v in out):
        raise ProtocolError(code="fep_protocol_invalid", message=f"{name} must be strictly monotonic within [0, 1], got {out}")
    return out


def abfe_windows(elec: list[float], sterics: list[float], restraint: Optional[list[float]] = None) -> tuple[list[dict], list[dict]]:
    """``(windows, phases)``: restrain (complex leg only), switch the charges
    off, then the soft-core sterics. Every window carries explicit values."""
    names = [*FEP_PARAMETERS, *([RESTRAINT_PARAMETER] if restraint else [])]
    states: list[dict] = []
    stages: list[tuple[str, int]] = []
    current = {**COUPLED, **({RESTRAINT_PARAMETER: restraint[0]} if restraint else {})}
    states.append(dict(current))
    for stage, parameter, values in (("restrain", RESTRAINT_PARAMETER, restraint), ("decharge", "fep_elec_old", elec),
                                     ("decouple_sterics", "fep_sterics_old", sterics)):
        if not values:
            continue
        for v in values[1:]:
            current[parameter] = float(v)
            states.append(dict(current))
        stages.append((stage, len(states) - 1))
    n = len(states)
    windows = [{"index": i, "lambda": round(i / (n - 1), 6), "parameters": {k: float(s[k]) for k in names}}
               for i, s in enumerate(states)]
    phases, lo = [], 0
    for stage, last in stages:
        phases.append({"name": stage, "lambda_lo": windows[lo]["lambda"], "lambda_hi": windows[last]["lambda"],
                       "first_window": lo, "last_window": last})
        lo = last
    return windows, phases


def _protocol(ligand: dict, leg: str, windows: list[dict], phases: list[dict], softcore_alpha: float, schedules: dict) -> dict:
    names = list(windows[0]["parameters"])
    return {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        # analyze_fep / estimate_* read the transformation's name from here.
        "mutation": {"label": f"decouple:{ligand['residue_name']}", "kind": ABFE_KIND},
        "transformation": {"kind": ABFE_KIND, "leg": leg, "ligand": ligand},
        "softcore_alpha": float(softcore_alpha),
        "global_parameters": names,
        "state_a": dict(windows[0]["parameters"]),
        "state_b": dict(windows[-1]["parameters"]),
        "schedules": schedules,
        "phases": phases,
        "n_windows": len(windows),
        "windows": windows,
    }


# --------------------------------------------------------------------------- #
# Ligand identity                                                               #
# --------------------------------------------------------------------------- #

def _describe(record: dict) -> str:
    chain = record.get("author_chain") or record.get("chain_id") or ""
    return f"{chain}:{record.get('residue_name')}:{record.get('resnum') or ''}"


def _refuse_charged(record: dict, charge: Optional[float]) -> None:
    if charge is not None and abs(float(charge)) > 1e-3:
        raise AbfeError(
            code="abfe_charged_ligand_unsupported",
            message=f"ligand {_describe(record)} carries {float(charge):+.2f} e. Decoupling a charged ligand changes "
            "the box charge, with a finite-size error that differs between the two legs; this is not handled yet. "
            "Report it; do not neutralise the ligand to get past this check")


def select_ligand_record(records: Optional[list], ligand: Optional[str]) -> dict:
    """The prepared ligand to decouple. ``ligand`` is ``RESNAME``,
    ``CHAIN:RESNAME`` or ``CHAIN:RESNAME:RESNUM``; it may be omitted when the
    preparation holds exactly one ligand."""
    records = [r for r in (records or []) if isinstance(r, dict) and r.get("residue_name")]
    if not records:
        raise AbfeError(code="abfe_ligand_not_found",
                        message="the prep ancestor prepared no ligand (no ligand_chemistry); absolute binding needs a "
                        "ligand kept by prepare_complex")
    if not ligand:
        if len(records) == 1:
            return records[0]
        raise AbfeError(code="abfe_ligand_ambiguous",
                        message=f"{len(records)} ligands were prepared: {', '.join(_describe(r) for r in records)}; "
                        "pass --ligand CHAIN:RESNAME:RESNUM",
                        extra={"ligand_candidates": [_describe(r) for r in records]})
    parts = [p.strip() for p in str(ligand).split(":")]
    chain, name, resnum = (None, parts[0], None) if len(parts) == 1 else (
        (parts[0] or None, parts[1], None) if len(parts) == 2 else (parts[0] or None, parts[1], parts[2] or None))
    matches = [r for r in records
               if str(r.get("residue_name")).upper() == name.upper()
               # chain_id is prepare_complex's internal component label; the
               # chain a user sees is author_chain.
               and (chain is None or chain in (str(r.get("chain_id") or ""), str(r.get("author_chain") or "")))
               and (resnum is None or str(r.get("resnum")) == resnum)]
    if len(matches) != 1:
        raise AbfeError(code="abfe_ligand_not_found" if not matches else "abfe_ligand_ambiguous",
                        message=f"--ligand {ligand!r} matched {len(matches)} prepared ligand(s); prepared: "
                        f"{', '.join(_describe(r) for r in records)}",
                        extra={"ligand_candidates": [_describe(r) for r in records]})
    return matches[0]


def _ligand_summary(record: dict) -> dict:
    return {k: record.get(k) for k in ("residue_name", "chain_id", "author_chain", "resnum", "ligand_id", "smiles",
                                       "net_charge")}


def ligand_atoms_in_topology(topology, ligand: dict) -> list[int]:
    """Atom indices of the one residue that is the ligand."""
    name = str(ligand["residue_name"]).upper()
    residues = [r for r in topology.residues() if r.name.upper() == name]
    resnum = ligand.get("resnum")
    if len(residues) > 1 and resnum is not None:
        narrowed = [r for r in residues if str(r.id).strip() == str(resnum)]
        residues = narrowed or residues
    if len(residues) != 1:
        raise AbfeError(code="abfe_ligand_not_found" if not residues else "abfe_ligand_ambiguous",
                        message=f"ligand residue {name} matched {len(residues)} residues in the built topology; one "
                        "copy of the ligand is decoupled (pass --ligand CHAIN:RESNAME:RESNUM)")
    return [a.index for a in residues[0].atoms()]


def _leg_of(topology, ligand_atoms: set[int]) -> str:
    """``complex`` when anything but the ligand, water and monatomic ions is present."""
    for residue in topology.residues():
        atoms = list(residue.atoms())
        if atoms[0].index in ligand_atoms or residue.name.upper() in _SOLVENT_RESIDUES or len(atoms) == 1:
            continue
        return LEG_COMPLEX
    return LEG_SOLVENT


# --------------------------------------------------------------------------- #
# extract_ligand: the solvent leg's prep node                                   #
# --------------------------------------------------------------------------- #

@node_tool(node_type="prep")
def extract_ligand(
    ligand: Optional[str] = None,
    pdb_file: Optional[str] = None,
    ligand_chemistry: Optional[List[Dict[str, Any]]] = None,
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Prepare the ligand alone as the solvent leg of an absolute binding free energy.

    Node mode: a ``prep`` node whose parent is the complex's completed ``prep``
    node. The ligand's atoms are copied from that parent's ``merged_pdb`` with
    their coordinates, names and residue identity, and its ``ligand_chemistry``
    record is carried over, so both legs use one parameterisation. The node is
    marked ``leg_role = "solvent"``. Continue with ``solvate_structure`` ->
    ``build_decoupled_system`` -> min -> eq -> ``run_fep`` -> ``analyze_fep``.

    Args:
        ligand: ``RESNAME``, ``CHAIN:RESNAME`` or ``CHAIN:RESNAME:RESNUM``;
            optional when the parent prepared exactly one ligand.
        pdb_file / ligand_chemistry: direct mode (resolved from the parent prep
            in node mode).
        output_dir / job_dir / node_id: standard mdclaw knobs.

    Returns:
        ``merged_pdb``, ``ligand``, ``leg_role`` and, on failure, ``code``
        (``abfe_ligand_prep_required``, ``abfe_ligand_not_found``,
        ``abfe_ligand_ambiguous``).
    """
    from mdclaw._node import fail_tool
    from mdclaw.fep.tripeptide import _chain_identity_map

    result: dict = {"success": False, "tool": "extract_ligand", "errors": [], "warnings": []}
    node_mode = bool(job_dir and node_id)

    def _fail(code: str, message: str, extra: Optional[dict] = None) -> dict:
        return fail_tool(result, code, message, job_dir=job_dir, node_id=node_id, extra=extra)

    parent_prep_id = None
    if node_mode:
        from mdclaw._node import _find_ancestor_node_id, find_ancestor_artifact, validate_node_execution_context

        ctx = validate_node_execution_context(job_dir, node_id, "prep", actual_conditions={"ligand": ligand})
        if not ctx["success"]:
            from mdclaw._node import fail_node_from_result

            return fail_node_from_result(job_dir, node_id, {"success": False, "error_type": "ValidationError", **ctx},
                                         default_error="extract_ligand node execution context invalid")
        parent_prep_id = _find_ancestor_node_id(job_dir, node_id, "prep")
        resolved = find_ancestor_artifact(job_dir, node_id, "prep", "merged_pdb") if parent_prep_id else None
        if not resolved:
            result["next_action"] = (f"mdclaw create_node --job-dir {job_dir} --node-type prep --parent-node-ids "
                                     "<completed prepare_complex prep node>, then run extract_ligand on the returned node_id")
            result["hints"] = [f"'{node_id}' cannot be re-parented; retire it with: mdclaw update_workflow_state "
                               f"--job-dir {job_dir} --node-id {node_id} --abandon --reason 'wrong parent'"]
            return _fail(code="abfe_ligand_prep_required",
                         message="extract_ligand needs a completed prep parent (prepare_complex) that holds the complex; "
                         "create this prep node with --parent-node-ids <that prep node>")
        pdb_file = resolved
        ligand_chemistry = find_ancestor_artifact(job_dir, node_id, "prep", "ligand_chemistry")
    if not pdb_file or not Path(pdb_file).is_file():
        return _fail(code="file_not_found", message=f"pdb_file not found: {pdb_file}")
    try:
        record = select_ligand_record(ligand_chemistry, ligand)
    except AbfeError as exc:
        return _fail(exc.code, str(exc), exc.extra)

    # merge_structures relabels chains, so the residue is found by name and
    # number; two copies under one name and number cannot be told apart.
    name, resnum = str(record["residue_name"]).upper(), record.get("resnum")
    lines = [ln for ln in Path(pdb_file).read_text().splitlines()
             if ln.startswith(("ATOM  ", "HETATM")) and ln[17:20].strip().upper() == name
             and (resnum is None or ln[22:26].strip() == str(resnum))]
    if not lines:
        return _fail(code="abfe_ligand_not_found",
                     message=f"no atoms of ligand {_describe(record)} in {pdb_file}")
    if len({(ln[21], ln[22:27]) for ln in lines}) != 1:
        return _fail(code="abfe_ligand_ambiguous",
                     message=f"ligand {_describe(record)} matches several residues in {pdb_file}; one copy is decoupled")
    try:
        _refuse_charged(record, record.get("net_charge"))
    except AbfeError as exc:
        return _fail(exc.code, str(exc))

    if node_mode:
        from mdclaw._node import begin_node

        out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        out_dir = create_unique_subdir(Path(output_dir) if output_dir else WORKING_DIR, "ligand_leg").resolve()
    merge_dir = out_dir / "merge"
    merge_dir.mkdir(exist_ok=True)
    merged_pdb = merge_dir / "merged.pdb"
    merged_pdb.write_text("\n".join([f"REMARK   1 MDCLAW extract_ligand {_describe(record)} from {Path(pdb_file).name}",
                                     *lines, "TER", "END", ""]))
    (out_dir / "chain_identity_map.json").write_text(json.dumps(_chain_identity_map(merged_pdb, Path(pdb_file)), indent=2))
    (out_dir / "disulfide_bonds.json").write_text("[]")
    # The record's file paths come back absolute from the DAG and are stored
    # relative to this node again (``../<parent prep>/artifacts/...``), so both
    # legs read one and the same prepared ligand.
    summary = _ligand_summary(record)
    result.update({"success": True, "output_dir": str(out_dir), "merged_pdb": str(merged_pdb), "ligand": summary,
                   "n_atoms": len(lines), "leg_role": LEG_SOLVENT})
    if node_mode:
        from mdclaw._node import complete_node

        complete_node(
            job_dir, node_id,
            artifacts={"merged_pdb": "artifacts/merge/merged.pdb",
                       "chain_identity_map": "artifacts/chain_identity_map.json",
                       "disulfide_bonds": "artifacts/disulfide_bonds.json",
                       "ligand_chemistry": [record]},
            metadata={"tool": "extract_ligand", "leg_role": LEG_SOLVENT, "ligand": summary,
                      "derived_from_prep_node_id": parent_prep_id,
                      "statistics": {"num_atoms": len(lines), "num_residues": 1}},
            warnings=result["warnings"],
        )
    return result


# --------------------------------------------------------------------------- #
# build_decoupled_system: topo node of either leg                               #
# --------------------------------------------------------------------------- #

@node_tool(node_type="topo")
def build_decoupled_system(
    ligand: Optional[str] = None,
    pdb_file: Optional[str] = None,
    box_dimensions: Optional[Dict[str, float]] = None,
    forcefield: Optional[str] = None,
    water_model: Optional[str] = None,
    hmr: bool = True,
    is_membrane: Optional[bool] = None,
    ligand_chemistry: Optional[List[Dict[str, Any]]] = None,
    disulfide_bonds: Optional[List[Dict[str, Any]]] = None,
    elec_lambdas: Optional[str] = None,
    sterics_lambdas: Optional[str] = None,
    softcore_alpha: float = DEFAULT_SOFTCORE_ALPHA,
    endpoint_tolerance_kj_mol: float = 1.0,
    platform: str = "auto",
    device_index: Optional[str] = None,
    output_name: str = "system",
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Build the alchemical topology that decouples one ligand, as a ``topo`` node.

    Runs ``build_amber_system`` once and rewrites the ligand's nonbonded
    interactions so two global parameters switch it off: ``fep_elec_old``
    (charges) then ``fep_sterics_old`` (soft-core LJ against everything else).
    At their defaults the System is the ordinary coupled system, so ``min`` /
    ``eq`` need no ABFE awareness. Used for both legs of an absolute binding
    free energy; which leg this is follows from the content:

    - ligand alone in water -> **solvent leg**: ``fep_protocol.json`` is
      written and the next stages are min -> eq -> ``run_fep``;
    - ligand with a receptor -> **complex leg**: no protocol is written, since
      a decoupled ligand must be held in the pose. After min -> eq, create a
      ``topo`` child of the ``eq`` node and run ``add_boresch_restraint``.

    Args:
        ligand: ``RESNAME``, ``CHAIN:RESNAME`` or ``CHAIN:RESNAME:RESNUM`` of
            the ligand to decouple; optional with exactly one prepared ligand.
        pdb_file / box_dimensions / forcefield / water_model / hmr /
            is_membrane / ligand_chemistry / disulfide_bonds: as
            ``build_amber_system``; resolved from the DAG in node mode.
        elec_lambdas: Charge scaling from 1 to 0 (default ``1,0.75,0.5,0.25,0``).
        sterics_lambdas: Soft-core scaling from 1 to 0 (default 14 values,
            denser near 0). Use the same schedules on both legs.
        softcore_alpha: Beutler soft-core alpha.
        endpoint_tolerance_kj_mol: Allowed end-point energy differences.
        platform / device_index / output_name / output_dir / job_dir / node_id:
            standard mdclaw knobs.

    Returns:
        The XML triple, ``hybrid_manifest`` (``kind: abfe_decouple``), ``leg``,
        ``ligand``, ``endpoint_validation`` and, for the solvent leg,
        ``fep_protocol``. Codes: ``abfe_ligand_not_found``,
        ``abfe_ligand_ambiguous``, ``abfe_charged_ligand_unsupported``,
        ``abfe_ligand_covalent``, ``abfe_ligand_invalid``,
        ``fep_unsupported_force``, ``fep_endstate_build_failed``,
        ``fep_endpoint_validation_failed``, ``fep_protocol_invalid``.
    """
    import numpy as np
    import openmm
    from openmm import unit
    from openmm.app import PDBFile

    from mdclaw._node import fail_tool
    from mdclaw.amber.build_system import build_amber_system
    from mdclaw.fep.build import BuildStepError, _resolve_inputs, fastest_platform_name
    from mdclaw.simulation._base import resolve_platform_name

    result: dict = {"success": False, "tool": "build_decoupled_system", "errors": [], "warnings": []}
    node_mode = bool(job_dir and node_id)

    def _fail(code: str, message: str, extra: Optional[dict] = None) -> dict:
        return fail_tool(result, code, message, job_dir=job_dir, node_id=node_id, extra=extra)

    try:
        elec = _parse_lambdas(elec_lambdas, DEFAULT_ELEC_LAMBDAS, "elec_lambdas", start=1.0, end=0.0)
        sterics = _parse_lambdas(sterics_lambdas, DEFAULT_STERICS_LAMBDAS, "sterics_lambdas", start=1.0, end=0.0)
        platform_name, platform_properties = resolve_platform_name(platform, device_index)
    except ProtocolError as exc:
        return _fail(exc.code, str(exc))
    except ValueError as exc:
        return _fail(code="invalid_parameter_value", message=str(exc))
    try:
        inputs = _resolve_inputs(
            job_dir=job_dir, node_id=node_id, mutation=None, pdb_file=pdb_file, forcefield=forcefield,
            water_model=water_model, hmr=hmr, is_membrane=is_membrane, ligand_chemistry=ligand_chemistry,
            disulfide_bonds=disulfide_bonds, box_dimensions=box_dimensions, mutant_backend=None, n_windows=None,
            lambda_schedule=None, softcore_alpha=softcore_alpha, output_name=output_name, platform=platform,
            conditions={"ligand": ligand, "hmr": hmr, "is_membrane": is_membrane, "elec_lambdas": elec_lambdas,
                        "sterics_lambdas": sterics_lambdas, "softcore_alpha": softcore_alpha,
                        "output_name": output_name, "platform": platform})
        record = select_ligand_record(inputs.ligand_chemistry, ligand)
        _refuse_charged(record, record.get("net_charge"))   # before the build, so the node stays pending
    except BuildStepError as exc:
        if exc.recorded:
            return {**result, **exc.extra, "success": False, "code": exc.code}
        return _fail(exc.code, str(exc), exc.extra)
    except AbfeError as exc:
        return _fail(exc.code, str(exc), exc.extra)
    result["warnings"].extend(inputs.warnings)
    ligand_info = _ligand_summary(record)

    if node_mode:
        from mdclaw._node import begin_node

        out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        out_dir = create_unique_subdir(Path(output_dir) if output_dir else WORKING_DIR, "decoupled_topology")
    result["output_dir"] = str(out_dir)

    try:
        endstate_dir = out_dir / "endstates"
        endstate_dir.mkdir(exist_ok=True)
        built = build_amber_system(
            pdb_file=str(inputs.pdb_file), output_dir=str(endstate_dir), output_name="coupled",
            box_dimensions=inputs.box_dimensions, forcefield=inputs.forcefield, water_model=inputs.water_model,
            is_membrane=inputs.is_membrane, hmr=hmr, ligand_chemistry=inputs.ligand_chemistry,
            disulfide_bonds=inputs.disulfide_bonds)
        if not built.get("success"):
            raise AbfeError(code="fep_endstate_build_failed",
                            message=f"coupled-state build failed ({built.get('code')}): "
                            + "; ".join(built.get("errors", [])[:3]),
                            extra={"endstate_result": {k: built.get(k) for k in ("code", "errors", "warnings", "output_dir")}})
        result["warnings"].extend(f"[coupled] {w}" for w in built.get("warnings", []))

        topology = PDBFile(built["topology_pdb"]).topology
        system = openmm.XmlSerializer.deserialize(Path(built["system_xml"]).read_text())
        state = openmm.XmlSerializer.deserialize(Path(built["state_xml"]).read_text())
        positions = np.asarray(state.getPositions(asNumpy=True).value_in_unit(unit.nanometer))
        system.setDefaultPeriodicBoxVectors(*state.getPeriodicBoxVectors())
        atoms = ligand_atoms_in_topology(topology, record)
        leg = _leg_of(topology, set(atoms))

        alchemical, report = decouple_ligand(system, atoms, softcore_alpha=softcore_alpha)
        _refuse_charged(record, report["ligand_net_charge_e"])   # the assigned charges have the last word
        validation = validate_decoupling(alchemical, system, positions, atoms,
                                         platform_name=platform_name or fastest_platform_name(),
                                         platform_properties=platform_properties,
                                         tolerance_kj_mol=endpoint_tolerance_kj_mol)
    except (AbfeError, HybridBuildError) as exc:
        return _fail(exc.code, str(exc), getattr(exc, "extra", None))
    except Exception as exc:  # noqa: BLE001
        return _fail(code="fep_hybrid_build_failed", message=f"{type(exc).__name__}: {exc}")

    files = {
        "system_xml": out_dir / f"{output_name}.system.xml",
        "topology_pdb": out_dir / f"{output_name}.topology.pdb",
        "state_xml": out_dir / f"{output_name}.state.xml",
        "hybrid_manifest": out_dir / "hybrid_manifest.json",
        "amber_metadata": out_dir / "amber_metadata.json",
    }
    files["system_xml"].write_text(openmm.XmlSerializer.serialize(alchemical))
    shutil.copyfile(built["topology_pdb"], files["topology_pdb"])
    shutil.copyfile(built["state_xml"], files["state_xml"])
    schedules = {"elec_lambdas": elec, "sterics_lambdas": sterics}
    n_residues = sum(1 for _ in topology.residues())
    manifest = {
        "schema_version": 1, "kind": ABFE_KIND, "leg": leg, "ligand": ligand_info, "ligand_atom_indices": atoms,
        "decoupling_report": report, "endpoint_validation": validation, "schedules": schedules,
        "forcefield": inputs.forcefield, "water_model": inputs.water_model if inputs.box_dimensions else None,
        "hmr": bool(hmr), "softcore_alpha": float(softcore_alpha), "charge_correction": "none",
        "restraint_required": leg == LEG_COMPLEX,
        "endstates": {"coupled": {k: built.get(k) for k in ("system_xml", "topology_pdb", "state_xml", "system_net_charge_e")}},
        "statistics": {"num_atoms": alchemical.getNumParticles(), "num_residues": n_residues},
    }
    files["hybrid_manifest"].write_text(json.dumps(manifest, indent=2, default=str))
    artifacts = {"system_xml": f"artifacts/{output_name}.system.xml", "topology_pdb": f"artifacts/{output_name}.topology.pdb",
                 "state_xml": f"artifacts/{output_name}.state.xml", "hybrid_manifest": "artifacts/hybrid_manifest.json",
                 "amber_metadata": "artifacts/amber_metadata.json"}
    n_windows = None
    if leg == LEG_SOLVENT:
        windows, phases = abfe_windows(elec, sterics)
        files["fep_protocol"] = out_dir / "fep_protocol.json"
        files["fep_protocol"].write_text(json.dumps(
            _protocol(ligand_info, leg, windows, phases, softcore_alpha, schedules), indent=2))
        artifacts["fep_protocol"] = "artifacts/fep_protocol.json"
        n_windows = len(windows)
    files["amber_metadata"].write_text(json.dumps({
        "success": True, "tool": "build_decoupled_system", "solvent_type": built.get("solvent_type"),
        "parameters": {**(built.get("parameters") or {}), "ligand": ligand_info, "leg": leg,
                       "softcore_alpha": float(softcore_alpha), **schedules},
        "forcefield_provenance": built.get("forcefield_provenance"), "statistics": manifest["statistics"],
        "hybrid_manifest": "artifacts/hybrid_manifest.json", "warnings": result["warnings"],
    }, indent=2, default=str))

    result.update({**{k: str(v) for k, v in files.items() if k != "amber_metadata"}, "leg": leg, "ligand": ligand_info,
                   "endpoint_validation": validation, "n_windows": n_windows, "statistics": manifest["statistics"]})
    if not validation["passed"]:
        return _fail(code="fep_endpoint_validation_failed", message=f"the alchemical System does not reproduce the built one (coupled d = "
                     f"{validation['coupled']['difference_kj_mol']:.3g}, decoupled-move d = "
                     f"{validation['decoupled']['difference_kj_mol']:.3g} kJ/mol, tol = {validation['tolerance_kj_mol']:.3g})")
    result["success"] = True
    if leg == LEG_COMPLEX:
        result["next_steps"] = [
            "min -> eq on this topology (the ligand is fully coupled at the defaults)",
            "mdclaw create_node --job-dir <job> --node-type topo --parent-node-ids <eq node>",
            "mdclaw --job-dir <job> --node-id <that topo> add_boresch_restraint",
            "fep nodes under that topo (run_fep) -> analyze_fep",
        ]
    if node_mode:
        from mdclaw._node import complete_node, update_job_summaries

        solvent_type = "explicit" if inputs.box_dimensions else "vacuum"
        complete_node(
            job_dir, node_id, artifacts=artifacts,
            metadata={
                "tool": "build_decoupled_system", "forcefield": inputs.forcefield,
                "effective_forcefield": (built.get("parameters") or {}).get("effective_forcefield", inputs.forcefield),
                "water_model": inputs.water_model if solvent_type == "explicit" else None,
                "solvent_type": solvent_type, "implicit_solvent": None, "hmr": bool(hmr),
                "is_membrane": bool(inputs.is_membrane), "system_artifact_kind": "openmm_system_xml",
                "forcefield_provenance": built.get("forcefield_provenance"),
                "fep": {"kind": ABFE_KIND, "leg": leg, "mutation": f"decouple:{ligand_info['residue_name']}",
                        "ligand": ligand_info, "n_windows": n_windows, "restraint_required": leg == LEG_COMPLEX,
                        "endpoint_validation_passed": True},
            },
            warnings=result["warnings"],
        )
        update_job_summaries(job_dir, params={"forcefield": inputs.forcefield, "solvation_type": solvent_type,
                                              "abfe_ligand": ligand_info["residue_name"]})
    return result


# --------------------------------------------------------------------------- #
# add_boresch_restraint: topo child of the complex leg's eq node                #
# --------------------------------------------------------------------------- #

def _sample_frames(system, positions, box_vectors, *, temperature_kelvin: float, sampling_time_ps: float,
                   frame_interval_ps: float, platform_name: Optional[str], platform_properties: dict,
                   random_seed: Optional[int]):
    """Short NVT run of the coupled complex; returns ``(frames_nm, boxes_nm, final_state)``."""
    import numpy as np
    import openmm
    from openmm import unit

    integrator = openmm.LangevinMiddleIntegrator(temperature_kelvin * unit.kelvin, 1.0 / unit.picosecond,
                                                 2.0 * unit.femtoseconds)
    if random_seed is not None:
        integrator.setRandomNumberSeed(int(random_seed))
    if platform_name:
        context = openmm.Context(system, integrator, openmm.Platform.getPlatformByName(platform_name),
                                 dict(platform_properties or {}))
    else:
        context = openmm.Context(system, integrator)
    if box_vectors is not None:
        context.setPeriodicBoxVectors(*box_vectors)
    context.setPositions(positions)
    context.setVelocitiesToTemperature(temperature_kelvin * unit.kelvin)
    steps = max(1, int(round(frame_interval_ps / 0.002)))
    n_frames = max(1, int(round(sampling_time_ps / frame_interval_ps)))
    frames, boxes = [], []
    for _ in range(n_frames):
        integrator.step(steps)
        state = context.getState(getPositions=True)
        frames.append(np.asarray(state.getPositions(asNumpy=True).value_in_unit(unit.nanometer)))
        boxes.append(np.asarray(state.getPeriodicBoxVectors(asNumpy=True).value_in_unit(unit.nanometer)))
    final = context.getState(getPositions=True, getVelocities=True, getEnergy=True, enforcePeriodicBox=False)
    del context, integrator
    return np.array(frames), (np.array(boxes) if box_vectors is not None else None), final


@node_tool(node_type="topo")
def add_boresch_restraint(
    restraint_lambdas: Optional[str] = None,
    sampling_time_ps: float = 200.0,
    frame_interval_ps: float = 2.0,
    temperature_kelvin: float = 300.0,
    k_distance: float = DEFAULT_K_DISTANCE,
    k_angle: float = DEFAULT_K_ANGLE,
    system_xml_file: Optional[str] = None,
    topology_pdb_file: Optional[str] = None,
    state_xml_file: Optional[str] = None,
    hybrid_manifest_file: Optional[str] = None,
    platform: str = "auto",
    device_index: Optional[str] = None,
    random_seed: Optional[int] = None,
    output_name: str = "system",
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Add the Boresch restraint to a complex leg, as a ``topo`` child of its ``eq`` node.

    Samples the equilibrated, fully coupled complex for ``sampling_time_ps``,
    picks three backbone atoms of the receptor and three bonded heavy atoms of
    the ligand whose six relative coordinates fluctuate least, takes their
    means as reference values, and writes a new XML triple: the parent
    topology plus one ``CustomCompoundBondForce`` scaled by the global
    ``fep_restraint``.
    ``fep_protocol.json`` then switches the restraint on, the charges off and
    the sterics off. The choice is made here and recorded, never passed in.

    Node mode: ``create_node --node-type topo --parent-node-ids <eq node>``;
    the topology comes from the ``build_decoupled_system`` ancestor and the
    coordinates from the ``eq`` parent. ``fep`` nodes hang directly under this
    node and start from that ``eq`` state; every window equilibrates at its own
    lambda, so no separate restrained equilibration is needed.

    Args:
        restraint_lambdas: ``fep_restraint`` from 0 to 1
            (default ``0,0.02,0.08,0.2,0.5,1``).
        sampling_time_ps / frame_interval_ps: length and stride of the
            selection run (default 200 ps, one frame every 2 ps).
        temperature_kelvin: temperature of the selection run and of the
            thermal widths used for scoring.
        k_distance / k_angle: force constants (kJ/mol/nm^2, kJ/mol/rad^2;
            defaults 10 kcal/mol/A^2 and 20 kcal/mol/rad^2).
        system_xml_file / topology_pdb_file / state_xml_file /
            hybrid_manifest_file: direct mode.
        platform / device_index / random_seed / output_name / output_dir /
            job_dir / node_id: standard mdclaw knobs.

    Returns:
        The XML triple, ``hybrid_manifest``, ``fep_protocol``, the ``boresch``
        block (atoms, reference values, fluctuation statistics) and
        ``restraint_free_energy_kj_mol``. Codes: ``abfe_topology_required``,
        ``abfe_restraint_unstable``, ``abfe_ligand_too_small``,
        ``abfe_receptor_missing``, ``fep_protocol_invalid``.
    """
    import numpy as np
    import openmm
    from openmm.app import PDBFile

    from mdclaw._node import fail_tool
    from mdclaw.fep.build import fastest_platform_name
    from mdclaw.simulation._base import resolve_platform_name

    result: dict = {"success": False, "tool": "add_boresch_restraint", "errors": [], "warnings": []}
    node_mode = bool(job_dir and node_id)

    def _fail(code: str, message: str) -> dict:
        return fail_tool(result, code, message, job_dir=job_dir, node_id=node_id)

    try:
        lambdas = _parse_lambdas(restraint_lambdas, DEFAULT_RESTRAINT_LAMBDAS, "restraint_lambdas", start=0.0, end=1.0)
        platform_name, platform_properties = resolve_platform_name(platform, device_index)
    except ProtocolError as exc:
        return _fail(exc.code, str(exc))
    except ValueError as exc:
        return _fail(code="invalid_parameter_value", message=str(exc))
    if sampling_time_ps <= 0 or frame_interval_ps <= 0 or sampling_time_ps / frame_interval_ps < 10:
        return _fail(code="invalid_parameter_value", message="sampling_time_ps / frame_interval_ps must give at least 10 frames")

    parent_topo_id = None
    if node_mode:
        from mdclaw._node import fail_node_from_result, validate_node_execution_context
        from mdclaw.node.inputs import _resolve_md_restart, _resolve_topology_files
        from mdclaw.node.io import _read_artifact_from_node

        ctx = validate_node_execution_context(
            job_dir, node_id, "topo",
            actual_conditions={"restraint_lambdas": restraint_lambdas, "sampling_time_ps": sampling_time_ps,
                               "frame_interval_ps": frame_interval_ps, "temperature_kelvin": temperature_kelvin,
                               "k_distance": k_distance, "k_angle": k_angle, "platform": platform,
                               "random_seed": random_seed, "output_name": output_name})
        if not ctx["success"]:
            return fail_node_from_result(job_dir, node_id, {"success": False, "error_type": "ValidationError", **ctx},
                                         default_error="add_boresch_restraint node execution context invalid")
        triple = _resolve_topology_files(job_dir, node_id)
        restart = _resolve_md_restart(job_dir, node_id)
        parent_topo_id = triple.get("topology_resolved_from_node_id")
        if not parent_topo_id or not restart.get("restart_from"):
            return _fail(code="abfe_topology_required", message="add_boresch_restraint runs on a topo node whose parent is the completed eq node of a complex "
                         "leg built by build_decoupled_system (create_node --node-type topo --parent-node-ids <eq node>)")
        system_xml_file, topology_pdb_file = triple["system_xml_file"], triple["topology_pdb_file"]
        state_xml_file = restart["restart_from"]
        hybrid_manifest_file = _read_artifact_from_node(job_dir, parent_topo_id, "hybrid_manifest")
    for label, path in (("system_xml_file", system_xml_file), ("topology_pdb_file", topology_pdb_file),
                        ("state_xml_file", state_xml_file), ("hybrid_manifest_file", hybrid_manifest_file)):
        if not path or not Path(path).is_file():
            return _fail(code="abfe_topology_required", message=f"{label} not found ({path}); it comes from build_decoupled_system")
    try:
        manifest = json.loads(Path(hybrid_manifest_file).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return _fail(code="abfe_topology_required", message=f"cannot read {hybrid_manifest_file}: {exc}")
    if manifest.get("kind") != ABFE_KIND or manifest.get("leg") != LEG_COMPLEX or manifest.get("boresch"):
        return _fail(code="abfe_topology_required", message=f"the topology is not an unrestrained complex leg (kind={manifest.get('kind')!r}, "
                     f"leg={manifest.get('leg')!r}, restrained={bool(manifest.get('boresch'))}); only the complex leg of "
                     "build_decoupled_system takes a Boresch restraint, once")
    if not str(state_xml_file).endswith(".xml"):
        return _fail(code="abfe_topology_required", message=f"the eq parent's restart artifact is not a portable XML state: {state_xml_file}")

    if node_mode:
        from mdclaw._node import begin_node

        out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        out_dir = create_unique_subdir(Path(output_dir) if output_dir else WORKING_DIR, "boresch_topology")
    result["output_dir"] = str(out_dir)

    try:
        topology = PDBFile(str(topology_pdb_file)).topology
        system = openmm.XmlSerializer.deserialize(Path(system_xml_file).read_text())
        state = openmm.XmlSerializer.deserialize(Path(state_xml_file).read_text())
        periodic = system.usesPeriodicBoundaryConditions()
        box = state.getPeriodicBoxVectors() if periodic else None
        frames, boxes, final = _sample_frames(
            system, state.getPositions(), box, temperature_kelvin=temperature_kelvin,
            sampling_time_ps=sampling_time_ps, frame_interval_ps=frame_interval_ps,
            platform_name=platform_name or fastest_platform_name(), platform_properties=platform_properties,
            random_seed=random_seed)
        if not np.all(np.isfinite(frames)):
            raise BoreschError(code="abfe_restraint_unstable", message="the selection run produced non-finite coordinates")
        restraint = select_boresch_restraint(topology, frames, boxes, manifest["ligand_atom_indices"],
                                             temperature_kelvin=temperature_kelvin, k_distance=k_distance, k_angle=k_angle,
                                             bonds=bonded_pairs(system))
        if periodic:
            system.setDefaultPeriodicBoxVectors(*final.getPeriodicBoxVectors())
        system.addForce(boresch_force(restraint, periodic=periodic, default_scale=1.0))
        windows, phases = abfe_windows(manifest["schedules"]["elec_lambdas"], manifest["schedules"]["sterics_lambdas"], lambdas)
    except BoreschError as exc:
        return _fail(exc.code, str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(code="fep_hybrid_build_failed", message=f"{type(exc).__name__}: {exc}")

    atoms = list(topology.atoms())

    def _name(index: int) -> str:
        a = atoms[index]
        return f"{a.residue.chain.id}:{a.residue.name}{a.residue.id}:{a.name}"

    boresch = {**restraint.to_json(), "receptor_atom_names": [_name(i) for i in restraint.receptor_atoms],
               "ligand_atom_names": [_name(i) for i in restraint.ligand_atoms],
               "selection_run": {"sampling_time_ps": sampling_time_ps, "frame_interval_ps": frame_interval_ps,
                                 "temperature_kelvin": temperature_kelvin}}
    dg_restraint = standard_state_restraint_free_energy(restraint, temperature_kelvin)
    schedules = {**manifest["schedules"], "restraint_lambdas": lambdas}
    new_manifest = {**manifest, "boresch": boresch, "schedules": schedules, "restraint_required": False,
                    "derived_from_topo_node_id": parent_topo_id,
                    "restraint_free_energy_kj_mol": {"temperature_kelvin": temperature_kelvin, "value": dg_restraint}}
    files = {"system_xml": out_dir / f"{output_name}.system.xml", "topology_pdb": out_dir / f"{output_name}.topology.pdb",
             "state_xml": out_dir / f"{output_name}.state.xml", "hybrid_manifest": out_dir / "hybrid_manifest.json",
             "fep_protocol": out_dir / "fep_protocol.json", "amber_metadata": out_dir / "amber_metadata.json"}
    files["system_xml"].write_text(openmm.XmlSerializer.serialize(system))
    shutil.copyfile(topology_pdb_file, files["topology_pdb"])
    files["state_xml"].write_text(openmm.XmlSerializer.serialize(final))
    files["hybrid_manifest"].write_text(json.dumps(new_manifest, indent=2, default=str))
    files["fep_protocol"].write_text(json.dumps(
        _protocol(manifest["ligand"], LEG_COMPLEX, windows, phases, manifest.get("softcore_alpha", DEFAULT_SOFTCORE_ALPHA),
                  schedules), indent=2))
    parent_meta: dict = {}
    parent_amber = Path(hybrid_manifest_file).parent / "amber_metadata.json"
    if parent_amber.is_file():
        try:
            parent_meta = json.loads(parent_amber.read_text())
        except json.JSONDecodeError:
            parent_meta = {}
    files["amber_metadata"].write_text(json.dumps({
        "success": True, "tool": "add_boresch_restraint", "solvent_type": parent_meta.get("solvent_type"),
        "parameters": {**(parent_meta.get("parameters") or {}), "boresch": boresch, **schedules},
        "forcefield_provenance": parent_meta.get("forcefield_provenance"),
        "statistics": manifest.get("statistics"), "hybrid_manifest": "artifacts/hybrid_manifest.json",
        "warnings": result["warnings"],
    }, indent=2, default=str))

    result.update({**{k: str(v) for k, v in files.items() if k != "amber_metadata"}, "success": True,
                   "leg": LEG_COMPLEX, "ligand": manifest["ligand"], "boresch": boresch, "n_windows": len(windows),
                   "restraint_free_energy_kj_mol": dg_restraint})
    if node_mode:
        from mdclaw._node import complete_node, read_node

        inherited = (read_node(job_dir, parent_topo_id).get("metadata") or {}) if parent_topo_id else {}
        complete_node(
            job_dir, node_id,
            artifacts={"system_xml": f"artifacts/{output_name}.system.xml",
                       "topology_pdb": f"artifacts/{output_name}.topology.pdb",
                       "state_xml": f"artifacts/{output_name}.state.xml",
                       "hybrid_manifest": "artifacts/hybrid_manifest.json", "fep_protocol": "artifacts/fep_protocol.json",
                       "amber_metadata": "artifacts/amber_metadata.json"},
            metadata={
                **{k: inherited.get(k) for k in ("forcefield", "effective_forcefield", "water_model", "solvent_type",
                                                 "implicit_solvent", "hmr", "is_membrane", "system_artifact_kind",
                                                 "forcefield_provenance")},
                "tool": "add_boresch_restraint",
                "fep": {**(inherited.get("fep") or {}), "n_windows": len(windows), "restraint_required": False,
                        "restraint": "boresch", "derived_from_topo_node_id": parent_topo_id,
                        "boresch_atoms": {"receptor": boresch["receptor_atom_names"], "ligand": boresch["ligand_atom_names"]}},
            },
            warnings=result["warnings"],
        )
    return result


# --------------------------------------------------------------------------- #
# estimate_binding_dg: comparison analyze node over the two legs                #
# --------------------------------------------------------------------------- #

def _leg_manifest(leg: dict, label: str) -> dict:
    path = leg.get("hybrid_manifest_file")
    try:
        manifest = json.loads(Path(path).read_text())
    except (TypeError, OSError, json.JSONDecodeError) as exc:
        raise AbfeError(code="abfe_legs_invalid", message=f"{label}: cannot read the leg's hybrid manifest ({path}): {exc}") from exc
    if manifest.get("kind") != ABFE_KIND:
        raise AbfeError(code="abfe_legs_invalid",
                        message=f"{label}: not a ligand-decoupling leg (kind={manifest.get('kind')!r}); "
                        "estimate_binding_dg takes legs built by build_decoupled_system (a mutation ddG uses estimate_ddg)")
    return manifest


@node_tool(node_type="analyze")
def estimate_binding_dg(
    complex: Optional[str] = None,  # noqa: A002 - the leg's name is the argument's name
    solvent: Optional[str] = None,
    ligand_symmetry_number: int = 1,
    output_file: Optional[str] = None,
    output_name: str = "binding_dg",
    study_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Standard binding free energy from the two legs' ``analyze_fep`` results.

    ``dG_bind = dG(solvent leg) - dG(complex leg) + dG_restraint - kT ln(sigma)``
    where each leg's dG is the MBAR free energy of its whole protocol (the
    complex leg's includes switching the Boresch restraint on), and
    ``dG_restraint`` is the analytic free energy of taking the decoupled ligand
    from the 1 M standard state into that restraint. Errors add in quadrature.

    Node mode: an ``analyze`` node with
    ``--conditions '{"analysis_data_scope": "comparison"}'`` whose two parents
    are the legs' ``analyze_fep`` nodes (any order); which is which is read
    from each leg's topology manifest. The legs must decouple the same ligand
    with the same force field, water model, HMR, soft core and temperature
    (``abfe_legs_incompatible``).

    Args:
        complex / solvent: the legs' ``fep_result.json`` (direct mode).
        ligand_symmetry_number: number of indistinguishable orientations of
            the ligand that the restraint confines it to one of (benzene: 12).
            Adds ``-kT ln(sigma)``. Default 1.
        output_file / output_name / study_dir / job_dir / node_id: as
            ``estimate_ddg``.

    Returns:
        ``dG_bind_kj_mol`` / ``dG_bind_error_kj_mol`` (and kcal/mol), the terms,
        both legs' dG, ``binding_dg_file``. Codes: ``abfe_scope_invalid``,
        ``abfe_legs_invalid``, ``abfe_legs_incompatible``,
        ``fep_result_invalid``, ``file_not_found``.
    """
    from mdclaw._node import fail_tool
    from mdclaw.fep.analysis import FEP_LEG_ANALYSIS, FepAnalysisError, _load_leg, _study_dir_from_job

    result: dict = {"success": False, "tool": "estimate_binding_dg", "errors": [], "warnings": []}
    node_mode = bool(job_dir and node_id)

    def _fail(code: str, message: str) -> dict:
        return fail_tool(result, code, message, job_dir=job_dir, node_id=node_id)

    if not isinstance(ligand_symmetry_number, int) or ligand_symmetry_number < 1:
        return _fail(code="invalid_parameter_value", message=f"ligand_symmetry_number must be an integer >= 1, got {ligand_symmetry_number!r}")
    leg_nodes: dict[str, Optional[str]] = {LEG_COMPLEX: None, LEG_SOLVENT: None}
    try:
        if node_mode:
            from mdclaw._node import read_node

            node = read_node(job_dir, node_id)
            scope = (node.get("conditions") or {}).get("analysis_data_scope")
            if scope != "comparison":
                raise AbfeError(code="abfe_scope_invalid",
                                message="estimate_binding_dg consumes two legs; create the analyze node with --conditions "
                                f"'{{\"analysis_data_scope\": \"comparison\"}}' (got {scope!r})")
            parents = list(node.get("parent_node_ids") or [])
            files: dict[str, str] = {}
            for pid in parents:
                pnode = read_node(job_dir, pid)
                artifact = (pnode.get("artifacts") or {}).get("fep_result")
                if (pnode.get("node_type") != "analyze" or (pnode.get("metadata") or {}).get("analysis") != FEP_LEG_ANALYSIS
                        or pnode.get("status") != "completed" or not artifact):
                    raise AbfeError(code="abfe_legs_invalid",
                                    message=f"parent {pid} is not a completed analyze_fep node; parent this node to the "
                                    "complex and solvent legs' analyze_fep nodes")
                files[pid] = str((Path(job_dir) / "nodes" / pid / artifact).resolve())
            if len(parents) != 2:
                raise AbfeError(code="abfe_legs_invalid",
                                message=f"estimate_binding_dg takes exactly two analyze_fep parents, got {len(parents)}")
            loaded = {pid: _load_leg(path, pid) for pid, path in files.items()}
            by_leg: dict[str, str] = {}
            for pid, leg in loaded.items():
                by_leg.setdefault(_leg_manifest(leg, pid).get("leg"), pid)
            if set(by_leg) != {LEG_COMPLEX, LEG_SOLVENT}:
                raise AbfeError(code="abfe_legs_invalid",
                                message=f"the parents are {sorted(str(k) for k in by_leg)} leg(s); one complex and one "
                                "solvent leg are needed (the solvent leg comes from extract_ligand)")
            leg_nodes = {LEG_COMPLEX: by_leg[LEG_COMPLEX], LEG_SOLVENT: by_leg[LEG_SOLVENT]}
            complex, solvent = files[by_leg[LEG_COMPLEX]], files[by_leg[LEG_SOLVENT]]
            if study_dir is None:
                study_dir = _study_dir_from_job(job_dir)
        elif not (complex and solvent):
            raise AbfeError(code="fep_result_invalid",
                            message="pass the complex and solvent fep_result.json files (--complex / --solvent), or "
                            "--job-dir/--node-id on a comparison analyze node over the two analyze_fep nodes")
        leg_c, leg_s = _load_leg(complex, LEG_COMPLEX), _load_leg(solvent, LEG_SOLVENT)
        man_c, man_s = _leg_manifest(leg_c, LEG_COMPLEX), _leg_manifest(leg_s, LEG_SOLVENT)
        if man_c.get("leg") != LEG_COMPLEX or man_s.get("leg") != LEG_SOLVENT:
            raise AbfeError(code="abfe_legs_invalid",
                            message=f"--complex is a {man_c.get('leg')!r} leg and --solvent a {man_s.get('leg')!r} leg")
        if not man_c.get("boresch"):
            raise AbfeError(code="abfe_legs_invalid",
                            message="the complex leg carries no Boresch restraint; it must be sampled on the topology "
                            "written by add_boresch_restraint")
        mismatches = []
        for key, a, b in [("ligand", (man_c.get("ligand") or {}).get("residue_name"), (man_s.get("ligand") or {}).get("residue_name")),
                          ("ligand smiles", (man_c.get("ligand") or {}).get("smiles"), (man_s.get("ligand") or {}).get("smiles")),
                          *[(k, man_c.get(k), man_s.get(k)) for k in ("forcefield", "water_model", "hmr", "softcore_alpha")],
                          ("temperature_kelvin", leg_c.get("temperature_kelvin"), leg_s.get("temperature_kelvin")),
                          ("elec_lambdas", man_c["schedules"].get("elec_lambdas"), man_s["schedules"].get("elec_lambdas")),
                          ("sterics_lambdas", man_c["schedules"].get("sterics_lambdas"), man_s["schedules"].get("sterics_lambdas"))]:
            if a is not None and b is not None and a != b:
                mismatches.append(f"{key}: complex={a!r}, solvent={b!r}")
        if mismatches:
            raise AbfeError(code="abfe_legs_incompatible",
                            message="the two legs do not describe one thermodynamic cycle: " + "; ".join(mismatches)
                            + ". Rebuild the solvent leg's build_decoupled_system with the complex leg's options.")
    except (AbfeError, FepAnalysisError) as exc:
        return _fail(exc.code, str(exc))

    if node_mode:
        from mdclaw._node import begin_node

        out_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)

    temperature = float(leg_c.get("temperature_kelvin") or 300.0)
    restraint = BoreschRestraint.from_json(man_c["boresch"])
    dg_restraint = standard_state_restraint_free_energy(restraint, temperature)
    dg_symmetry = -_KB * temperature * math.log(ligand_symmetry_number)
    dg = leg_s["dG_kj_mol"] - leg_c["dG_kj_mol"] + dg_restraint + dg_symmetry
    err = math.sqrt(leg_s.get("dG_error_kj_mol", 0.0) ** 2 + leg_c.get("dG_error_kj_mol", 0.0) ** 2)
    for label, leg in ((LEG_COMPLEX, leg_c), (LEG_SOLVENT, leg_s)):
        result["warnings"].extend(f"[{label}] {w}" for w in leg.get("warnings") or [])

    def _block(path: str, leg: dict, node: Optional[str]) -> dict:
        return {"file": str(Path(path).resolve()), "node_id": node, "dG_kj_mol": leg["dG_kj_mol"],
                "dG_error_kj_mol": leg.get("dG_error_kj_mol"), "phases": leg.get("phases"),
                "n_states": leg.get("n_states"), "n_samples_total": leg.get("n_samples_total"),
                "min_neighbour_overlap": leg.get("min_neighbour_overlap")}

    ligand_name = (man_c.get("ligand") or {}).get("residue_name")
    report = {
        "schema_version": 1, "analysis": BINDING_ANALYSIS, "ligand": man_c.get("ligand"),
        "quantity": "dG_bind (1 M standard state) = dG_solvent - dG_complex + dG_restraint - kT ln(sigma)",
        "sign_convention": "dG_bind < 0: binding is favourable",
        "dG_bind_kj_mol": dg, "dG_bind_error_kj_mol": err,
        "dG_bind_kcal_mol": dg / _KJ_PER_KCAL, "dG_bind_error_kcal_mol": err / _KJ_PER_KCAL,
        "terms_kj_mol": {"solvent_leg": leg_s["dG_kj_mol"], "complex_leg": leg_c["dG_kj_mol"],
                         "restraint_standard_state": dg_restraint, "ligand_symmetry": dg_symmetry},
        "temperature_kelvin": temperature, "ligand_symmetry_number": ligand_symmetry_number,
        "boresch": man_c["boresch"],
        "legs": {LEG_COMPLEX: _block(complex, leg_c, leg_nodes[LEG_COMPLEX]),
                 LEG_SOLVENT: _block(solvent, leg_s, leg_nodes[LEG_SOLVENT])},
        "job_dir": str(Path(job_dir).resolve()) if job_dir else None, "node_id": node_id,
        "warnings": list(result["warnings"]),
    }
    if node_mode:
        out = out_dir / f"{output_name}.json"
    elif output_file:
        out = Path(output_file)
    elif study_dir:
        out = Path(study_dir) / "evidence" / f"binding_dg_{ligand_name}.json"
    else:
        out = WORKING_DIR / f"binding_dg_{ligand_name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    result.update({"success": True, "binding_dg_file": str(out), **{k: v for k, v in report.items() if k != "warnings"}})
    if study_dir:
        try:
            from mdclaw.study import record_study_log

            record_study_log(
                study_dir=study_dir, record_type="decision", phase="analysis",
                decision=(f"estimate_binding_dg {ligand_name}: dG_bind = {dg / _KJ_PER_KCAL:+.2f} +/- "
                          f"{err / _KJ_PER_KCAL:.2f} kcal/mol"),
                reason="double decoupling: solvent leg - complex leg + analytic Boresch restraint term",
                inputs=[str(Path(complex).resolve()), str(Path(solvent).resolve())], outputs=[str(out)],
                metadata={"dG_bind_kj_mol": dg, "dG_bind_error_kj_mol": err, "job_dir": report["job_dir"], "node_id": node_id})
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"study log not updated: {type(exc).__name__}: {exc}")
    if node_mode:
        from mdclaw._node import complete_node

        complete_node(
            job_dir, node_id, artifacts={"binding_dg": f"artifacts/{output_name}.json"},
            metadata={"analysis": BINDING_ANALYSIS, "ligand": ligand_name, "dG_bind_kj_mol": dg,
                      "dG_bind_error_kj_mol": err, "dG_bind_kcal_mol": dg / _KJ_PER_KCAL,
                      "dG_bind_error_kcal_mol": err / _KJ_PER_KCAL, "terms_kj_mol": report["terms_kj_mol"],
                      "ligand_symmetry_number": ligand_symmetry_number,
                      "legs": {role: {"node_id": leg_nodes[role], "dG_kj_mol": leg["dG_kj_mol"],
                                      "dG_error_kj_mol": leg.get("dG_error_kj_mol")}
                               for role, leg in ((LEG_COMPLEX, leg_c), (LEG_SOLVENT, leg_s))}},
            warnings=result["warnings"] or None)
    return result


__all__ = ["ABFE_KIND", "AbfeError", "abfe_windows", "add_boresch_restraint", "build_decoupled_system",
           "estimate_binding_dg", "extract_ligand", "ligand_atoms_in_topology", "select_ligand_record"]
