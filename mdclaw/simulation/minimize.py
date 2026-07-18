"""
MD Simulation Server - Molecular dynamics simulation & analysis tools.

Provides MCP tools for:
- OpenMM MD simulation (NVT/NPT equilibration, production)
- MDTraj trajectory analysis (RMSD, RMSF, distances, hydrogen bonds, etc.)
- Energy analysis
- Secondary structure analysis
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402
from mdclaw._tool_meta import node_tool  # noqa: E402

logger = setup_logger(__name__)

import json  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Dict, Optional  # noqa: E402

import numpy as np  # noqa: E402
from mdclaw._common import (  # noqa: E402
    create_validation_error,
    ensure_directory,
    generate_job_id,
)

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

from mdclaw.simulation._base import _check_topology_implicit_solvent_match, _fail_node_if_running, _node_artifact_path, _resolve_implicit_solvent_model  # noqa: E402
from mdclaw.simulation.restraints import RESTRAINT_SELECTIONS, select_restraint_atoms  # noqa: E402
from mdclaw.simulation.restart import _save_state_atomic  # noqa: E402
from mdclaw.simulation.xml_contract import _ModernSystemContractError, _deserialize_xml_system, _load_xml_topology_inputs, _system_signature, _validate_xml_system_contract  # noqa: E402


@node_tool(node_type="min")
def run_minimization(
    system_xml_file: Optional[str] = None,
    topology_pdb_file: Optional[str] = None,
    state_xml_file: Optional[str] = None,
    max_iterations: int = 5000,
    restraint_atoms: str = "solute_heavy",
    restraint_force_constant: float = 100.0,
    name: Optional[str] = None,
    output_dir: Optional[str] = None,
    is_membrane: bool = False,
    implicit_solvent: Optional[str] = None,
    platform: str = "auto",
    device_index: Optional[str] = None,
    hmr: bool = True,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a standalone topology-level energy minimization DAG node.

    This tool consumes the same OpenMM XML topology triple as equilibration:
    ``system.xml`` + ``topology.pdb`` + ``state.xml`` from a ``topo`` ancestor.
    It writes a portable minimized ``state`` artifact and a PDB view named
    ``minimized_structure.pdb``. Downstream ``eq`` nodes parented to this node
    auto-resolve the minimized state and therefore do not embed minimization in
    the equilibration node.
    """
    if max_iterations < 0:
        return create_validation_error(
            "max_iterations",
            "max_iterations must be non-negative",
            expected="integer >= 0",
            actual=max_iterations,
            code="minimization_iterations_invalid",
        )

    _node_mode = bool(job_dir and node_id)
    _chain_identity_map_file = None
    if _node_mode:
        from mdclaw._node import (
            begin_node, fail_node,
            resolve_node_inputs, validate_node_execution_context,
        )
        _inputs = resolve_node_inputs(job_dir, node_id, "min")
        if "input_resolution_error" in _inputs:
            err = _inputs["input_resolution_error"]
            begin_node(job_dir, node_id)
            fail_node(job_dir, node_id, errors=[err])
            return create_validation_error(
                "job_dir/node_id",
                err,
                expected=(
                    "Completed topo ancestor with system.xml + topology.pdb "
                    "+ state.xml triple"
                ),
                actual=f"job_dir={job_dir}, node_id={node_id}",
                context_extra={
                    "input_resolution_errors": _inputs.get(
                        "input_resolution_errors", []
                    ),
                },
                code="input_resolution_blocked",
            )
        if not system_xml_file and "system_xml_file" in _inputs:
            system_xml_file = _inputs["system_xml_file"]
        if not topology_pdb_file and "topology_pdb_file" in _inputs:
            topology_pdb_file = _inputs["topology_pdb_file"]
        if not state_xml_file and "state_xml_file" in _inputs:
            state_xml_file = _inputs["state_xml_file"]
        if not is_membrane and _inputs.get("is_membrane"):
            is_membrane = True
        _chain_identity_map_file = _inputs.get("chain_identity_map_file")

        _topo_solvent_mismatch = _check_topology_implicit_solvent_match(
            topology_implicit_solvent=_inputs.get("topology_implicit_solvent"),
            runtime_implicit_solvent=implicit_solvent,
        )
        if _topo_solvent_mismatch is not None:
            begin_node(job_dir, node_id)
            fail_node(job_dir, node_id, errors=_topo_solvent_mismatch["errors"])
            err = create_validation_error(
                "implicit_solvent",
                _topo_solvent_mismatch["message"],
                expected="build-time and runtime implicit_solvent agree",
                actual=(
                    f"build={_inputs.get('topology_implicit_solvent')!r}, "
                    f"runtime={implicit_solvent!r}"
                ),
                code=_topo_solvent_mismatch["code"],
            )
            err["errors"] = _topo_solvent_mismatch["errors"]
            return err

        _ctx = validate_node_execution_context(
            job_dir,
            node_id,
            "min",
            actual_conditions={
                "max_iterations": max_iterations,
                "restraint_atoms": restraint_atoms,
                "restraint_force_constant": restraint_force_constant,
                "is_membrane": is_membrane,
                "implicit_solvent": implicit_solvent,
                "platform": platform,
                "device_index": device_index,
                "hmr": hmr,
            },
        )
        if not _ctx["success"]:
            return {"success": False, "error_type": "ValidationError", **_ctx}

    if not (system_xml_file and topology_pdb_file):
        return create_validation_error(
            "topology_inputs",
            "system_xml_file and topology_pdb_file are required",
            expected="XML triple from build_amber_system / build_openmm_system",
            actual=(
                f"system_xml_file={system_xml_file!r}, "
                f"topology_pdb_file={topology_pdb_file!r}"
            ),
            hints=["Run in node mode from a min node parented to topo."],
            code="missing_xml_topology_inputs",
        )

    job_id = generate_job_id()
    result: Dict[str, Any] = {
        "success": False,
        "job_id": job_id,
        "output_dir": None,
        "minimized_structure": None,
        "state_file": None,
        "minimization_report": None,
        "max_iterations": max_iterations,
        "restraint_atoms": restraint_atoms,
        "restraint_count": 0,
        "restraint_counts_by_component": {},
        "restraint_selection_source": None,
        "platform": None,
        "nan_failure_diagnostics": None,
        "errors": [],
        "warnings": [],
    }

    system_xml_path = Path(system_xml_file).resolve()
    topology_pdb_path = Path(topology_pdb_file).resolve()
    state_xml_path = Path(state_xml_file).resolve() if state_xml_file else None
    if not system_xml_path.is_file():
        result["errors"].append(f"system.xml not found: {system_xml_file}")
        return _fail_node_if_running(job_dir, node_id, result)
    if not topology_pdb_path.is_file():
        result["errors"].append(f"topology.pdb not found: {topology_pdb_file}")
        return _fail_node_if_running(job_dir, node_id, result)
    if state_xml_path is not None and not state_xml_path.is_file():
        result["errors"].append(f"state.xml not found: {state_xml_file}")
        return _fail_node_if_running(job_dir, node_id, result)

    if restraint_atoms not in RESTRAINT_SELECTIONS:
        result["errors"].append(
            "restraint_atoms must be one of: " + ", ".join(RESTRAINT_SELECTIONS)
        )
        result["code"] = "minimization_restraint_atoms_invalid"
        result["allowed_values"] = list(RESTRAINT_SELECTIONS)
        result["recommended_value"] = "solute_heavy"
        return _fail_node_if_running(job_dir, node_id, result)

    try:
        from openmm.app import HCT, OBC1, OBC2, GBn, GBn2, Simulation
        from openmm import CustomExternalForce, Platform, VerletIntegrator
        from openmm.unit import kilojoules_per_mole, nanometer
    except ImportError:
        result["errors"].append("OpenMM not installed")
        return _fail_node_if_running(job_dir, node_id, result)

    IMPLICIT_MODELS = {
        "HCT": HCT, "OBC1": OBC1, "OBC2": OBC2, "GBn": GBn, "GBn2": GBn2,
    }

    try:
        if _node_mode:
            from mdclaw._node import begin_node
            out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            begin_node(job_dir, node_id)
        elif output_dir:
            out_dir = Path(output_dir) / "minimization"
        else:
            out_dir = WORKING_DIR / job_id / "minimization"
        ensure_directory(out_dir)
        result["output_dir"] = str(out_dir)

        xml_inputs = _load_xml_topology_inputs(
            system_xml_file=str(system_xml_path),
            topology_pdb_file=str(topology_pdb_path),
            state_xml_file=str(state_xml_path) if state_xml_path else None,
        )
        restraint_selection = select_restraint_atoms(
            xml_inputs.topology,
            restraint_atoms,
            chain_identity_map_file=_chain_identity_map_file,
        )
        result["warnings"].extend(restraint_selection["warnings"])
        restraint_indices = restraint_selection["atom_indices"]
        result["restraint_count"] = len(restraint_indices)
        result["restraint_counts_by_component"] = restraint_selection[
            "counts_by_component"
        ]
        result["restraint_selection_source"] = restraint_selection[
            "selection_source"
        ]
        if restraint_force_constant > 0 and not restraint_indices:
            result["code"] = "restraint_selection_empty"
            result["allowed_values"] = list(RESTRAINT_SELECTIONS)
            result["recommended_value"] = "solute_heavy"
            result["errors"].append(
                f"restraint_atoms={restraint_atoms!r} matched zero atoms"
            )
            return _fail_node_if_running(job_dir, node_id, result)
        if implicit_solvent:
            _gb_model, gb_err = _resolve_implicit_solvent_model(
                implicit_solvent, IMPLICIT_MODELS
            )
            if gb_err:
                result["errors"].extend(gb_err["errors"])
                result["code"] = gb_err["code"]
                return _fail_node_if_running(job_dir, node_id, result)

        if implicit_solvent:
            solvent_type = "implicit"
        elif xml_inputs.is_periodic:
            solvent_type = "explicit"
        else:
            solvent_type = "vacuum"
            result["warnings"].append(
                "Minimizing a non-periodic topology without implicit_solvent; "
                "downstream equilibration will require a GB or explicit system."
            )

        system_min = _deserialize_xml_system(xml_inputs)
        _validate_xml_system_contract(
            system_min, xml_inputs.topology,
            hmr_request=hmr,
            implicit_solvent_request=implicit_solvent,
        )

        positions = xml_inputs.positions
        restraint = CustomExternalForce(
            "k*periodicdistance(x, y, z, x0, y0, z0)^2"
        )
        restraint.addPerParticleParameter("k")
        restraint.addPerParticleParameter("x0")
        restraint.addPerParticleParameter("y0")
        restraint.addPerParticleParameter("z0")
        k_value = (
            restraint_force_constant
            * kilojoules_per_mole
            / (nanometer * nanometer)
        )
        for atom_index in restraint_indices:
            restraint.addParticle(atom_index, [
                k_value,
                positions[atom_index][0],
                positions[atom_index][1],
                positions[atom_index][2],
            ])
        system_min.addForce(restraint)

        integrator = VerletIntegrator(0.001)
        PLATFORM_MAP = {
            "cuda": "CUDA", "opencl": "OpenCL",
            "cpu": "CPU", "reference": "Reference",
        }
        platform_obj = None
        platform_properties = {}
        if platform.lower() != "auto":
            plat_key = platform.lower()
            if plat_key in PLATFORM_MAP:
                platform_obj = Platform.getPlatformByName(PLATFORM_MAP[plat_key])
                if device_index and plat_key in ("cuda", "opencl"):
                    platform_properties["DeviceIndex"] = device_index
        if platform_obj:
            simulation = Simulation(
                xml_inputs.topology, system_min, integrator,
                platform_obj, platform_properties,
            )
        else:
            simulation = Simulation(xml_inputs.topology, system_min, integrator)
        result["platform"] = simulation.context.getPlatform().getName()
        simulation.context.setPositions(positions)
        if xml_inputs.is_periodic and xml_inputs.box_vectors is not None:
            simulation.context.setPeriodicBoxVectors(*xml_inputs.box_vectors)

        def _finite_energy_check(stage: str) -> dict:
            state = simulation.context.getState(getEnergy=True, getForces=True)
            potential = state.getPotentialEnergy().value_in_unit(
                kilojoules_per_mole
            )
            forces = state.getForces(asNumpy=True)
            force_values = forces.value_in_unit(kilojoules_per_mole / nanometer)
            max_force = (
                float(np.max(np.linalg.norm(force_values, axis=1)))
                if len(force_values) else 0.0
            )
            check = {
                "stage": stage,
                "potential_energy_kj_per_mol": float(potential),
                "max_force_kj_per_mol_nm": max_force,
                "finite": bool(np.isfinite(potential) and np.isfinite(max_force)),
            }
            if not check["finite"]:
                result["nan_failure_diagnostics"] = {
                    "stage": stage,
                    "solvent_type": solvent_type,
                    "implicit_solvent": implicit_solvent,
                    "potential_energy_kj_per_mol": (
                        check["potential_energy_kj_per_mol"]
                    ),
                    "max_force_kj_per_mol_nm": check["max_force_kj_per_mol_nm"],
                    "recommended_next_action": (
                        "inspect/repair the input structure or parameters"
                    ),
                }
                raise RuntimeError(
                    f"Non-finite energy/force detected during {stage}"
                )
            return check

        initial_check = _finite_energy_check("initial")
        simulation.minimizeEnergy(maxIterations=max_iterations)
        final_check = _finite_energy_check("minimized")

        pref = f"{name}_" if name else ""
        state_file = out_dir / f"{pref}minimized.xml"
        _save_state_atomic(simulation, state_file)
        result["state_file"] = str(state_file)

        minimized_state = simulation.context.getState(getPositions=True)
        minimized_structure = out_dir / f"{pref}minimized_structure.pdb"
        # OpenMM's PDBFile loader normalized the Amber/PTM/water residue names
        # when topology.pdb was loaded; the shared exporter restores them from
        # that source file (pure text relabel; MD result/state.xml unaffected).
        from mdclaw.structure.pdb_utils import (
            render_simulation_pdb_preserving_resnames,
        )
        minimized_structure.write_text(
            render_simulation_pdb_preserving_resnames(
                xml_inputs.topology,
                minimized_state.getPositions(),
                topology_pdb_file,
            )
        )
        result["minimized_structure"] = str(minimized_structure)

        report = {
            "minimization": {
                "attempted": True,
                "completed": True,
                "max_iterations": max_iterations,
                "restraint_atoms": restraint_atoms,
                "restraint_force_constant": restraint_force_constant,
                "restraint_count": result["restraint_count"],
                "restraint_counts_by_component": result[
                    "restraint_counts_by_component"
                ],
                "restraint_selection_source": result[
                    "restraint_selection_source"
                ],
                "energy_is_finite": final_check["finite"],
                "positions_are_finite": True,
                "atom_count_preserved": (
                    xml_inputs.topology.getNumAtoms()
                    == len(minimized_state.getPositions())
                ),
                "energy_initial_kj_mol": (
                    initial_check["potential_energy_kj_per_mol"]
                ),
                "energy_final_kj_mol": (
                    final_check["potential_energy_kj_per_mol"]
                ),
                "max_force_initial_kj_mol_nm": (
                    initial_check["max_force_kj_per_mol_nm"]
                ),
                "max_force_final_kj_mol_nm": (
                    final_check["max_force_kj_per_mol_nm"]
                ),
            },
            "checks": [initial_check, final_check],
            "inputs": {
                "system_xml_file": str(system_xml_path),
                "topology_pdb_file": str(topology_pdb_path),
                "state_xml_file": str(state_xml_path) if state_xml_path else None,
            },
            "platform": result["platform"],
            "solvent_type": solvent_type,
            "implicit_solvent": implicit_solvent,
        }
        report_file = out_dir / f"{pref}minimization_report.json"
        tmp_report = report_file.with_name(f".{report_file.name}.tmp.{os.getpid()}")
        tmp_report.write_text(json.dumps(report, indent=2))
        os.replace(tmp_report, report_file)
        result["minimization_report"] = str(report_file)
        result["minimization"] = report["minimization"]
        result["system_signature"] = _system_signature(
            xml_inputs,
            solvent_type=solvent_type,
            ensemble="minimized",
            pressure_bar=None,
            is_membrane=is_membrane,
            implicit_solvent=implicit_solvent,
            hmr=hmr,
        )
        result["success"] = True
    except _ModernSystemContractError as exc:
        logger.error("Minimization aborted by modern-system contract: %s", exc)
        result["errors"].append(str(exc))
        result["code"] = exc.code
    except Exception as exc:  # noqa: BLE001
        logger.error("Minimization failed: %s", exc)
        result["errors"].append(f"Minimization failed: {exc}")

    if _node_mode:
        from mdclaw._node import complete_node, fail_node
        if result.get("success"):
            complete_node(
                job_dir,
                node_id,
                artifacts={
                    "state": _node_artifact_path(result.get("state_file")),
                    "minimized_structure": _node_artifact_path(
                        result.get("minimized_structure")
                    ),
                    "final_structure": _node_artifact_path(
                        result.get("minimized_structure")
                    ),
                    "minimization_report": _node_artifact_path(
                        result.get("minimization_report")
                    ),
                },
                metadata={
                    "platform": result.get("platform"),
                    "max_iterations": max_iterations,
                    "restraint_atoms": restraint_atoms,
                    "restraint_force_constant": restraint_force_constant,
                    "restraint_count": result.get("restraint_count"),
                    "restraint_counts_by_component": result.get(
                        "restraint_counts_by_component"
                    ),
                    "restraint_selection_source": result.get(
                        "restraint_selection_source"
                    ),
                    "is_membrane": is_membrane,
                    "implicit_solvent": implicit_solvent,
                    "hmr": hmr,
                    "final_step": 0,
                    "system_signature": result.get("system_signature"),
                    "minimization": result.get("minimization"),
                },
            )
        else:
            fail_node(job_dir, node_id, errors=result.get("errors", []))

    return result
