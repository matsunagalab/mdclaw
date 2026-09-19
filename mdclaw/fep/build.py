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
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from mdclaw._common import create_unique_subdir
from mdclaw._tool_meta import node_tool
from mdclaw.fep.hybrid import (
    DEFAULT_SOFTCORE_ALPHA,
    STATE_A,
    HybridBuildError,
    build_hybrid,
    relax_dummy_atoms,
    validate_endpoints,
)
from mdclaw.fep.mapping import (
    MappingError,
    atom_records_from_topology,
    bonds_from_topology,
    map_mutation,
)
from mdclaw.fep.mutant import MutantBuildError, parse_single_mutation, write_mutant_pdb
from mdclaw.fep.protocol import ProtocolError, build_protocol, windows_from_schedule
from mdclaw.sidechain_packer import PROTEIN_RESNAME_TO_ONE

logger = logging.getLogger(__name__)

WORKING_DIR = Path("outputs").resolve()


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


def hybrid_topology(top_a, top_b, mapping):
    """WT topology with the appearing atoms appended to the mutated residue."""
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
    ``(info, positions_nm)`` so the topology PDB uses the same coordinates."""
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
    return {"potential_energy_kj_mol": energy}, final


def _fail(result: dict, code: str, message: str, *, job_dir, node_id, extra: Optional[dict] = None) -> dict:
    result.update({"success": False, "code": code, "message": message, "error_type": "ValidationError"})
    result.setdefault("errors", []).append(message)
    if extra:
        result.update(extra)
    if job_dir and node_id:
        from mdclaw._node import fail_node_from_result

        return fail_node_from_result(job_dir, node_id, result, default_error=message)
    return result


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
        lambda_schedule: Comma-separated lambdas (``"0,0.1,...,1"``) or a
            JSON list of lambdas / explicit parameter dicts. First window
            must be state A, last state B.
        softcore_alpha: Beutler soft-core alpha for the dummy LJ terms.
        endpoint_tolerance_kj_mol: Allowed |E_hybrid - E_reference| at each
            end state (scaled up automatically for large systems).
        output_name / output_dir / job_dir / node_id: standard mdclaw knobs.

    Returns:
        Dict with ``system_xml`` / ``topology_pdb`` / ``state_xml`` /
        ``hybrid_manifest`` / ``fep_protocol`` paths, the mapping summary
        (``n_core``, ``n_unique_old``, ``n_unique_new``), the end-point
        validation block, and structured ``code`` on failure
        (``fep_mutation_spec_invalid``, ``fep_mutant_model_failed``,
        ``fep_endstate_build_failed``, ``fep_environment_mismatch``,
        ``fep_mapping_failed``, ``fep_unsupported_force``,
        ``fep_endpoint_validation_failed``, ``fep_protocol_invalid``).
    """
    from mdclaw.amber.build_system import _resolve_build_amber_node_inputs, build_amber_system
    from mdclaw.amber.water_utils import resolve_water_and_forcefield

    result: dict = {
        "success": False,
        "tool": "build_hybrid_system",
        "mutation": mutation,
        "errors": [],
        "warnings": [],
    }
    _node_mode = bool(job_dir and node_id)
    solvation_water_model = None
    solvation_node_id = None
    neutralization_expected = False

    # --- protocol first: cheap and purely syntactic ---------------------
    try:
        windows = windows_from_schedule(lambda_schedule, n_windows)
    except ProtocolError as exc:
        return _fail(result, exc.code, str(exc), job_dir=job_dir, node_id=node_id)

    # --- DAG resolution --------------------------------------------------
    if _node_mode:
        try:
            from mdclaw._node import resolve_node_inputs as _peek_inputs

            _peek = _peek_inputs(job_dir, node_id, "topo")
            solvation_water_model = _peek.get("solvation_water_model")
            solvation_node_id = _peek.get("solvation_node_id")
        except Exception:  # noqa: BLE001 - the full resolver reports problems below
            pass
    _ff_water = resolve_water_and_forcefield(
        water_model=water_model, forcefield=forcefield,
        solvation_water_model=solvation_water_model,
        solvation_node_id=solvation_node_id, job_dir=job_dir, node_id=node_id,
    )
    water_model = _ff_water["water_model"]
    forcefield = _ff_water["forcefield"]
    result["warnings"].extend(_ff_water["warnings"])
    if _ff_water["mismatch"]:
        mismatch = _ff_water["mismatch"]
        return _fail(result, code="solvation_topology_water_model_mismatch", message=mismatch["message"],
                     job_dir=job_dir, node_id=node_id, extra={"next_action": mismatch["next_action"]})
    if _node_mode:
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
            return resolved
        pdb_file = resolved["pdb_file"]
        ligand_chemistry = resolved["ligand_chemistry"]
        disulfide_bonds = resolved["disulfide_bonds"]
        box_dimensions = resolved["box_dimensions"]
        is_membrane = resolved["is_membrane"]
        neutralization_expected = resolved["neutralization_expected"]
        if resolved.get("glycan_metadata") or resolved.get("glycan_linkages"):
            return _fail(result, code="fep_unsupported_force", message="glycoproteins are not supported by build_hybrid_system yet",
                         job_dir=job_dir, node_id=node_id)
    if not pdb_file:
        return _fail(result, code="missing_pdb_file", message="pdb_file is required (or --job-dir/--node-id under a completed solv node)",
                     job_dir=job_dir, node_id=node_id)
    wt_pdb = Path(pdb_file).resolve()
    if not wt_pdb.is_file():
        return _fail(result, code="file_not_found", message=f"pdb_file not found: {wt_pdb}", job_dir=job_dir, node_id=node_id)

    # --- output directory ------------------------------------------------
    if _node_mode:
        from mdclaw._node import begin_node

        out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        out_dir = create_unique_subdir(Path(output_dir) if output_dir else WORKING_DIR, "hybrid_topology")
    result["output_dir"] = str(out_dir)

    # --- mutant structure ------------------------------------------------
    try:
        spec = parse_single_mutation(mutation, wt_pdb)
        mutant = write_mutant_pdb(
            wt_pdb, spec, out_dir / "mutant_input.pdb",
            backend=mutant_backend, work_dir=out_dir / "mutant_model",
        )
    except MutantBuildError as exc:
        return _fail(result, exc.code, str(exc), job_dir=job_dir, node_id=node_id)
    result["warnings"].extend(mutant.pop("warnings", []))
    result["mutation"] = spec.to_json()
    result["mutant_model"] = mutant
    shutil.rmtree(out_dir / "mutant_model", ignore_errors=True)

    # --- end-state builds ------------------------------------------------
    endstate_dir = out_dir / "endstates"
    endstate_dir.mkdir(exist_ok=True)
    common = dict(
        box_dimensions=box_dimensions, forcefield=forcefield, water_model=water_model,
        is_membrane=is_membrane, hmr=hmr, ligand_chemistry=ligand_chemistry,
        disulfide_bonds=disulfide_bonds, minimize_max_iterations=10,
    )
    endstates: dict[str, dict] = {}
    for label, pdb in (("wt", wt_pdb), ("mut", Path(mutant["mutant_pdb"]))):
        logger.info("build_hybrid_system: building %s end state from %s", label, pdb)
        built = build_amber_system(pdb_file=str(pdb), output_dir=str(endstate_dir), output_name=label, **common)
        if not built.get("success"):
            return _fail(
                result, code="fep_endstate_build_failed", message=f"{label} end-state build failed ({built.get('code')}): "
                + "; ".join(built.get("errors", [])[:3]),
                job_dir=job_dir, node_id=node_id,
                extra={"endstate": label, "endstate_result": {
                    k: built.get(k) for k in ("code", "errors", "warnings", "output_dir")}},
            )
        endstates[label] = built
        result["warnings"].extend(f"[{label}] {w}" for w in built.get("warnings", []))
    wt_charge = endstates["wt"].get("system_net_charge_e")
    mut_charge = endstates["mut"].get("system_net_charge_e")
    if neutralization_expected and wt_charge is not None and abs(float(wt_charge)) > 1e-3:
        return _fail(result, code="neutralization_charge_mismatch", message=f"the solvation step placed ions, yet the wild-type System has net charge {wt_charge} e",
                     job_dir=job_dir, node_id=node_id)
    if wt_charge is not None and mut_charge is not None and abs(float(mut_charge) - float(wt_charge)) > 1e-3:
        result["warnings"].append(
            f"Charge-changing mutation: system net charge {wt_charge:+.2f} e -> {mut_charge:+.2f} e. "
            f"PME applies a uniform neutralising background; the resulting ddG carries a finite-size "
            f"artefact that largely cancels between folded and unfolded legs but is not corrected here."
        )

    # --- hybrid build ----------------------------------------------------
    import numpy as np
    import openmm
    from openmm import unit
    from openmm.app import PDBFile

    try:
        top_a = PDBFile(endstates["wt"]["topology_pdb"]).topology
        top_b = PDBFile(endstates["mut"]["topology_pdb"]).topology
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
            raise MappingError(code="fep_environment_mismatch", message=f"mutated residue index differs between builds ({res_a} vs {res_b})")
        atoms_a = atom_records_from_topology(top_a)
        atoms_b = atom_records_from_topology(top_b)
        mapping = map_mutation(atoms_a, atoms_b, res_a,
                               old_bonds=bonds_from_topology(top_a), new_bonds=bonds_from_topology(top_b))
        build = build_hybrid(sys_a, pos_a, sys_b, pos_b, mapping, softcore_alpha=softcore_alpha)
        platform = fastest_platform_name()
        relax = relax_dummy_atoms(build, platform_name=platform)
        validation = validate_endpoints(build, sys_a, sys_b, platform_name=platform,
                                        tolerance_kj_mol=endpoint_tolerance_kj_mol)
    except (MappingError, HybridBuildError) as exc:
        return _fail(result, exc.code, str(exc), job_dir=job_dir, node_id=node_id)
    except Exception as exc:  # noqa: BLE001
        return _fail(result, code="fep_hybrid_build_failed", message=f"{type(exc).__name__}: {exc}",
                     job_dir=job_dir, node_id=node_id)

    result["mapping"] = {
        "n_hybrid": mapping.n_hybrid,
        "n_core": len(mapping.core_pairs),
        "n_unique_old": len(mapping.unique_old),
        "n_unique_new": len(mapping.unique_new),
    }
    result["dummy_relaxation"] = relax
    result["endpoint_validation"] = validation
    result["hybrid_report"] = build.report

    # --- artifacts -------------------------------------------------------
    system_xml = out_dir / f"{output_name}.system.xml"
    topology_pdb = out_dir / f"{output_name}.topology.pdb"
    state_xml = out_dir / f"{output_name}.state.xml"
    manifest_file = out_dir / "hybrid_manifest.json"
    protocol_file = out_dir / "fep_protocol.json"
    try:
        top_h = hybrid_topology(top_a, top_b, mapping)
        system_xml.write_text(openmm.XmlSerializer.serialize(build.system))
        state_info, final_positions = _write_hybrid_state(build.system, build.positions_nm, platform, state_xml)
        with topology_pdb.open("w") as fh:
            PDBFile.writeFile(top_h, final_positions * unit.nanometer, fh, keepIds=True)
    except Exception as exc:  # noqa: BLE001
        return _fail(result, code="fep_hybrid_build_failed", message=f"serialization failed: {type(exc).__name__}: {exc}",
                     job_dir=job_dir, node_id=node_id)
    manifest = {
        "schema_version": 1,
        "mutation": spec.to_json(),
        "mutant_model": mutant,
        "mapping": mapping.to_json(),
        "hybrid_report": build.report,
        "dummy_relaxation": relax,
        "endpoint_validation": validation,
        "hybrid_state": state_info,
        "endstates": {
            label: {k: built.get(k) for k in ("system_xml", "topology_pdb", "state_xml", "system_net_charge_e")}
            for label, built in endstates.items()
        },
        "forcefield": forcefield,
        "water_model": water_model if box_dimensions else None,
        "hmr": bool(hmr),
    }
    manifest_file.write_text(json.dumps(manifest, indent=2, default=str))
    protocol = build_protocol(mutation=spec.to_json(), windows=windows, softcore_alpha=softcore_alpha)
    protocol_file.write_text(json.dumps(protocol, indent=2))
    # Topo nodes must carry the parameter / provenance envelope; the wild-type
    # build's metadata is the physical system, annotated with the FEP layer.
    wt_meta = endstates["wt"]
    amber_metadata = {
        "success": True,
        "tool": "build_hybrid_system",
        "solvent_type": wt_meta.get("solvent_type"),
        "parameters": {
            **(wt_meta.get("parameters") or {}),
            "mutation": spec.to_json(),
            "mutant_backend": mutant["backend"],
            "softcore_alpha": float(softcore_alpha),
            "n_windows": len(windows),
        },
        "forcefield_provenance": wt_meta.get("forcefield_provenance"),
        "statistics": {"num_atoms": mapping.n_hybrid, "num_residues": sum(1 for _ in top_h.residues())},
        "mapping": result["mapping"],
        "endpoint_validation": validation,
        "endstates": manifest["endstates"],
        "warnings": result["warnings"],
    }
    (out_dir / "amber_metadata.json").write_text(json.dumps(amber_metadata, indent=2, default=str))
    result.update({
        "system_xml": str(system_xml), "topology_pdb": str(topology_pdb), "state_xml": str(state_xml),
        "hybrid_manifest": str(manifest_file), "fep_protocol": str(protocol_file),
        "n_windows": len(windows),
        "statistics": {"num_atoms": mapping.n_hybrid, "num_residues": sum(1 for _ in top_h.residues())},
    })

    if not validation.get("passed") or not validation.get("all_finite"):
        return _fail(
            result, code="fep_endpoint_validation_failed", message="hybrid end-state energies do not reproduce the wild-type / mutant Systems "
            f"(dA={validation['state_a']['difference_kj_mol']:.3g}, "
            f"dB={validation['state_b']['difference_kj_mol']:.3g} kJ/mol, tol={validation['tolerance_kj_mol']:.3g})",
            job_dir=job_dir, node_id=node_id,
        )

    result["success"] = True
    solvent_type = "explicit" if box_dimensions else "vacuum"
    if _node_mode:
        from mdclaw._node import complete_node, update_job_summaries

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
                "forcefield": forcefield,
                "effective_forcefield": wt_meta.get("parameters", {}).get("effective_forcefield", forcefield),
                "water_model": water_model if solvent_type == "explicit" else None,
                "solvent_type": solvent_type,
                "implicit_solvent": None,
                "hmr": bool(hmr),
                "is_membrane": bool(is_membrane),
                "system_artifact_kind": "openmm_system_xml",
                "forcefield_provenance": wt_meta.get("forcefield_provenance"),
                "fep": {
                    "mutation": spec.to_json(),
                    "mutant_backend": mutant["backend"],
                    "n_windows": len(windows),
                    "softcore_alpha": float(softcore_alpha),
                    "n_unique_old": len(mapping.unique_old),
                    "n_unique_new": len(mapping.unique_new),
                    "endpoint_validation_passed": True,
                },
            },
            warnings=result["warnings"],
        )
        update_job_summaries(job_dir, params={
            "forcefield": forcefield, "solvation_type": solvent_type,
            "water_model": water_model if solvent_type == "explicit" else None,
            "fep_mutation": spec.label,
        })
    return result


__all__ = ["build_hybrid_system", "fastest_platform_name", "hybrid_topology", "locate_residue_index"]
