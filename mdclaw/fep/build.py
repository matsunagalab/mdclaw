"""``build_hybrid_system`` — topo-stage tool that emits a hybrid XML triple.

Pipeline (all inside one ``topo`` node)::

    solvated.pdb ──► build_amber_system (wild type)  ──┐
        │                                              ├─► map ─► hybrid System
        └─► mutant.pdb ─► build_amber_system (mutant) ─┘        + hybrid topology
                                                                 + hybrid state

The node completes with the standard ``system.xml`` / ``topology.pdb`` /
``state.xml`` triple (global parameters default to state A, so ``min`` and
``eq`` treat it as an ordinary wild-type system) plus ``hybrid_manifest``
and ``fep_protocol`` artifacts consumed by ``run_fep`` / ``analyze_fep``.

``hybrid_manifest.json`` is the single record of what was built (mapping,
end-state files, dummy relaxation, end-point validation). The tool result
and the node metadata carry only the summary an agent needs to decide.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from mdclaw._common import create_unique_subdir
from mdclaw._tool_meta import node_tool
from mdclaw.fep.hybrid import (
    DEFAULT_SOFTCORE_ALPHA,
    STATE_A,
    HybridBuild,
    HybridBuildError,
    build_hybrid,
    relax_dummy_atoms,
    validate_endpoints,
)
from mdclaw.fep.mapping import (
    HybridMapping,
    MappingError,
    atom_records_from_topology,
    bonds_from_topology,
    map_mutation,
)
from mdclaw.fep.mutant import MutantBuildError, MutationSpec, parse_single_mutation, write_mutant_pdb
from mdclaw.fep.protocol import ProtocolError, build_protocol, windows_from_schedule
from mdclaw.sidechain_packer import PROTEIN_RESNAME_TO_ONE

logger = logging.getLogger(__name__)

WORKING_DIR = Path("outputs").resolve()


class BuildStepError(RuntimeError):
    """A build step failed with a structured ``code``; ``extra`` is merged
    into the tool result."""

    def __init__(self, code: str, message: str, extra: Optional[dict] = None, *, recorded: bool = False):
        super().__init__(message)
        self.code = code
        self.extra = extra or {}
        self.recorded = recorded  # the node failure was already written by a nested tool


def fastest_platform_name() -> str:
    import openmm

    available = {openmm.Platform.getPlatform(i).getName() for i in range(openmm.Platform.getNumPlatforms())}
    for name in ("CUDA", "OpenCL", "CPU", "Reference"):
        if name in available:
            return name
    return "Reference"  # pragma: no cover


def locate_residue_index(topology, chain_id: str, residue_id: str) -> int:
    """Index of the *protein* residue ``chain_id:residue_id`` (solvent may reuse ids)."""
    matches = [
        r.index for r in topology.residues()
        if (str(r.chain.id).strip() or "") == chain_id
        and str(r.id).strip() + (getattr(r, "insertionCode", "") or "").strip() == residue_id
        and r.name in PROTEIN_RESNAME_TO_ONE
    ]
    if len(matches) != 1:
        raise MappingError(
            code="fep_mutation_residue_not_found" if not matches else "fep_mutation_residue_ambiguous",
            message=f"protein residue {chain_id}:{residue_id} matched {len(matches)} residues in the built topology",
        )
    return matches[0]


def hybrid_topology(top_a, top_b, mapping: HybridMapping):
    """WT topology with the appearing atoms appended to the mutated residue.

    Residue names are copied from ``top_a``; the caller restores the
    end-state ``topology.pdb`` names on it first (``PDBFile`` normalises
    HIE/CYX/ASH/... on load), so the hybrid ``topology.pdb`` carries the same
    protonation-state names as every other topo node's.

    Known limitation: the residue keeps its wild-type name, and
    ``PDBFile.writeFile`` emits no CONECT records for standard residue names,
    so the appended atoms are unbonded when the PDB is re-read by tools that
    infer bonds from templates. The physics lives in ``system.xml``; this only
    affects imaging / visualisation of the hybrid.
    """
    from openmm.app import Topology

    atoms_b = list(top_b.atoms())
    new_h_to_b = {mapping.new_to_hybrid[b]: b for b in mapping.unique_new}
    new_by_hybrid = dict(sorted(new_h_to_b.items()))
    out = Topology()
    out.setPeriodicBoxVectors(top_a.getPeriodicBoxVectors())
    h_atoms: dict[int, Any] = {}
    for chain in top_a.chains():
        oc = out.addChain(chain.id)
        for residue in chain.residues():
            orr = out.addResidue(residue.name, oc, residue.id, getattr(residue, "insertionCode", ""))
            for atom in residue.atoms():
                h = mapping.old_to_hybrid[atom.index]
                h_atoms[h] = out.addAtom(atom.name, atom.element, orr, atom.id)
            if residue.index == mapping.residue_index:
                for h, b in new_by_hybrid.items():
                    src = atoms_b[b]
                    name = mapping.hybrid_new_pdb_names.get(b, src.name)
                    h_atoms[h] = out.addAtom(name, src.element, orr)
    for bond in top_a.bonds():
        out.addBond(h_atoms[mapping.old_to_hybrid[bond.atom1.index]],
                    h_atoms[mapping.old_to_hybrid[bond.atom2.index]], bond.type, bond.order)
    unique_new = set(mapping.unique_new)
    for bond in top_b.bonds():
        i, j = bond.atom1.index, bond.atom2.index
        if i in unique_new or j in unique_new:
            out.addBond(h_atoms[mapping.new_to_hybrid[i]], h_atoms[mapping.new_to_hybrid[j]],
                        bond.type, bond.order)
    return out


def _write_hybrid_state(system, positions_nm, platform_name: str, path: Path):
    """Serialize the state-A ``State`` (virtual sites recomputed) and return
    ``(potential_energy_kj_mol, positions_nm)`` so the topology PDB uses the
    same coordinates."""
    import numpy as np
    import openmm
    from openmm import unit

    integrator = openmm.VerletIntegrator(0.001 * unit.picoseconds)
    context = openmm.Context(system, integrator, openmm.Platform.getPlatformByName(platform_name))
    context.setPositions(positions_nm * unit.nanometer)
    for name, value in STATE_A.items():
        context.setParameter(name, value)
    context.computeVirtualSites()
    state = context.getState(getPositions=True, getVelocities=True, getEnergy=True, enforcePeriodicBox=False)
    path.write_text(openmm.XmlSerializer.serialize(state))
    energy = float(state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole))
    final = np.asarray(state.getPositions(asNumpy=True).value_in_unit(unit.nanometer))
    del context, integrator
    return energy, final


# --------------------------------------------------------------------------- #
# Build steps                                                                   #
# --------------------------------------------------------------------------- #

@dataclass
class _Inputs:
    pdb_file: Path
    forcefield: Optional[str]
    water_model: Optional[str]
    box_dimensions: Optional[dict]
    is_membrane: Optional[bool]
    ligand_chemistry: Optional[list]
    disulfide_bonds: Optional[list]
    neutralization_expected: bool = False
    warnings: list[str] = field(default_factory=list)


def _resolve_inputs(
    *, job_dir, node_id, mutation, pdb_file, forcefield, water_model, hmr, is_membrane,
    ligand_chemistry, disulfide_bonds, box_dimensions, mutant_backend, n_windows, lambda_schedule,
    softcore_alpha, output_name,
) -> _Inputs:
    """Force field / water model from the DAG (or the explicit arguments) and,
    in node mode, the solvated PDB plus solvation metadata."""
    from mdclaw.amber.build_system import _resolve_build_amber_node_inputs
    from mdclaw.amber.water_utils import resolve_water_and_forcefield

    node_mode = bool(job_dir and node_id)
    solvation_water_model = solvation_node_id = None
    if node_mode:
        try:
            from mdclaw._node import resolve_node_inputs as _peek_inputs

            peek = _peek_inputs(job_dir, node_id, "topo")
            solvation_water_model = peek.get("solvation_water_model")
            solvation_node_id = peek.get("solvation_node_id")
        except Exception:  # noqa: BLE001 - the full resolver reports problems below
            pass
    ff_water = resolve_water_and_forcefield(
        water_model=water_model, forcefield=forcefield,
        solvation_water_model=solvation_water_model,
        solvation_node_id=solvation_node_id, job_dir=job_dir, node_id=node_id,
    )
    warnings = list(ff_water["warnings"])
    if ff_water["mismatch"]:
        mismatch = ff_water["mismatch"]
        raise BuildStepError(code="solvation_topology_water_model_mismatch", message=mismatch["message"],
                             extra={"next_action": mismatch["next_action"]})
    water_model, forcefield = ff_water["water_model"], ff_water["forcefield"]
    neutralization_expected = False
    if node_mode:
        resolved = _resolve_build_amber_node_inputs(
            job_dir=job_dir, node_id=node_id,
            actual_conditions={
                "mutation": mutation, "forcefield": forcefield, "water_model": water_model,
                "hmr": hmr, "is_membrane": is_membrane, "mutant_backend": mutant_backend,
                "n_windows": n_windows, "lambda_schedule": lambda_schedule,
                "softcore_alpha": softcore_alpha, "output_name": output_name,
            },
            pdb_file=pdb_file, ligand_chemistry=ligand_chemistry, modxna_params=None,
            disulfide_bonds=disulfide_bonds, glycan_metadata=None, glycan_linkages=None,
            box_dimensions=box_dimensions, is_membrane=is_membrane,
        )
        if not resolved["success"]:
            # _resolve_build_amber_node_inputs has already recorded the failure.
            raise BuildStepError(resolved.get("code") or "input_resolution_blocked",
                                 resolved.get("message") or "; ".join(resolved.get("errors") or []) or "input resolution failed",
                                 {k: v for k, v in resolved.items() if k != "success"}, recorded=True)
        if resolved.get("glycan_metadata") or resolved.get("glycan_linkages"):
            raise BuildStepError(code="fep_unsupported_force", message="glycoproteins are not supported by build_hybrid_system yet")
        pdb_file = resolved["pdb_file"]
        ligand_chemistry = resolved["ligand_chemistry"]
        disulfide_bonds = resolved["disulfide_bonds"]
        box_dimensions = resolved["box_dimensions"]
        is_membrane = resolved["is_membrane"]
        neutralization_expected = resolved["neutralization_expected"]
    if not pdb_file:
        raise BuildStepError(code="missing_pdb_file", message="pdb_file is required (or --job-dir/--node-id under a completed solv node)")
    wt_pdb = Path(pdb_file).resolve()
    if not wt_pdb.is_file():
        raise BuildStepError(code="file_not_found", message=f"pdb_file not found: {wt_pdb}")
    return _Inputs(
        pdb_file=wt_pdb, forcefield=forcefield, water_model=water_model, box_dimensions=box_dimensions,
        is_membrane=is_membrane, ligand_chemistry=ligand_chemistry, disulfide_bonds=disulfide_bonds,
        neutralization_expected=neutralization_expected, warnings=warnings,
    )


def _build_endstates(inputs: _Inputs, spec: MutationSpec, mutant_pdb: Path, out_dir: Path, *, hmr: bool) -> dict:
    """``build_amber_system`` for the wild type and the mutant; returns
    ``{"wt": result, "mut": result, "warnings": [...]}``."""
    from mdclaw.amber.build_system import build_amber_system

    endstate_dir = out_dir / "endstates"
    endstate_dir.mkdir(exist_ok=True)
    common = dict(
        box_dimensions=inputs.box_dimensions, forcefield=inputs.forcefield, water_model=inputs.water_model,
        is_membrane=inputs.is_membrane, hmr=hmr, ligand_chemistry=inputs.ligand_chemistry,
        disulfide_bonds=inputs.disulfide_bonds, minimize_max_iterations=10,
    )
    out: dict[str, Any] = {"warnings": []}
    for label, pdb in (("wt", inputs.pdb_file), ("mut", mutant_pdb)):
        logger.info("build_hybrid_system: building %s end state from %s", label, pdb)
        built = build_amber_system(pdb_file=str(pdb), output_dir=str(endstate_dir), output_name=label, **common)
        if not built.get("success"):
            raise BuildStepError(
                code="fep_endstate_build_failed",
                message=f"{label} end-state build failed ({built.get('code')}): " + "; ".join(built.get("errors", [])[:3]),
                extra={"endstate": label, "endstate_result": {k: built.get(k) for k in ("code", "errors", "warnings", "output_dir")}},
            )
        out[label] = built
        out["warnings"].extend(f"[{label}] {w}" for w in built.get("warnings", []))
    wt_charge, mut_charge = out["wt"].get("system_net_charge_e"), out["mut"].get("system_net_charge_e")
    if inputs.neutralization_expected and wt_charge is not None and abs(float(wt_charge)) > 1e-3:
        raise BuildStepError(code="neutralization_charge_mismatch",
                             message=f"the solvation step placed ions, yet the wild-type System has net charge {wt_charge} e")
    if wt_charge is not None and mut_charge is not None and abs(float(mut_charge) - float(wt_charge)) > 1e-3:
        out["warnings"].append(
            f"Charge-changing mutation: system net charge {wt_charge:+.2f} e -> {mut_charge:+.2f} e. "
            "PME applies a uniform neutralising background; the resulting ddG carries a finite-size "
            "artefact that largely cancels between folded and unfolded legs but is not corrected here.")
    return out


@dataclass
class _Assembled:
    build: HybridBuild
    top_a: Any
    top_b: Any
    sys_a: Any
    sys_b: Any
    platform: str
    relaxation: dict
    validation: dict


def _assemble_hybrid(endstates: dict, spec: MutationSpec, *, softcore_alpha: float,
                     endpoint_tolerance_kj_mol: float) -> _Assembled:
    """Load both end states, map the mutated residue, build / relax / validate
    the hybrid System."""
    import numpy as np
    import openmm
    from openmm import unit
    from openmm.app import PDBFile

    from mdclaw.structure.pdb_utils import restore_topology_resnames_from_pdb

    try:
        top_a = PDBFile(endstates["wt"]["topology_pdb"]).topology
        top_b = PDBFile(endstates["mut"]["topology_pdb"]).topology
        # PDBFile normalised HIE/CYX/ASH/GLH/LYN/WAT on load; the hybrid
        # Topology copies residue names from these objects, so put the
        # end-state topology.pdb names back first (atom order, exact).
        for top, built in ((top_a, endstates["wt"]), (top_b, endstates["mut"])):
            if restore_topology_resnames_from_pdb(top, built["topology_pdb"]) is None:
                raise MappingError(code="fep_environment_mismatch",
                                   message=f"{built['topology_pdb']} does not match the Topology loaded from it")
        sys_a = openmm.XmlSerializer.deserialize(Path(endstates["wt"]["system_xml"]).read_text())
        sys_b = openmm.XmlSerializer.deserialize(Path(endstates["mut"]["system_xml"]).read_text())
        state_a = openmm.XmlSerializer.deserialize(Path(endstates["wt"]["state_xml"]).read_text())
        state_b = openmm.XmlSerializer.deserialize(Path(endstates["mut"]["state_xml"]).read_text())
        pos_a = np.asarray(state_a.getPositions(asNumpy=True).value_in_unit(unit.nanometer))
        pos_b = np.asarray(state_b.getPositions(asNumpy=True).value_in_unit(unit.nanometer))
        sys_a.setDefaultPeriodicBoxVectors(*state_a.getPeriodicBoxVectors())
        sys_b.setDefaultPeriodicBoxVectors(*state_b.getPeriodicBoxVectors())

        res_a = locate_residue_index(top_a, spec.chain_id, spec.residue_id)
        res_b = locate_residue_index(top_b, spec.chain_id, spec.residue_id)
        if res_a != res_b:
            raise MappingError(code="fep_environment_mismatch",
                               message=f"mutated residue index differs between builds ({res_a} vs {res_b})")
        mapping = map_mutation(atom_records_from_topology(top_a), atom_records_from_topology(top_b), res_a,
                               old_bonds=bonds_from_topology(top_a), new_bonds=bonds_from_topology(top_b))
        build = build_hybrid(sys_a, pos_a, sys_b, pos_b, mapping, softcore_alpha=softcore_alpha)
        platform = fastest_platform_name()
        relaxation = relax_dummy_atoms(build, platform_name=platform)
        validation = validate_endpoints(build, sys_a, sys_b, platform_name=platform,
                                        tolerance_kj_mol=endpoint_tolerance_kj_mol)
    except (MappingError, HybridBuildError) as exc:
        raise BuildStepError(exc.code, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise BuildStepError(code="fep_hybrid_build_failed", message=f"{type(exc).__name__}: {exc}") from exc
    return _Assembled(build=build, top_a=top_a, top_b=top_b, sys_a=sys_a, sys_b=sys_b, platform=platform,
                      relaxation=relaxation, validation=validation)


def _write_artifacts(
    asm: _Assembled, endstates: dict, spec: MutationSpec, mutant: dict, windows: list[dict], inputs: _Inputs,
    out_dir: Path, *, output_name: str, softcore_alpha: float, hmr: bool, warnings: list[str],
) -> dict:
    """XML triple + hybrid manifest + protocol + amber_metadata; returns the
    artifact paths and the manifest."""
    import openmm
    from openmm import unit
    from openmm.app import PDBFile

    files = {
        "system_xml": out_dir / f"{output_name}.system.xml",
        "topology_pdb": out_dir / f"{output_name}.topology.pdb",
        "state_xml": out_dir / f"{output_name}.state.xml",
        "hybrid_manifest": out_dir / "hybrid_manifest.json",
        "fep_protocol": out_dir / "fep_protocol.json",
        "amber_metadata": out_dir / "amber_metadata.json",
    }
    try:
        top_h = hybrid_topology(asm.top_a, asm.top_b, asm.build.mapping)
        files["system_xml"].write_text(openmm.XmlSerializer.serialize(asm.build.system))
        energy, final_positions = _write_hybrid_state(asm.build.system, asm.build.positions_nm, asm.platform,
                                                      files["state_xml"])
        with files["topology_pdb"].open("w") as fh:
            PDBFile.writeFile(top_h, final_positions * unit.nanometer, fh, keepIds=True)
    except Exception as exc:  # noqa: BLE001
        raise BuildStepError(code="fep_hybrid_build_failed", message=f"serialization failed: {type(exc).__name__}: {exc}") from exc

    mapping = asm.build.mapping
    n_residues = sum(1 for _ in top_h.residues())
    manifest = {
        "schema_version": 1,
        "mutation": spec.to_json(),
        "mutant_model": mutant,
        "mapping": mapping.to_json(),
        "mapping_summary": {"n_hybrid": mapping.n_hybrid, "n_core": len(mapping.core_pairs),
                            "n_unique_old": len(mapping.unique_old), "n_unique_new": len(mapping.unique_new)},
        "hybrid_report": asm.build.report,
        "dummy_relaxation": asm.relaxation,
        "endpoint_validation": asm.validation,
        "hybrid_state": {"potential_energy_kj_mol": energy},
        "endstates": {
            label: {k: endstates[label].get(k) for k in ("system_xml", "topology_pdb", "state_xml", "system_net_charge_e")}
            for label in ("wt", "mut")
        },
        "forcefield": inputs.forcefield,
        "water_model": inputs.water_model if inputs.box_dimensions else None,
        "hmr": bool(hmr),
        "softcore_alpha": float(softcore_alpha),
        "n_windows": len(windows),
        "statistics": {"num_atoms": mapping.n_hybrid, "num_residues": n_residues},
    }
    files["hybrid_manifest"].write_text(json.dumps(manifest, indent=2, default=str))
    files["fep_protocol"].write_text(json.dumps(
        build_protocol(mutation=spec.to_json(), windows=windows, softcore_alpha=softcore_alpha), indent=2))
    # Topo nodes must carry the parameter / provenance envelope; the wild-type
    # build's metadata is the physical system, annotated with the FEP layer.
    wt_meta = endstates["wt"]
    files["amber_metadata"].write_text(json.dumps({
        "success": True,
        "tool": "build_hybrid_system",
        "solvent_type": wt_meta.get("solvent_type"),
        "parameters": {**(wt_meta.get("parameters") or {}), "mutation": spec.to_json(),
                       "mutant_backend": mutant["backend"], "softcore_alpha": float(softcore_alpha),
                       "n_windows": len(windows)},
        "forcefield_provenance": wt_meta.get("forcefield_provenance"),
        "statistics": manifest["statistics"],
        "hybrid_manifest": "artifacts/hybrid_manifest.json",
        "warnings": warnings,
    }, indent=2, default=str))
    return {"files": files, "manifest": manifest}


# --------------------------------------------------------------------------- #
# Tool                                                                          #
# --------------------------------------------------------------------------- #

@node_tool(node_type="topo")
def build_hybrid_system(
    mutation: str,
    pdb_file: Optional[str] = None,
    box_dimensions: Optional[Dict[str, float]] = None,
    forcefield: Optional[str] = None,
    water_model: Optional[str] = None,
    hmr: bool = True,
    is_membrane: Optional[bool] = None,
    ligand_chemistry: Optional[List[Dict[str, Any]]] = None,
    disulfide_bonds: Optional[List[Dict[str, Any]]] = None,
    mutant_backend: str = "auto",
    n_windows: int = 21,
    lambda_schedule: Optional[str] = None,
    softcore_alpha: float = DEFAULT_SOFTCORE_ALPHA,
    endpoint_tolerance_kj_mol: float = 1.0,
    output_name: str = "system",
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Build a single-point-mutation hybrid topology as a ``topo`` node.

    Runs ``build_amber_system`` twice (wild type and mutant) on the solvated
    structure and merges the two Systems into one alchemical System whose
    five global parameters (``fep_elec_old``, ``fep_sterics_old``,
    ``fep_core``, ``fep_sterics_new``, ``fep_elec_new``) default to the wild
    type. Downstream ``min`` / ``eq`` need no FEP awareness; ``fep`` nodes
    (``run_fep``) sample the windows recorded in ``fep_protocol.json``.

    Args:
        mutation: One point mutation, ``L99A`` or chain-qualified ``A:L99A``
            (one-letter codes, PDB residue number of the *prepared* file).
        pdb_file: Solvated PDB; auto-resolved from the ``solv`` parent in
            node mode.
        box_dimensions / forcefield / water_model / hmr / is_membrane /
            ligand_chemistry / disulfide_bonds: as ``build_amber_system``;
            resolved from the DAG in node mode. Implicit solvent is not
            supported (the hybrid needs explicit or vacuum electrostatics).
        mutant_backend: ``"auto"`` (HPacker, PDBFixer fallback),
            ``"hpacker"`` or ``"pdbfixer"`` for modelling the new side chain.
        n_windows: Evenly spaced windows when ``lambda_schedule`` is omitted
            (default 21: 6 decharge + 11 steric swap + 6 recharge points).
        lambda_schedule: Strictly increasing lambdas from 0 to 1, as
            ``"0,0.1,...,1"`` or a JSON list of numbers.
        softcore_alpha: Beutler soft-core alpha for the dummy LJ terms.
        endpoint_tolerance_kj_mol: Allowed |E_hybrid - E_reference| at each
            end state (scaled up automatically for large systems).
        output_name / output_dir / job_dir / node_id: standard mdclaw knobs.

    Returns:
        Dict with ``system_xml`` / ``topology_pdb`` / ``state_xml`` /
        ``hybrid_manifest`` / ``fep_protocol`` paths, the ``mapping``
        summary (``n_core``, ``n_unique_old``, ``n_unique_new``), the
        ``endpoint_validation`` block, and structured ``code`` on failure
        (``fep_mutation_spec_invalid``, ``fep_mutant_model_failed``,
        ``fep_endstate_build_failed``, ``fep_environment_mismatch``,
        ``fep_mapping_failed``, ``fep_unsupported_force``,
        ``fep_endpoint_validation_failed``, ``fep_protocol_invalid``).
        Everything else (full mapping, end-state files, relaxation energies)
        is in ``hybrid_manifest.json``.
    """
    from mdclaw._node import fail_tool

    result: dict = {"success": False, "tool": "build_hybrid_system", "mutation": mutation,
                    "errors": [], "warnings": []}
    node_mode = bool(job_dir and node_id)

    def _fail(exc: BuildStepError) -> dict:
        if exc.recorded:
            return {**result, **exc.extra, "success": False, "code": exc.code}
        return fail_tool(result, exc.code, str(exc), job_dir=job_dir, node_id=node_id, extra=exc.extra)

    # --- cheap, purely syntactic checks; the node stays pending on failure --
    try:
        windows = windows_from_schedule(lambda_schedule, n_windows)
    except ProtocolError as exc:
        return fail_tool(result, exc.code, str(exc), job_dir=job_dir, node_id=node_id)
    try:
        inputs = _resolve_inputs(
            job_dir=job_dir, node_id=node_id, mutation=mutation, pdb_file=pdb_file, forcefield=forcefield,
            water_model=water_model, hmr=hmr, is_membrane=is_membrane, ligand_chemistry=ligand_chemistry,
            disulfide_bonds=disulfide_bonds, box_dimensions=box_dimensions, mutant_backend=mutant_backend,
            n_windows=n_windows, lambda_schedule=lambda_schedule, softcore_alpha=softcore_alpha,
            output_name=output_name)
        spec = parse_single_mutation(mutation, inputs.pdb_file)
    except BuildStepError as exc:
        return _fail(exc)
    except MutantBuildError as exc:
        return fail_tool(result, exc.code, str(exc), job_dir=job_dir, node_id=node_id)
    result["warnings"].extend(inputs.warnings)
    result["mutation_spec"] = spec.to_json()

    # --- the node starts here -------------------------------------------
    if node_mode:
        from mdclaw._node import begin_node

        out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        out_dir = create_unique_subdir(Path(output_dir) if output_dir else WORKING_DIR, "hybrid_topology")
    result["output_dir"] = str(out_dir)

    try:
        # mutant structure (HPacker / PDBFixer side chain spliced into the WT
        # frame); the modelling work dir is kept for inspection on failure.
        try:
            mutant = write_mutant_pdb(inputs.pdb_file, spec, out_dir / "mutant_input.pdb",
                                      backend=mutant_backend, work_dir=out_dir / "mutant_model")
        except MutantBuildError as exc:
            raise BuildStepError(exc.code, str(exc)) from exc
        result["warnings"].extend(mutant.pop("warnings", []))
        result["mutant_backend"] = mutant["backend"]

        endstates = _build_endstates(inputs, spec, Path(mutant["mutant_pdb"]), out_dir, hmr=hmr)
        result["warnings"].extend(endstates.pop("warnings"))
        asm = _assemble_hybrid(endstates, spec, softcore_alpha=softcore_alpha,
                               endpoint_tolerance_kj_mol=endpoint_tolerance_kj_mol)
        result["warnings"].extend(asm.build.report.get("warnings") or [])
        written = _write_artifacts(asm, endstates, spec, mutant, windows, inputs, out_dir, output_name=output_name,
                                   softcore_alpha=softcore_alpha, hmr=hmr, warnings=result["warnings"])
    except BuildStepError as exc:
        return _fail(exc)

    manifest = written["manifest"]
    validation = asm.validation
    result.update({
        **{key: str(path) for key, path in written["files"].items() if key != "amber_metadata"},
        "mapping": manifest["mapping_summary"],
        "endpoint_validation": validation,
        "n_windows": len(windows),
        "statistics": manifest["statistics"],
    })
    if not validation.get("passed") or not validation.get("all_finite"):
        return _fail(BuildStepError(
            code="fep_endpoint_validation_failed",
            message="hybrid end-state energies do not reproduce the wild-type / mutant Systems "
            f"(dA={validation['state_a']['difference_kj_mol']:.3g}, "
            f"dB={validation['state_b']['difference_kj_mol']:.3g} kJ/mol, tol={validation['tolerance_kj_mol']:.3g}); "
            "see hybrid_manifest.json"))

    result["success"] = True
    solvent_type = "explicit" if inputs.box_dimensions else "vacuum"
    if node_mode:
        from mdclaw._node import complete_node, update_job_summaries

        wt_meta = endstates["wt"]
        complete_node(
            job_dir, node_id,
            artifacts={
                "system_xml": f"artifacts/{output_name}.system.xml",
                "topology_pdb": f"artifacts/{output_name}.topology.pdb",
                "state_xml": f"artifacts/{output_name}.state.xml",
                "hybrid_manifest": "artifacts/hybrid_manifest.json",
                "fep_protocol": "artifacts/fep_protocol.json",
                "mutant_input_pdb": "artifacts/mutant_input.pdb",
                "amber_metadata": "artifacts/amber_metadata.json",
            },
            metadata={
                "tool": "build_hybrid_system",
                "forcefield": inputs.forcefield,
                "effective_forcefield": (wt_meta.get("parameters") or {}).get("effective_forcefield", inputs.forcefield),
                "water_model": inputs.water_model if solvent_type == "explicit" else None,
                "solvent_type": solvent_type,
                "implicit_solvent": None,
                "hmr": bool(hmr),
                "is_membrane": bool(inputs.is_membrane),
                "system_artifact_kind": "openmm_system_xml",
                "forcefield_provenance": wt_meta.get("forcefield_provenance"),
                "fep": {"mutation": spec.label, "n_windows": len(windows),
                        "endpoint_validation_passed": True},
            },
            warnings=result["warnings"],
        )
        update_job_summaries(job_dir, params={
            "forcefield": inputs.forcefield, "solvation_type": solvent_type,
            "water_model": inputs.water_model if solvent_type == "explicit" else None,
            "fep_mutation": spec.label,
        })
    return result


__all__ = ["BuildStepError", "build_hybrid_system", "fastest_platform_name", "hybrid_topology", "locate_residue_index"]
