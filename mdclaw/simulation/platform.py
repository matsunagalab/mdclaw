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

logger = setup_logger(__name__)

from pathlib import Path  # noqa: E402
from typing import Any, Dict, Optional  # noqa: E402

from mdclaw._common import (  # noqa: E402
    ensure_directory,
)

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)


def export_state_pdb(
    topology_pdb_file: str,
    state_xml_file: str,
    output_pdb_file: str,
    keep_ids: bool = True,
) -> Dict[str, Any]:
    """Export PDB coordinates from an OpenMM ``state.xml`` and ``topology.pdb``.

    Use this when a workflow needs a PDB view of the coordinates stored in a
    serialized OpenMM state. In MDClaw topology builds, ``build_amber_system``
    and ``build_openmm_system`` write a ``system.xml`` + ``topology.pdb`` +
    ``state.xml`` artifact triple. The ``state.xml`` carries the post-build
    topology-time minimization coordinates, while ``topology.pdb`` supplies the
    atom/residue topology used to write a PDB.

    This helper is intentionally not a DAG node; it is an export/convenience
    tool for reports and benchmark submissions. For MDPrepBench, write
    ``output_pdb_file`` as ``submission/minimized_structure.pdb`` and record the
    command in ``provenance.command_log``.

    Args:
        topology_pdb_file: Path to the ``topology.pdb`` from the same topology
            build as the state.
        state_xml_file: Path to the ``state.xml`` whose positions should be
            exported.
        output_pdb_file: Destination PDB path to write.
        keep_ids: Preserve chain and residue IDs from ``topology.pdb`` when
            writing the PDB.

    Returns:
        Dict with ``success``, ``output_pdb``, input paths, atom/position counts,
        and ``errors`` / ``warnings``.
    """
    result: Dict[str, Any] = {
        "success": False,
        "topology_pdb_file": str(topology_pdb_file),
        "state_xml_file": str(state_xml_file),
        "output_pdb_file": str(output_pdb_file),
        "atom_count": 0,
        "position_count": 0,
        "used_state_xml_positions": False,
        "warnings": [],
        "errors": [],
    }

    topology_path = Path(topology_pdb_file)
    state_path = Path(state_xml_file)
    output_path = Path(output_pdb_file)

    if not topology_path.is_file():
        result["code"] = "topology_pdb_not_found"
        result["errors"].append(f"topology.pdb not found: {topology_path}")
        return result
    if not state_path.is_file():
        result["code"] = "state_xml_not_found"
        result["errors"].append(f"state.xml not found: {state_path}")
        return result

    try:
        from openmm import XmlSerializer
        from openmm.app import PDBFile
    except Exception as exc:  # noqa: BLE001
        result["code"] = "openmm_import_failed"
        result["errors"].append(
            f"OpenMM import failed: {type(exc).__name__}: {exc}"
        )
        return result

    try:
        pdb = PDBFile(str(topology_path))
        topology = pdb.topology
        atom_count = topology.getNumAtoms()
        with state_path.open("r") as fh:
            state = XmlSerializer.deserialize(fh.read())
        positions = state.getPositions()
        if positions is None:
            result["code"] = "state_xml_missing_positions"
            result["errors"].append("state.xml does not contain positions")
            return result
        position_count = len(positions)
        result["atom_count"] = atom_count
        result["position_count"] = position_count
        if position_count != atom_count:
            result["code"] = "state_topology_atom_count_mismatch"
            result["errors"].append(
                "state.xml position count does not match topology.pdb atom "
                f"count: positions={position_count}, atoms={atom_count}"
            )
            return result

        try:
            box_vectors = state.getPeriodicBoxVectors()
            if box_vectors is not None:
                topology.setPeriodicBoxVectors(box_vectors)
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(
                "Could not copy periodic box vectors from state.xml: "
                f"{type(exc).__name__}: {exc}"
            )

        ensure_directory(output_path.parent)
        import io as _io

        from mdclaw.structure.pdb_utils import (
            preserve_long_resnames_in_pdb_text,
        )
        pdb_buffer = _io.StringIO()
        PDBFile.writeFile(topology, positions, pdb_buffer, keepIds=keep_ids)
        output_path.write_text(
            preserve_long_resnames_in_pdb_text(pdb_buffer.getvalue(), topology)
        )

        result["success"] = True
        result["output_pdb"] = str(output_path)
        result["used_state_xml_positions"] = True
        return result
    except Exception as exc:  # noqa: BLE001
        result["code"] = "state_pdb_export_failed"
        result["errors"].append(
            f"state PDB export failed: {type(exc).__name__}: {exc}"
        )
        return result


