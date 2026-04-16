"""Job progress tracking — auto-updated after each tool execution.

progress.json is the source of truth for job state.  Each tool in the
pipeline (prepare_complex → solvate_structure → build_amber_system →
run_equilibration → run_production) automatically updates it via the
CLI hook in _cli.py.

Design principle: "skill が stateful" ではなく "job directory が stateful
で、正本が progress.json"。
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def update_progress_from_result(
    tool_name: str, result: dict, output_dir: str,
) -> None:
    """Update progress.json after a tool execution.

    Called by _cli.py after every tool in TOOL_HANDLERS.
    Creates progress.json on first tool (prepare_complex) and merges
    updates for subsequent tools.  Works on both success and failure —
    failure records status/errors without advancing completed_steps.
    """
    if tool_name not in _TOOL_HANDLERS:
        return

    handler = _TOOL_HANDLERS[tool_name]

    # For prepare_complex, the job root is the parent of output_dir (which is split/)
    if tool_name == "prepare_complex":
        job_dir = _infer_job_dir_from_prepare(result, output_dir)
    else:
        job_dir = _find_job_dir(output_dir)

    if job_dir is None:
        logger.debug(f"No job directory found for {tool_name}, skipping progress update")
        return

    try:
        handler(result, job_dir)
    except Exception as e:
        logger.warning(f"Failed to update progress.json for {tool_name}: {e}")


# ---------------------------------------------------------------------------
# Job directory detection
# ---------------------------------------------------------------------------

def _find_job_dir(output_dir: str) -> Optional[Path]:
    """Find the job root by searching for progress.json upward."""
    p = Path(output_dir)
    for d in [p, *list(p.parents)[:3]]:
        if (d / "progress.json").exists():
            return d
    return None


def _infer_job_dir_from_prepare(result: dict, output_dir: str) -> Optional[Path]:
    """Infer job root from prepare_complex result.

    prepare_complex sets output_dir = job_xxx/split, so parent = job root.
    Also check merged_pdb path as fallback.
    """
    # output_dir is typically job_xxx/split
    candidate = Path(output_dir).parent
    if candidate.name.startswith("job_"):
        return candidate

    # Fallback: derive from merged_pdb path
    merged = result.get("merged_pdb")
    if merged:
        # merged_pdb = job_xxx/merge/merged.pdb → parent.parent = job root
        return Path(merged).parent.parent

    return Path(output_dir)


# ---------------------------------------------------------------------------
# Deep merge helper
# ---------------------------------------------------------------------------

def _merge_progress(job_dir: Path, updates: dict) -> None:
    """Read existing progress.json, apply updates, write back.

    Merge rules:
    - completed_steps: append, deduplicate
    - runs: match by run_id → update; otherwise append
    - warnings: extend
    - dicts (artifacts, system, solvation, ...): shallow merge (update keys)
    - scalars: overwrite
    """
    progress_path = job_dir / "progress.json"
    if progress_path.exists():
        data = json.loads(progress_path.read_text())
    else:
        data = {}

    for key, value in updates.items():
        if key == "completed_steps":
            existing = data.get("completed_steps", [])
            for step in value:
                if step not in existing:
                    existing.append(step)
            data["completed_steps"] = existing

        elif key == "runs":
            existing_runs = data.get("runs", [])
            for new_run in value:
                matched = False
                for i, er in enumerate(existing_runs):
                    if er.get("run_id") == new_run.get("run_id"):
                        existing_runs[i].update(new_run)
                        matched = True
                        break
                if not matched:
                    existing_runs.append(new_run)
            data["runs"] = existing_runs

        elif key == "warnings":
            existing_warnings = data.get("warnings", [])
            existing_warnings.extend(value)
            data["warnings"] = existing_warnings

        elif isinstance(value, dict):
            existing_dict = data.get(key, {})
            if isinstance(existing_dict, dict):
                existing_dict.update(value)
                data[key] = existing_dict
            else:
                data[key] = value
        else:
            data[key] = value

    progress_path.write_text(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# Per-tool handlers
# ---------------------------------------------------------------------------

def _on_prepare_complex(result: dict, job_dir: Path) -> None:
    """Create or update progress.json from prepare_complex result."""
    success = result.get("success", False)
    overall_status = result.get("overall_status", "failed")

    # Extract system info
    proteins = result.get("proteins", [])
    ligands = result.get("ligands", [])
    system = {}
    if proteins:
        system["chains"] = [p.get("chain_id", "") for p in proteins]
        system["num_residues"] = sum(
            p.get("statistics", {}).get("final_residues", 0)
            for p in proteins if p.get("success")
        )
    if ligands:
        system["ligands"] = [
            lig["ligand_id"] for lig in ligands if lig.get("success")
        ]

    # Extract artifacts
    artifacts = {"source_file": result.get("source_file")}
    if result.get("merged_pdb"):
        artifacts["merged_pdb"] = result["merged_pdb"]

    successful_ligands = [
        {
            "mol2": lig.get("mol2_file"),
            "frcmod": lig.get("frcmod_file"),
            "residue_name": lig.get("ligand_id", "LIG")[:3].upper(),
            "parameter_source": lig.get("parameter_source"),
        }
        for lig in ligands
        if lig.get("success") and lig.get("mol2_file")
    ]
    if successful_ligands:
        artifacts["ligand_params"] = successful_ligands

    # Build progress data
    now = datetime.now(timezone.utc).isoformat()
    progress = {
        "schema_version": "2.0",
        "job_id": result.get("job_id", job_dir.name),
        "created_at": now,
        "status": overall_status,
        "current_step": "prepare",
        "completed_steps": ["prepare"] if success else [],
        "system": system,
        "preparation": result.get("preparation_summary", {}),
        "solvation": {},
        "forcefield": {},
        "artifacts": artifacts,
        "runs": [],
        "warnings": result.get("warnings", []),
    }

    # Next step depends on status
    if overall_status == "success":
        progress["next_step"] = {
            "skill": "solvation",
            "cli_hint": f"mdclaw solvate_structure --pdb-file {result.get('merged_pdb', '<merged_pdb>')} --output-dir {job_dir}",
        }
    elif overall_status == "completed_with_blocking_ligand_failure":
        progress["blocking"] = result.get("workflow_recommendation")
        progress["next_step"] = None
    else:
        progress["next_step"] = None

    # Write (init or overwrite)
    progress_path = job_dir / "progress.json"
    progress_path.write_text(json.dumps(progress, indent=2, default=str))
    logger.info(f"progress.json created: {progress_path}")


def _on_solvate_structure(result: dict, job_dir: Path) -> None:
    """Merge solvation info into progress.json."""
    success = result.get("success", False)
    params = result.get("parameters", {})
    box = result.get("box_dimensions", {})

    updates = {
        "current_step": "solvate",
        "status": "solvation_done" if success else "solvation_failed",
    }

    if success:
        updates["completed_steps"] = ["solvate"]
        updates["solvation"] = {
            "type": "explicit",
            "water_model": params.get("water_model", ""),
            "box_shape": "cubic" if box.get("is_cubic") else "rectangular",
            "box_size_angstrom": [box.get("box_a"), box.get("box_b"), box.get("box_c")],
            "buffer_distance_angstrom": params.get("dist"),
            "salt_concentration_M": params.get("saltcon"),
        }
        updates["system"] = {
            "num_atoms_total": result.get("statistics", {}).get("total_atoms"),
        }
        updates["artifacts"] = {"solvated_pdb": result.get("output_file")}
        updates["next_step"] = {
            "skill": "topology",
            "cli_hint": f"mdclaw build_amber_system --pdb-file {result.get('output_file', '<solvated_pdb>')} --output-dir {job_dir}",
        }
    else:
        updates["warnings"] = result.get("errors", [])

    _merge_progress(job_dir, updates)
    logger.info(f"progress.json updated: solvate (success={success})")


def _on_build_amber_system(result: dict, job_dir: Path) -> None:
    """Merge topology info into progress.json."""
    success = result.get("success", False)

    updates = {
        "current_step": "topology",
        "status": "topology_done" if success else "topology_failed",
    }

    if success:
        updates["completed_steps"] = ["topology"]
        params = result.get("parameters", {})
        updates["forcefield"] = {
            "protein": params.get("forcefield") or result.get("forcefield"),
            "water": params.get("water_model") or result.get("water_model"),
        }
        updates["artifacts"] = {
            "parm7": result.get("parm7"),
            "rst7": result.get("rst7"),
        }
        updates["next_step"] = {
            "skill": "md-equilibration",
            "cli_hint": f"/md-equilibration {job_dir}",
        }
    else:
        updates["warnings"] = result.get("errors", [])

    _merge_progress(job_dir, updates)
    logger.info(f"progress.json updated: topology (success={success})")


def _on_run_equilibration(result: dict, job_dir: Path) -> None:
    """Update progress.json and run.json after equilibration."""
    success = result.get("success", False)

    updates = {
        "current_step": "equilibration",
        "status": "equilibration_done" if success else "equilibration_failed",
    }

    if success:
        updates["completed_steps"] = ["equilibration"]
        # Add/update run entry
        output_dir = result.get("output_dir", "")
        run_dir = Path(output_dir).parent if output_dir else None
        run_id = run_dir.name if run_dir else "unknown"
        run_entry = {
            "run_id": run_id,
            "status": "equilibrated",
            "checkpoint": result.get("checkpoint_file"),
        }
        updates["runs"] = [run_entry]
        updates["next_step"] = {
            "skill": "md-production",
            "cli_hint": f"/md-production {job_dir}",
        }
    else:
        updates["warnings"] = result.get("errors", [])

    _merge_progress(job_dir, updates)

    # Also update run.json if it exists
    if success and result.get("output_dir"):
        _update_run_json_equilibration(result)

    logger.info(f"progress.json updated: equilibration (success={success})")


def _on_run_production(result: dict, job_dir: Path) -> None:
    """Update progress.json and run.json after production."""
    success = result.get("success", False)

    updates = {
        "current_step": "production",
        "status": "production_done" if success else "production_failed",
    }

    if success:
        updates["completed_steps"] = ["production"]
        output_dir = result.get("output_dir", "")
        run_dir = Path(output_dir).parent if output_dir else None
        run_id = run_dir.name if run_dir else "unknown"
        run_entry = {
            "run_id": run_id,
            "status": "completed",
            "trajectory": result.get("trajectory_file"),
        }
        updates["runs"] = [run_entry]
        updates["next_step"] = {
            "skill": "md-analyze",
            "cli_hint": f"/md-analyze {job_dir}",
        }
    else:
        updates["warnings"] = result.get("errors", [])

    _merge_progress(job_dir, updates)

    if success and result.get("output_dir"):
        _update_run_json_production(result)

    logger.info(f"progress.json updated: production (success={success})")


# ---------------------------------------------------------------------------
# run.json helpers
# ---------------------------------------------------------------------------

def _update_run_json_equilibration(result: dict) -> None:
    """Update run.json with equilibration results."""
    output_dir = Path(result.get("output_dir", ""))
    # output_dir is typically runs/run_xxx/equilibration → parent = run dir
    run_dir = output_dir.parent
    run_json = run_dir / "run.json"
    if not run_json.exists():
        return

    try:
        data = json.loads(run_json.read_text())
        stages = data.setdefault("stages", {})
        stages["equilibration"] = {
            "status": "completed" if result.get("success") else "failed",
            "checkpoint": result.get("checkpoint_file"),
            "final_structure": result.get("final_structure"),
            "platform": result.get("platform"),
        }
        run_json.write_text(json.dumps(data, indent=2, default=str))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to update run.json: {e}")


def _update_run_json_production(result: dict) -> None:
    """Update run.json with production results."""
    output_dir = Path(result.get("output_dir", ""))
    run_dir = output_dir.parent
    run_json = run_dir / "run.json"
    if not run_json.exists():
        return

    try:
        data = json.loads(run_json.read_text())
        stages = data.setdefault("stages", {})
        stages["production"] = {
            "status": "completed" if result.get("success") else "failed",
            "trajectory": result.get("trajectory_file"),
            "checkpoint_file": result.get("checkpoint_file"),
            "final_structure": result.get("final_structure"),
            "simulation_time_ns": result.get("simulation_time_ns"),
        }
        run_json.write_text(json.dumps(data, indent=2, default=str))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to update run.json: {e}")


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

_TOOL_HANDLERS = {
    "prepare_complex": _on_prepare_complex,
    "solvate_structure": _on_solvate_structure,
    "build_amber_system": _on_build_amber_system,
    "run_equilibration": _on_run_equilibration,
    "run_production": _on_run_production,
}