def inspect_openmm_platforms(
    atom_count: Optional[int] = None,
    solvent_type: str = "explicit",
) -> dict:
    """Report usable OpenMM platforms and local-run feasibility guidance.

    This is a lightweight preflight helper for agents before launching local
    explicit-water topology/equilibration/production. Each platform is counted
    as available only after a tiny Context can be created; plugins that merely
    register but cannot execute are reported under ``unusable_platforms``.
    """
    result = {
        "success": False,
        "platforms": [],
        "unusable_platforms": [],
        "gpu_platforms": [],
        "fastest_platform": None,
        "default_platform": None,
        "atom_count": atom_count,
        "solvent_type": solvent_type,
        "local_feasibility": None,
        "recommendation": None,
        "warnings": [],
        "errors": [],
    }
    try:
        from openmm import Context, Platform, System, VerletIntegrator, unit
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"OpenMM platform inspection failed: {type(exc).__name__}: {exc}"
        )
        result["code"] = "openmm_platform_inspection_failed"
        return result

    try:
        platform_names = [
            Platform.getPlatform(i).getName()
            for i in range(Platform.getNumPlatforms())
        ]
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"Could not enumerate OpenMM platforms: {type(exc).__name__}: {exc}"
        )
        result["code"] = "openmm_platform_inspection_failed"
        return result

    usable_platforms: list[str] = []
    unusable_platforms: list[dict[str, str]] = []
    platform_speeds: dict[str, float] = {}
    for platform_name in platform_names:
        try:
            platform_obj = Platform.getPlatformByName(platform_name)
            system = System()
            system.addParticle(39.948)
            integrator = VerletIntegrator(1.0 * unit.femtoseconds)
            context = Context(system, integrator, platform_obj)
            usable_platforms.append(platform_name)
            platform_speeds[platform_name] = float(platform_obj.getSpeed())
            del context, integrator
        except Exception as exc:  # noqa: BLE001
            unusable_platforms.append({
                "platform": platform_name,
                "error": f"{type(exc).__name__}: {exc}",
            })

    try:
        system = System()
        system.addParticle(39.948)
        integrator = VerletIntegrator(1.0 * unit.femtoseconds)
        context = Context(system, integrator)
        result["default_platform"] = context.getPlatform().getName()
        del context, integrator
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(
            f"OpenMM default Context probe failed: {type(exc).__name__}: {exc}"
        )

    gpu_platforms = [
        p for p in usable_platforms if p in {"CUDA", "OpenCL", "HIP"}
    ]
    result["platforms"] = usable_platforms
    result["unusable_platforms"] = unusable_platforms
    result["gpu_platforms"] = gpu_platforms
    result["fastest_platform"] = (
        max(usable_platforms, key=lambda name: platform_speeds.get(name, 0.0))
        if usable_platforms else None
    )
    result["success"] = True

    for item in unusable_platforms:
        name = item["platform"]
        if name in {"CUDA", "OpenCL", "HIP"}:
            result["warnings"].append(
                f"{name} is registered by OpenMM but cannot create a Context "
                f"in this process ({item['error']})."
            )

    if atom_count is None:
        result["local_feasibility"] = "unknown"
        result["recommendation"] = (
            "Provide atom_count from solvate_structure statistics to classify "
            "local explicit-water feasibility."
        )
        return result

    explicit = str(solvent_type).strip().lower() == "explicit"
    if explicit and not gpu_platforms and atom_count >= 30000:
        result["local_feasibility"] = "not_recommended"
        result["recommendation"] = (
            "Explicit-water local CPU execution is likely slow for this system. "
            "Use /hpc-run or shorten equilibration/production deliberately for "
            "a smoke test before starting a full local run."
        )
        result["warnings"].append(
            "No usable CUDA/OpenCL/HIP platform detected for a large "
            "explicit-water system."
        )
    elif explicit and not gpu_platforms and atom_count >= 10000:
        result["local_feasibility"] = "slow_on_cpu"
        result["recommendation"] = (
            "Local CPU execution may be slow. Prefer a usable GPU platform "
            "or use short explicit smoke-test steps before longer runs."
        )
    else:
        result["local_feasibility"] = "reasonable"
        result["recommendation"] = (
            "Local execution is not blocked by the simple platform/atom-count "
            "preflight. Continue with explicit platform selection when needed."
        )
    return result


# =============================================================================
# Tool Registry
# =============================================================================
