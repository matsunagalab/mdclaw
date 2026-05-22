"""Deterministic check execution and run-level aggregation for v1.0.

Public entry points:

- :func:`score_submission` — runs every check on a submission and produces a
  :class:`Score` (still a pydantic model; serialized to score.json by callers).
- :func:`aggregate_run_scores` — combines per-task score.json files into a
  run-level summary, using axis aggregation that divides by the number of
  tasks where the axis is in scope (NOT total task count).
"""

from __future__ import annotations

import math
import re
import statistics
from pathlib import Path
from typing import Any, Optional

from mdclaw.benchmark import integrity
from mdclaw.benchmark.models import (
    SCORE_AXES,
    CheckResult,
    DeterministicCheck,
    GroundTruthCheck,
    LLMJudgeResult,
    RuntimeRecord,
    Score,
    Task,
)


_CRITICAL_PREP_CHECK_TYPES = {
    "structure_component_rescan",
    "pdb_residue_state",
    "pdb_no_deuterium_atoms",
    "assembly_identity_check",
    "candidate_selection_check",
    "topology_artifact_bundle",
    "openmm_system_load",
    "openmm_energy_rescan",
    "minimization_report_check",
    "minimized_structure_component_rescan",
    "minimized_structure_required",
}

_DEUTERIUM_FALLBACK_ATOM_NAME_RE = re.compile(r"^D[0-9]*$")

# A finite OpenMM energy is not automatically physically meaningful. Values
# above this per-particle scale indicate severe clashes or bad periodic boxes,
# like Packmol-forced membrane outputs that produce 1e20 kJ/mol energies.
_MAX_ABS_PREP_ENERGY_PER_PARTICLE_KJ_MOL = 1.0e6
_MAX_ABS_PREP_TOTAL_ENERGY_KJ_MOL = 1.0e12


# ---------------------------------------------------------------------------
# Submission-level scoring


def score_submission(
    task: Task,
    submission_dir: Path,
    run_id: str = "",
    llm_judge_payload: Optional[dict[str, Any]] = None,
    task_dir: Optional[Path] = None,
) -> Score:
    """Run every deterministic and ground-truth check defined by ``task``
    against ``submission_dir`` and return a :class:`Score`.

    ``task_dir`` is the directory of the task contract; when given, ground
    truth files are read from ``<task_dir>/<truth_file>``. If omitted,
    ground-truth checks are skipped.
    """

    submission_dir = Path(submission_dir)
    manifest = integrity.read_json_safe(submission_dir / "manifest.json")
    metrics = integrity.read_json_safe(submission_dir / "metrics.json")
    provenance = integrity.read_json_safe(submission_dir / "provenance.json")
    evidence = integrity.read_json_safe(submission_dir / "evidence_report.json")

    deterministic_results: list[CheckResult] = []
    ground_truth_results: list[CheckResult] = []
    integrity_warnings: list[str] = []

    # 1. provenance md5 verification
    integrity_warnings.extend(
        integrity.verify_provenance_hashes(submission_dir, provenance)
    )

    # 2. metrics ↔ manifest cross-check
    integrity_warnings.extend(
        integrity.manifest_metrics_consistency(manifest, metrics)
    )

    # 2b. artifact integrity (re-verify bytes on disk, not just JSON values)
    artifact_warnings = integrity.run_artifact_integrity(
        submission_dir,
        task.scoring.integrity_checks,
        manifest=manifest,
        evidence=evidence,
        task_dir=task_dir,
    )
    integrity_warnings.extend(artifact_warnings)

    manifest_status = manifest.get("status", "completed")
    if (
        manifest_status == "blocked"
        and not task.failure_policy.blocked_by_missing_input_allowed
        and not task.failure_policy.insufficient_information_allowed
    ):
        integrity_warnings.append(
            "manifest.status='blocked' but task failure_policy does not allow "
            "blocked outcomes"
        )

    # 3. deterministic checks
    for check in task.scoring.deterministic_checks:
        result = _run_deterministic(
            check, submission_dir, manifest, metrics,
            provenance=provenance, evidence=evidence, task_dir=task_dir,
        )
        deterministic_results.append(result)

    minimized_required = _completed_minimized_structure_check(
        task,
        manifest,
        submission_dir,
    )
    if minimized_required is not None:
        deterministic_results.append(minimized_required)

    critical_failures = [
        result for result in deterministic_results
        if (
            result.check_type in _CRITICAL_PREP_CHECK_TYPES
            or result.check_id.startswith("minimized_")
        ) and not result.passed
    ]

    # 4. ground-truth checks
    if task_dir is not None:
        for gtc in task.scoring.ground_truth_checks:
            result = _run_ground_truth(gtc, submission_dir, task_dir, evidence)
            ground_truth_results.append(result)

    # 5. axis assembly
    axis_scores = _assemble_axis_scores(
        task, deterministic_results, ground_truth_results,
        llm_judge_payload=llm_judge_payload,
    )

    # 6. apply manifest.status semantics
    weighted_total = _weighted_total(task, axis_scores)
    weighted_total = _apply_status_modifier(
        manifest_status, weighted_total, axis_scores, ground_truth_results,
    )

    # 7a. reject-phase clamp — if the task opts in to integrity_policy="reject"
    # and any artifact integrity warning fired, weighted_total drops to 0.
    # This is a hard contract: the agent cannot earn primary score with a
    # template-stub submission.
    integrity_rejected = bool(
        task.scoring.integrity_policy == "reject" and artifact_warnings
    )
    if integrity_rejected:
        weighted_total = 0.0
        axis_scores = {
            axis: (0.0 if value is not None else None)
            for axis, value in axis_scores.items()
        }

    # 7b. warn-phase penalty (per-warning -0.05, capped at -0.2). Applies under
    # both policies; under "reject" it just turns 0 into 0.
    if integrity_warnings:
        penalty = min(0.05 * len(integrity_warnings), 0.2)
        weighted_total = max(0.0, weighted_total - penalty)

    if manifest_status == "completed" and critical_failures:
        weighted_total = 0.0
        axis_scores = {
            axis: (0.0 if value is not None else None)
            for axis, value in axis_scores.items()
        }

    score_status = _score_status(weighted_total, deterministic_results,
                                 ground_truth_results)
    if manifest_status == "blocked":
        score_status = "failed"
    if manifest_status == "completed" and critical_failures:
        score_status = "failed"
    if integrity_rejected:
        # Reject overrides the "any check passed → partial" rule: if the
        # artifact layer rejected the submission, the run did not produce
        # work worth crediting, even if a deterministic string-equality check
        # happens to match.
        score_status = "failed"

    runtime = _extract_runtime(manifest, metrics)

    return Score(
        schema_version="1.0",
        run_id=run_id,
        task_id=task.task_id,
        primary_score=task.primary_score,
        status=score_status,
        weighted_total=round(weighted_total, 4),
        scores={k: (round(v, 4) if v is not None else None)
                for k, v in axis_scores.items()},
        deterministic_checks=deterministic_results,
        ground_truth_checks=ground_truth_results,
        llm_judge=_build_llm_judge_record(task, llm_judge_payload),
        runtime=runtime,
        integrity_warnings=integrity_warnings,
        errors=[],
    )


# ---------------------------------------------------------------------------
# Per-check dispatch


def _run_deterministic(
    check: DeterministicCheck,
    submission_dir: Path,
    manifest: dict,
    metrics: dict,
    provenance: dict,
    evidence: dict,
    task_dir: Optional[Path],
) -> CheckResult:
    handler = _DETERMINISTIC_DISPATCH.get(check.check_type)
    if handler is None:
        return CheckResult(
            check_id=check.check_id, check_type=check.check_type,
            passed=False, score=0.0, weight=check.weight,
            message=f"unknown check_type {check.check_type!r}",
        )
    try:
        passed, score, message = handler(
            check, submission_dir,
            manifest=manifest, metrics=metrics,
            provenance=provenance, evidence=evidence, task_dir=task_dir,
        )
    except Exception as exc:  # pragma: no cover -- handler exceptions become 0.0
        return CheckResult(
            check_id=check.check_id, check_type=check.check_type,
            passed=False, score=0.0, weight=check.weight,
            message=f"check raised {type(exc).__name__}: {exc}",
        )
    return CheckResult(
        check_id=check.check_id, check_type=check.check_type,
        passed=passed, score=score, weight=check.weight, message=message,
    )


def _completed_minimized_structure_check(
    task: Task,
    manifest: dict, submission_dir: Path,
) -> CheckResult | None:
    if "minimized_structure.pdb" not in task.required_outputs:
        return None
    if manifest.get("status", "completed") != "completed":
        return None
    rel = _manifest_artifact_path(
        manifest, "outputs.minimized_structure", "outputs.minimized_structure",
    )
    if not rel:
        return CheckResult(
            check_id="completed_manifest_minimized_structure_present",
            check_type="minimized_structure_required",
            passed=False,
            score=0.0,
            weight=1.0,
            message="manifest.status='completed' requires outputs.minimized_structure",
        )
    path = _resolve_relative(submission_dir, rel)
    if not path.is_file():
        return CheckResult(
            check_id="completed_manifest_minimized_structure_present",
            check_type="minimized_structure_required",
            passed=False,
            score=0.0,
            weight=1.0,
            message=f"outputs.minimized_structure points to missing file: {rel}",
        )
    return None


def _check_required_files(check: DeterministicCheck, submission_dir: Path,
                          **_):
    paths = check.required_outputs or []
    missing = [p for p in paths
               if not _resolve_relative(submission_dir, p).exists()]
    if missing:
        return False, 0.0, f"missing: {missing}"
    return True, 1.0, f"all {len(paths)} required outputs present"


def _check_forbidden_files(check: DeterministicCheck, submission_dir: Path,
                           **_):
    paths = check.forbidden_outputs or []
    present = [p for p in paths
               if _resolve_relative(submission_dir, p).exists()]
    if present:
        return False, 0.0, f"forbidden files present: {present}"
    return True, 1.0, f"all {len(paths)} forbidden outputs absent"


def _check_artifact_provenance_text(check: DeterministicCheck,
                                    submission_dir: Path,
                                    provenance: dict,
                                    evidence: dict, **_):
    files = check.text_files or ["provenance.json", "evidence_report.json"]
    chunks: list[str] = []
    for rel in files:
        if rel == "provenance.json":
            chunks.extend(_recursive_text_fragments(provenance))
            continue
        if rel == "evidence_report.json":
            chunks.extend(_recursive_text_fragments(evidence))
            continue
        path = _resolve_relative(submission_dir, rel)
        if not path.is_file():
            continue
        try:
            if path.suffix.lower() == ".json":
                chunks.extend(_recursive_text_fragments(integrity.read_json_safe(path)))
            else:
                chunks.append(path.read_text(errors="replace"))
        except OSError:
            continue

    haystack = "\n".join(chunks).casefold()
    missing_groups: list[list[str]] = []
    for group in check.required_text_groups or []:
        if not any(str(term).casefold() in haystack for term in group):
            missing_groups.append([str(term) for term in group])
    if missing_groups:
        return (
            False,
            0.0,
            f"required provenance/evidence text not found: {missing_groups}",
        )
    return (
        True,
        1.0,
        f"required provenance/evidence text groups found in {files}",
    )


def _check_json_equals(check: DeterministicCheck, submission_dir: Path,
                       metrics: dict, **_):
    value = _read_json_path(submission_dir, check, metrics_default=metrics)
    if value is None:
        return (False, 0.0,
                f"JSON path {check.json_path!r} not found in {check.json_file or 'metrics.json'}")
    ok = (value == check.equals)
    return ok, (1.0 if ok else 0.0), (
        f"{check.json_path}={value!r} expected {check.equals!r}")


def _check_json_max(check: DeterministicCheck, submission_dir: Path,
                    metrics: dict, **_):
    value = _read_json_path(submission_dir, check, metrics_default=metrics)
    if value is None:
        return False, 0.0, f"JSON path {check.json_path!r} not found"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False, 0.0, f"JSON path {check.json_path!r} value {value!r} is not numeric"
    ok = numeric <= float(check.max_value)
    return ok, (1.0 if ok else 0.0), f"{check.json_path}={numeric} <= {check.max_value}"


def _check_json_min(check: DeterministicCheck, submission_dir: Path,
                    metrics: dict, **_):
    value = _read_json_path(submission_dir, check, metrics_default=metrics)
    if value is None:
        return False, 0.0, f"JSON path {check.json_path!r} not found"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False, 0.0, f"JSON path {check.json_path!r} value {value!r} is not numeric"
    ok = numeric >= float(check.min_value)
    return ok, (1.0 if ok else 0.0), f"{check.json_path}={numeric} >= {check.min_value}"


def _check_json_min_length(check: DeterministicCheck, submission_dir: Path,
                           manifest: dict, metrics: dict, **_):
    # length checks usually live in manifest (figures), not metrics
    value = _read_json_path(submission_dir, check,
                            metrics_default=metrics, manifest=manifest)
    if value is None:
        return False, 0.0, f"JSON path {check.json_path!r} not found"
    try:
        length = len(value)
    except TypeError:
        return False, 0.0, f"value at {check.json_path!r} has no length"
    ok = length >= int(check.min_length)
    return ok, (1.0 if ok else 0.0), f"len({check.json_path})={length} >= {check.min_length}"


def _check_json_allowed_values(check: DeterministicCheck, submission_dir: Path,
                               metrics: dict, **_):
    value = _read_json_path(submission_dir, check, metrics_default=metrics)
    if value is None:
        return False, 0.0, f"JSON path {check.json_path!r} not found"
    ok = value in (check.allowed_values or [])
    return ok, (1.0 if ok else 0.0), (
        f"{check.json_path}={value!r} in {check.allowed_values}")


def _check_trajectory_rescan(check: DeterministicCheck, submission_dir: Path,
                             manifest: dict, **_):
    traj_rel = _manifest_artifact_path(
        manifest, check.trajectory_manifest_path, "outputs.trajectories.0",
    ) or check.trajectory_path
    top_rel = _manifest_artifact_path(
        manifest, check.topology_manifest_path, "outputs.topology.0",
    ) or check.topology_path
    if not traj_rel or not top_rel:
        return False, 0.0, (
            "trajectory/topology path required via task path or manifest outputs"
        )
    traj_path = _resolve_relative(submission_dir, traj_rel)
    top_path = _resolve_relative(submission_dir, top_rel)
    n_frames, has_nan, msg = integrity.rescan_trajectory_for_nan(traj_path, top_path)
    if n_frames is None:
        return False, 0.0, msg
    require = check.require_min_frames or 1
    if n_frames < require:
        return False, 0.0, f"{msg}; require >= {require}"
    if has_nan:
        return False, 0.0, f"{msg}; NaN coordinates detected"
    return True, 1.0, msg


def _check_topology_solvent_rescan(check: DeterministicCheck,
                                   submission_dir: Path,
                                   manifest: dict, **_):
    top_rel = _manifest_artifact_path(
        manifest, check.topology_manifest_path, "outputs.topology.0",
    ) or check.topology_path
    if not top_rel:
        return False, 0.0, "topology path required via task path or manifest outputs"

    topology_path = _resolve_relative(submission_dir, top_rel)
    if not topology_path.is_file():
        return False, 0.0, f"topology file not found: {topology_path}"

    water_names = {
        str(name).strip().upper()
        for name in (check.water_residue_names or ["HOH", "WAT", "TIP3", "TP3"])
    }
    min_water = int(check.min_water_residues or 1)
    water_residues: set[tuple[str, str, str, str]] = set()

    try:
        with topology_path.open() as handle:
            for line in handle:
                if not line.startswith(("ATOM  ", "HETATM")):
                    continue
                resname = line[17:20].strip().upper()
                if resname not in water_names:
                    continue
                chain_id = line[21].strip()
                resseq = line[22:26].strip()
                icode = line[26].strip()
                water_residues.add((chain_id, resseq, icode, resname))
    except OSError as exc:
        return False, 0.0, f"could not read topology file: {exc}"

    water_count = len(water_residues)
    required = (check.required_solvent_type or "explicit_water").strip().lower()
    if required != "explicit_water":
        return False, 0.0, f"unsupported required_solvent_type {required!r}"
    if water_count < min_water:
        return (
            False,
            0.0,
            f"found {water_count} water residues in topology; require >= {min_water}",
        )
    return (
        True,
        1.0,
        f"found {water_count} water residues in topology; require >= {min_water}",
    )


def _topology_backend(check: DeterministicCheck,
                      submission_dir: Path,
                      metrics: dict,
                      manifest: Optional[dict] = None) -> str:
    backend_file = check.topology_backend_json_file or "metrics.json"
    backend_path = check.topology_backend_json_path or "topology.backend"
    value = _read_submission_json_path(
        submission_dir,
        backend_file,
        backend_path,
        metrics_default=metrics,
        manifest=manifest,
    )
    return str(value or "").strip().lower()


def _check_topology_artifact_bundle(check: DeterministicCheck,
                                    submission_dir: Path,
                                    manifest: dict,
                                    metrics: dict, **_):
    backend = _topology_backend(check, submission_dir, metrics, manifest)
    required_backend = (check.required_topology_backend or "").strip().lower()
    if required_backend and backend != required_backend:
        return (
            False,
            0.0,
            f"topology.backend={backend!r} expected {required_backend!r}",
        )
    if not required_backend and backend != "openmm":
        return (
            False,
            0.0,
            "prep battery requires OpenMM topology bundle; "
            f"topology.backend={backend or 'unspecified'!r}",
        )

    topology_rels = _manifest_artifact_paths(
        manifest, check.topology_manifest_path, "outputs.topology",
    )
    min_count = int(check.min_topology_artifact_count or 1)
    if len(topology_rels) < min_count:
        return (
            False,
            0.0,
            f"outputs.topology has {len(topology_rels)} artifact(s); require >= {min_count}",
        )

    missing_files = [
        rel for rel in topology_rels
        if not _resolve_relative(submission_dir, rel).is_file()
    ]
    if missing_files:
        return False, 0.0, f"topology artifacts missing: {missing_files}"

    if backend == "openmm":
        artifacts, issues = _resolve_openmm_artifacts(check, submission_dir, manifest)
        if issues:
            return False, 0.0, "; ".join(issues)
        required = set(check.required_topology_artifacts or [
            "system_xml", "topology_pdb", "state_xml",
        ])
        missing_roles = sorted(role for role in required if artifacts.get(role) is None)
        if missing_roles:
            return False, 0.0, f"OpenMM topology bundle missing roles: {missing_roles}"
        return True, 1.0, "OpenMM topology bundle contains system/topology/state artifacts"

    return (
        True,
        1.0,
        f"{backend or 'unspecified'} topology bundle has {len(topology_rels)} artifact(s)",
    )


def _check_openmm_system_load(check: DeterministicCheck,
                              submission_dir: Path,
                              manifest: dict,
                              metrics: dict, **_):
    backend = _topology_backend(check, submission_dir, metrics, manifest)
    if backend != "openmm":
        return (
            False,
            0.0,
            "prep battery requires OpenMM topology bundle; "
            f"topology.backend={backend or 'unspecified'!r}",
        )

    loaded = _load_openmm_bundle(check, submission_dir, manifest)
    if not loaded["success"]:
        return False, 0.0, loaded["message"]
    return True, 1.0, loaded["message"]


def _check_openmm_energy_rescan(check: DeterministicCheck,
                                submission_dir: Path,
                                manifest: dict,
                                metrics: dict, **_):
    backend = _topology_backend(check, submission_dir, metrics, manifest)
    if backend != "openmm":
        return (
            False,
            0.0,
            "prep battery requires OpenMM topology bundle; "
            f"topology.backend={backend or 'unspecified'!r}",
        )

    loaded = _load_openmm_bundle(check, submission_dir, manifest)
    if not loaded["success"]:
        return False, 0.0, loaded["message"]

    try:
        from openmm import LangevinIntegrator, Platform, unit
        from openmm.app import Simulation
    except Exception as exc:  # noqa: BLE001
        return False, 0.0, f"OpenMM import failed: {type(exc).__name__}: {exc}"

    system = loaded["system"]
    topology = loaded["topology"]
    state = loaded["state"]
    positions = loaded["positions"]

    try:
        integrator = LangevinIntegrator(
            300 * unit.kelvin, 1.0 / unit.picosecond, 2.0 * unit.femtoseconds
        )
        try:
            platform = Platform.getPlatformByName("CPU")
            simulation = Simulation(topology, system, integrator, platform)
        except Exception:  # noqa: BLE001
            platform = Platform.getPlatformByName("Reference")
            simulation = Simulation(topology, system, integrator, platform)

        try:
            box_vectors = state.getPeriodicBoxVectors()
            if box_vectors is not None:
                simulation.context.setPeriodicBoxVectors(*box_vectors)
        except Exception:  # noqa: BLE001
            pass
        simulation.context.setPositions(positions)
        rescanned = simulation.context.getState(getEnergy=True, getPositions=True)
        energy = rescanned.getPotentialEnergy().value_in_unit(
            unit.kilojoule_per_mole
        )
        if not math.isfinite(float(energy)):
            return False, 0.0, f"potential energy is not finite: {energy!r}"
        particle_count = max(1, system.getNumParticles())
        energy_per_particle = abs(float(energy)) / particle_count
        if energy_per_particle > _MAX_ABS_PREP_ENERGY_PER_PARTICLE_KJ_MOL:
            return (
                False,
                0.0,
                "OpenMM potential energy is finite but physically implausible: "
                f"{float(energy):.6g} kJ/mol "
                f"({energy_per_particle:.6g} kJ/mol/particle)",
            )
        rescanned_positions = rescanned.getPositions(asNumpy=True).value_in_unit(
            unit.nanometer
        )
        if not _positions_are_finite(rescanned_positions):
            return False, 0.0, "rescanned positions contain NaN or Inf"
    except Exception as exc:  # noqa: BLE001
        return False, 0.0, f"OpenMM energy rescan failed: {type(exc).__name__}: {exc}"

    return True, 1.0, f"OpenMM potential energy finite: {float(energy):.6g} kJ/mol"


def _check_minimization_report(check: DeterministicCheck,
                               submission_dir: Path,
                               manifest: dict,
                               metrics: dict, **_):
    report_rel = _manifest_artifact_path(
        manifest,
        check.minimization_report_manifest_path,
        "outputs.minimization_report",
    ) or check.minimization_report_path or "minimization_report.json"
    report_path = _resolve_relative(submission_dir, report_rel)
    if not report_path.is_file():
        return False, 0.0, f"minimization report not found: {report_path}"

    report = integrity.read_json_safe(report_path)

    def value(name: str) -> Any:
        return (
            integrity._safe_path(metrics, f"minimization.{name}")
            if integrity._safe_path(metrics, f"minimization.{name}") is not None
            else (
                integrity._safe_path(report, f"minimization.{name}")
                if integrity._safe_path(report, f"minimization.{name}") is not None
                else integrity._safe_path(report, name)
            )
        )

    required_true = [
        "attempted",
        "completed",
        "energy_is_finite",
        "positions_are_finite",
        "atom_count_preserved",
    ]
    issues = [name for name in required_true if value(name) is not True]
    for energy_field in ("energy_initial_kj_mol", "energy_final_kj_mol"):
        raw = value(energy_field)
        try:
            numeric = float(raw)
        except (TypeError, ValueError):
            issues.append(energy_field)
            continue
        if not math.isfinite(numeric):
            issues.append(energy_field)
        elif abs(numeric) > _MAX_ABS_PREP_TOTAL_ENERGY_KJ_MOL:
            atom_count_raw = value("atom_count") or value("particle_count")
            try:
                atom_count = max(1, int(atom_count_raw))
            except (TypeError, ValueError):
                atom_count = 1
            if (
                atom_count == 1
                or abs(numeric) / atom_count
                > _MAX_ABS_PREP_ENERGY_PER_PARTICLE_KJ_MOL
            ):
                issues.append(energy_field)
    if issues:
        return False, 0.0, f"minimization report failed required fields: {issues}"
    return True, 1.0, "minimization report confirms completed finite-energy minimization"


def _resolve_openmm_artifacts(check: DeterministicCheck,
                              submission_dir: Path,
                              manifest: dict) -> tuple[dict[str, Optional[Path]], list[str]]:
    roles: dict[str, Optional[Path]] = {
        "system_xml": None,
        "topology_pdb": None,
        "state_xml": None,
    }
    explicit_paths = {
        "system_xml": check.system_xml_manifest_path,
        "topology_pdb": check.topology_pdb_manifest_path,
        "state_xml": check.state_xml_manifest_path,
    }
    for role, manifest_path in explicit_paths.items():
        rel = _manifest_artifact_path(manifest, manifest_path, "") if manifest_path else None
        if rel:
            roles[role] = _resolve_relative(submission_dir, rel)

    topology_rels = _manifest_artifact_paths(
        manifest, check.topology_manifest_path, "outputs.topology",
    )
    for rel in topology_rels:
        path = _resolve_relative(submission_dir, rel)
        lower = path.name.lower()
        if roles["system_xml"] is None and (
            lower == "system.xml" or lower.endswith(".system.xml")
        ):
            roles["system_xml"] = path
        elif roles["state_xml"] is None and (
            lower == "state.xml" or lower.endswith(".state.xml")
        ):
            roles["state_xml"] = path
        elif roles["topology_pdb"] is None and (
            lower == "topology.pdb" or lower.endswith(".topology.pdb")
        ):
            roles["topology_pdb"] = path
        elif roles["topology_pdb"] is None and lower.endswith(".pdb"):
            roles["topology_pdb"] = path

    issues: list[str] = []
    for role, path in roles.items():
        if path is None:
            issues.append(f"{role} not listed in outputs.topology")
        elif not path.is_file():
            issues.append(f"{role} file not found: {path}")
    return roles, issues


def _load_openmm_bundle(check: DeterministicCheck,
                        submission_dir: Path,
                        manifest: dict) -> dict[str, Any]:
    artifacts, issues = _resolve_openmm_artifacts(check, submission_dir, manifest)
    if issues:
        return {"success": False, "message": "; ".join(issues)}

    try:
        from openmm import XmlSerializer
        from openmm.app import PDBFile
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "message": f"OpenMM import failed: {type(exc).__name__}: {exc}",
        }

    try:
        system = XmlSerializer.deserialize(artifacts["system_xml"].read_text())
        pdb = PDBFile(str(artifacts["topology_pdb"]))
        state = XmlSerializer.deserialize(artifacts["state_xml"].read_text())
        positions = state.getPositions()
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "message": f"OpenMM artifact load failed: {type(exc).__name__}: {exc}",
        }

    n_particles = system.getNumParticles()
    n_atoms = pdb.topology.getNumAtoms()
    try:
        n_positions = len(positions)
    except TypeError:
        n_positions = -1
    if n_particles != n_atoms or n_particles != n_positions:
        return {
            "success": False,
            "message": (
                "OpenMM artifact atom count mismatch: "
                f"system={n_particles}, topology={n_atoms}, state_positions={n_positions}"
            ),
        }

    return {
        "success": True,
        "message": f"OpenMM artifacts loaded with {n_particles} particles",
        "system": system,
        "topology": pdb.topology,
        "state": state,
        "positions": positions,
    }


def _positions_are_finite(array: Any) -> bool:
    try:
        for row in array:
            for value in row:
                if not math.isfinite(float(value)):
                    return False
    except TypeError:
        return False
    return True


def _residue_counts_from_pdb(path: Path) -> dict[str, int]:
    """Count unique residues by residue name in a PDB-like coordinate file."""
    residues: set[tuple[str, str, str, str]] = set()
    with path.open() as handle:
        for line in handle:
            if not line.startswith(("ATOM  ", "HETATM")):
                continue
            resname = line[17:21].strip().upper()
            parts = line.split()
            if len(parts) >= 4 and len(parts[3].strip()) > len(resname):
                resname = parts[3].strip().upper()
            if not resname and len(parts) >= 4:
                resname = parts[3].strip().upper()
            if not resname:
                continue
            chain_id = line[21:22].strip()
            resseq = line[22:26].strip()
            icode = line[26:27].strip()
            residues.add((chain_id, resseq, icode, resname))
    counts: dict[str, int] = {}
    for *_site, resname in residues:
        counts[resname] = counts.get(resname, 0) + 1
    return counts


def _chain_ids_from_pdb(path: Path) -> set[str]:
    """Collect non-empty chain IDs from ATOM/HETATM records."""
    chain_ids: set[str] = set()
    with path.open() as handle:
        for line in handle:
            if not line.startswith(("ATOM  ", "HETATM")):
                continue
            chain_id = line[21:22].strip()
            if not chain_id:
                parts = line.split()
                if len(parts) >= 5:
                    chain_id = parts[4].strip()
            if chain_id:
                chain_ids.add(chain_id)
    return chain_ids


def _check_structure_component_rescan(check: DeterministicCheck,
                                      submission_dir: Path,
                                      manifest: dict, **_):
    structure_rel = _manifest_artifact_path(
        manifest, check.structure_manifest_path, "outputs.prepared_structure",
    ) or check.structure_path or "prepared_structure.pdb"
    structure_path = _resolve_relative(submission_dir, structure_rel)
    return _check_component_counts_for_structure(check, structure_path)


def _check_minimized_structure_component_rescan(check: DeterministicCheck,
                                                submission_dir: Path,
                                                manifest: dict, **_):
    structure_rel = _manifest_artifact_path(
        manifest,
        check.minimized_structure_manifest_path or check.structure_manifest_path,
        "outputs.minimized_structure",
    ) or check.structure_path or "minimized_structure.pdb"
    structure_path = _resolve_relative(submission_dir, structure_rel)
    return _check_component_counts_for_structure(check, structure_path)


def _pdb_deuterium_records(path: Path) -> list[str]:
    records: list[str] = []
    with path.open() as handle:
        for line in handle:
            if not line.startswith(("ATOM", "HETATM")):
                continue
            if _is_deuterium_atom_record(line):
                atom_name = line[12:16].strip().upper()
                chain_id = line[21:22].strip()
                resseq = line[22:26].strip()
                resname = line[17:20].strip()
                records.append(f"{atom_name}:{resname}:{chain_id}:{resseq}")
    return records


def _is_deuterium_atom_record(line: str) -> bool:
    """Return True for experimental deuterium PDB atom records.

    Prefer the element column. If legacy PDB text lacks an element, only
    isotope-like atom names such as D, D1, D2, ... are treated as deuterium;
    deoxy nucleic atom names such as D5' or D3' are not.
    """
    if not line.startswith(("ATOM", "HETATM")):
        return False
    atom_name = line[12:16].strip().upper()
    element = line[76:78].strip().upper() if len(line) >= 78 else ""
    if element == "D":
        return True
    if element:
        return False
    return bool(_DEUTERIUM_FALLBACK_ATOM_NAME_RE.fullmatch(atom_name))


def _check_pdb_no_deuterium_atoms(check: DeterministicCheck,
                                  submission_dir: Path,
                                  manifest: dict, **_):
    structure_rel = _manifest_artifact_path(
        manifest, check.structure_manifest_path, "outputs.prepared_structure",
    ) or check.structure_path or "prepared_structure.pdb"
    structure_path = _resolve_relative(submission_dir, structure_rel)
    if not structure_path.is_file():
        return False, 0.0, f"structure file not found: {structure_path}"
    try:
        records = _pdb_deuterium_records(structure_path)
    except OSError as exc:
        return False, 0.0, f"could not read structure file: {exc}"
    if records:
        preview = ", ".join(records[:5])
        extra = "" if len(records) <= 5 else f", ... +{len(records) - 5} more"
        return False, 0.0, f"found {len(records)} deuterium atom record(s): {preview}{extra}"
    return True, 1.0, f"no deuterium atom records in {structure_path.name}"


def _check_component_counts_for_structure(check: DeterministicCheck,
                                          structure_path: Path):
    if not structure_path.is_file():
        return False, 0.0, f"structure file not found: {structure_path}"

    try:
        counts = _residue_counts_from_pdb(structure_path)
    except OSError as exc:
        return False, 0.0, f"could not read structure file: {exc}"

    def observed_count(resname: str) -> int:
        canonical = str(resname).upper()
        aliases = {
            str(alias).upper()
            for alias in (check.residue_aliases or {}).get(str(resname), [])
        }
        aliases.add(canonical)
        return sum(counts.get(alias, 0) for alias in aliases)

    issues: list[str] = []
    for resname, minimum in (check.min_residue_counts or {}).items():
        observed = observed_count(resname)
        if observed < int(minimum):
            issues.append(f"{resname}: observed {observed} < min {minimum}")
    for resname, maximum in (check.max_residue_counts or {}).items():
        observed = observed_count(resname)
        if observed > int(maximum):
            issues.append(f"{resname}: observed {observed} > max {maximum}")
    for resname, expected in (check.exact_residue_counts or {}).items():
        observed = observed_count(resname)
        if observed != int(expected):
            issues.append(f"{resname}: observed {observed} != expected {expected}")

    if issues:
        return False, 0.0, "; ".join(issues)
    requested = {
        "min": check.min_residue_counts or {},
        "max": check.max_residue_counts or {},
        "exact": check.exact_residue_counts or {},
    }
    return True, 1.0, f"component counts satisfied: {requested}"


def _check_pdb_residue_state(check: DeterministicCheck,
                             submission_dir: Path,
                             manifest: dict, **_):
    structure_rel = _manifest_artifact_path(
        manifest, check.structure_manifest_path, "outputs.prepared_structure",
    ) or check.structure_path or "prepared_structure.pdb"
    structure_path = _resolve_relative(submission_dir, structure_rel)
    if not structure_path.is_file():
        return False, 0.0, f"structure file not found: {structure_path}"
    if not check.residue_number or not check.required_residue_name:
        return False, 0.0, "residue_number and required_residue_name are required"

    expected_chain = (check.residue_chain or "").strip()
    expected_number = str(check.residue_number).strip()
    expected_icode = (check.insertion_code or "").strip()
    expected_resname = check.required_residue_name.strip().upper()
    required_atoms = {
        atom.strip().upper()
        for atom in (check.required_atom_names or [])
        if atom.strip()
    }
    forbidden_atoms = {
        atom.strip().upper()
        for atom in (check.forbidden_atom_names or [])
        if atom.strip()
    }

    residue_names: set[str] = set()
    atom_names: set[str] = set()
    try:
        with structure_path.open() as handle:
            for line in handle:
                if not line.startswith(("ATOM  ", "HETATM")):
                    continue
                chain_id = line[21].strip()
                resseq = line[22:26].strip()
                icode = line[26].strip()
                resname = line[17:20].strip().upper()
                atom_name = line[12:16].strip().upper()
                parts = line.split()
                if len(parts) >= 6 and not resseq:
                    chain_id = parts[4].strip()
                    resseq = parts[5].strip()
                    icode = ""
                if len(parts) >= 6 and resseq != expected_number:
                    # Fallback for permissive PDB-like fixtures where wider
                    # residue names shift fixed columns.
                    chain_id = parts[4].strip()
                    resseq = parts[5].strip()
                    icode = ""
                    resname = parts[3].strip().upper()
                    atom_name = parts[2].strip().upper()
                if chain_id != expected_chain:
                    continue
                if resseq != expected_number or icode != expected_icode:
                    continue
                residue_names.add(resname)
                atom_names.add(atom_name)
    except OSError as exc:
        return False, 0.0, f"could not read structure file: {exc}"

    residue_label = f"{expected_chain}:{expected_number}{expected_icode}"
    if not residue_names:
        return False, 0.0, f"residue {residue_label} not found"
    if expected_resname not in residue_names:
        return (
            False,
            0.0,
            f"residue {residue_label} names {sorted(residue_names)} "
            f"do not include {expected_resname}",
        )

    missing = sorted(required_atoms - atom_names)
    if missing:
        return False, 0.0, f"residue {residue_label} missing atoms {missing}"
    forbidden_present = sorted(forbidden_atoms & atom_names)
    if forbidden_present:
        return (
            False,
            0.0,
            f"residue {residue_label} contains forbidden atoms {forbidden_present}",
        )
    return (
        True,
        1.0,
        f"residue {residue_label} is {expected_resname} with required atoms present",
    )


def _check_assembly_identity(check: DeterministicCheck,
                             submission_dir: Path,
                             manifest: dict,
                             metrics: dict, **_):
    structure_rel = _manifest_artifact_path(
        manifest, check.structure_manifest_path, "outputs.prepared_structure",
    ) or check.structure_path or "prepared_structure.pdb"
    structure_path = _resolve_relative(submission_dir, structure_rel)
    if not structure_path.is_file():
        return False, 0.0, f"structure file not found: {structure_path}"

    issues: list[str] = []
    try:
        chain_ids = _chain_ids_from_pdb(structure_path)
    except OSError as exc:
        return False, 0.0, f"could not read structure file: {exc}"

    if check.required_assembly_id is not None:
        assembly_id = _read_submission_json_path(
            submission_dir,
            check.assembly_id_json_file or check.json_file or "metrics.json",
            check.assembly_id_json_path or check.json_path,
            metrics_default=metrics,
            manifest=manifest,
        )
        required_id = str(check.required_assembly_id)
        if assembly_id is None:
            issues.append("assembly_id path not found")
        elif str(assembly_id) != required_id:
            issues.append(f"assembly_id {assembly_id!r} != required {required_id!r}")

    mapping_path = check.chain_identity_json_path
    if mapping_path:
        mapping = _read_submission_json_path(
            submission_dir,
            check.chain_identity_json_file or "metrics.json",
            mapping_path,
            metrics_default=metrics,
            manifest=manifest,
        )
    else:
        mapping = None

    if mapping_path and not isinstance(mapping, list):
        issues.append(f"chain identity map at {mapping_path!r} is not a list")
        mapping_entries: list[dict[str, Any]] = []
    else:
        mapping_entries = [entry for entry in (mapping or []) if isinstance(entry, dict)]
        if mapping is not None and len(mapping_entries) != len(mapping):
            issues.append("chain identity map contains non-object entries")

    min_entries = int(check.min_mapping_entries or 0)
    if min_entries and len(mapping_entries) < min_entries:
        issues.append(
            f"chain identity map has {len(mapping_entries)} entries < min {min_entries}"
        )

    required_fields = check.required_mapping_fields or []
    for index, entry in enumerate(mapping_entries):
        missing: list[str] = []
        for field_spec in required_fields:
            alternatives = [part.strip() for part in str(field_spec).split("|")
                            if part.strip()]
            if not alternatives:
                continue
            if not any(str(entry.get(field, "")).strip() for field in alternatives):
                missing.append(field_spec)
        if missing:
            issues.append(f"mapping entry {index} missing fields {missing}")

    output_chain_ids = [
        str(entry.get("output_chain_id", "")).strip()
        for entry in mapping_entries
        if str(entry.get("output_chain_id", "")).strip()
    ]
    mapped_output_chain_ids = set(output_chain_ids)
    if mapped_output_chain_ids:
        chain_count = len(mapped_output_chain_ids)
        chain_count_label = "mapped output chain count"
    else:
        chain_count = len(chain_ids)
        chain_count_label = "structure chain count"
    if check.exact_chain_count is not None:
        expected = int(check.exact_chain_count)
        if chain_count != expected:
            issues.append(
                f"{chain_count_label} {chain_count} != expected {expected}"
            )
    if check.min_chain_count is not None:
        minimum = int(check.min_chain_count)
        if chain_count < minimum:
            issues.append(f"{chain_count_label} {chain_count} < min {minimum}")

    if check.min_distinct_output_chains is not None:
        distinct_count = len(mapped_output_chain_ids)
        minimum = int(check.min_distinct_output_chains)
        if distinct_count < minimum:
            issues.append(
                f"chain identity map covers {distinct_count} distinct output "
                f"chains < min {minimum}"
            )
    if check.require_unique_output_chains:
        duplicate_ids = sorted({
            chain_id for chain_id in output_chain_ids
            if output_chain_ids.count(chain_id) > 1
        })
        if duplicate_ids:
            issues.append(f"duplicate output_chain_id values {duplicate_ids}")

    if check.require_output_chains_in_structure:
        missing_chains = sorted(set(output_chain_ids) - chain_ids)
        if missing_chains:
            issues.append(
                f"mapped output chains absent from structure: {missing_chains}"
            )

    if check.required_operator_ids:
        observed_ops = {
            str(entry.get("operator_id", "")).strip()
            for entry in mapping_entries
            if str(entry.get("operator_id", "")).strip()
        }
        missing_ops = sorted(set(map(str, check.required_operator_ids)) - observed_ops)
        if missing_ops:
            issues.append(f"operator_id values missing from map: {missing_ops}")

    if issues:
        return False, 0.0, "; ".join(issues)
    return (
        True,
        1.0,
        f"assembly identity satisfied: chains={sorted(chain_ids)}, "
        f"mapping_entries={len(mapping_entries)}",
    )


def _check_candidate_selection(check: DeterministicCheck,
                               submission_dir: Path,
                               manifest: dict,
                               metrics: dict,
                               provenance: dict,
                               evidence: dict, **_):
    payloads = _candidate_selection_payloads(
        check,
        submission_dir=submission_dir,
        manifest=manifest,
        metrics=metrics,
        provenance=provenance,
        evidence=evidence,
    )
    if not payloads:
        return False, 0.0, "no source selection artifact or structured provenance found"

    issues: list[str] = []
    for label, payload in payloads:
        extracted = _extract_candidate_selection(payload)
        payload_issues = _candidate_selection_issues(check, extracted)
        if not payload_issues:
            details = []
            if extracted.get("candidate_ids"):
                details.append(f"candidate_ids={sorted(extracted['candidate_ids'])}")
            if extracted.get("model_rank") is not None:
                details.append(f"model_rank={extracted['model_rank']}")
            if extracted.get("selection_reason"):
                details.append("selection_reason=present")
            return True, 1.0, f"{label} satisfies candidate selection ({', '.join(details)})"
        issues.append(f"{label}: {'; '.join(payload_issues)}")

    return False, 0.0, "candidate selection mismatch: " + " | ".join(issues)


def _candidate_selection_payloads(
    check: DeterministicCheck,
    *,
    submission_dir: Path,
    manifest: dict,
    metrics: dict,
    provenance: dict,
    evidence: dict,
) -> list[tuple[str, dict[str, Any]]]:
    payloads: list[tuple[str, dict[str, Any]]] = []
    seen_paths: set[Path] = set()

    manifest_rel = _manifest_artifact_path(
        manifest,
        check.source_selection_manifest_path,
        "outputs.source_selection",
    )
    candidate_paths = [manifest_rel, check.source_selection_path or "source_selection.json"]
    for rel in candidate_paths:
        if not rel:
            continue
        path = _resolve_relative(submission_dir, rel)
        if path in seen_paths or not path.is_file():
            continue
        seen_paths.add(path)
        payload = integrity.read_json_safe(path)
        if payload:
            payloads.append((rel, payload))

    for label, payload in (
        ("provenance.json", provenance),
        ("metrics.json", metrics),
        ("evidence_report.json", evidence),
    ):
        extracted = _extract_candidate_selection(payload)
        if (
            extracted.get("has_structured_selection")
            or extracted.get("selected_structure_present")
        ):
            payloads.append((label, payload))

    return payloads


def _extract_candidate_selection(payload: dict[str, Any]) -> dict[str, Any]:
    candidate_ids: set[str] = set()
    model_rank: int | None = None
    selection_reason: str | None = None
    selected_structure_present = False
    has_structured_selection = False

    for path in (
        "selected_structure",
        "source_selection.selected_structure",
        "candidate_selection.selected_structure",
        "preparation.source_selection.selected_structure",
        "preparation.selected_structure",
    ):
        selected_structure = _safe_path_with_index(payload, path)
        if isinstance(selected_structure, dict):
            selected_structure_present = True
            candidate_ids.update(_candidate_ids_from_mapping(selected_structure))
            origin = selected_structure.get("origin") or {}
            model_rank = _first_int(
                origin.get("model_rank"),
                selected_structure.get("model_rank"),
                selected_structure.get("rank"),
                _one_based_index(origin.get("model_index")),
            )
            break

    for path in (
        "selection",
        "source_selection.selection",
        "candidate_selection.selection",
        "preparation.source_selection.selection",
        "candidate_selection",
        "source_selection",
        "preparation.source_selection",
    ):
        selection = _safe_path_with_index(payload, path)
        if isinstance(selection, dict):
            has_structured_selection = True
            candidate_ids.update(_candidate_ids_from_mapping(selection))
            if model_rank is None:
                model_rank = _first_int(
                    selection.get("model_rank"),
                    selection.get("selected_model_rank"),
                    selection.get("source_model_rank"),
                    selection.get("model_index"),
                    selection.get("source_model_index"),
                )
            if not selection_reason:
                selection_reason = _first_nonempty_string(
                    selection.get("selection_reason"),
                    selection.get("reason"),
                    selection.get("rationale"),
                )

    candidate_ids.update(_candidate_ids_from_mapping(payload))
    if model_rank is None:
        model_rank = _first_int(payload.get("model_rank"), payload.get("selected_model_rank"))
    if not selection_reason:
        selection_reason = _first_nonempty_string(
            payload.get("selection_reason"),
            payload.get("candidate_selection_reason"),
        )

    return {
        "candidate_ids": candidate_ids,
        "model_rank": model_rank,
        "selection_reason": selection_reason,
        "selected_structure_present": selected_structure_present,
        "has_structured_selection": has_structured_selection,
    }


def _candidate_selection_issues(
    check: DeterministicCheck,
    extracted: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    required_candidate = check.required_candidate_id
    if required_candidate:
        candidate_ids = extracted.get("candidate_ids") or set()
        if required_candidate not in candidate_ids:
            issues.append(
                f"candidate ids {sorted(candidate_ids)} do not include {required_candidate!r}"
            )

    required_rank = check.required_model_rank
    if required_rank is not None and extracted.get("model_rank") != int(required_rank):
        issues.append(
            f"model_rank {extracted.get('model_rank')!r} != {int(required_rank)}"
        )

    if check.require_selection_reason and not extracted.get("selection_reason"):
        issues.append("selection reason missing")

    if not (
        extracted.get("selected_structure_present")
        or extracted.get("has_structured_selection")
    ):
        issues.append("structured selected_structure/source_selection record missing")

    return issues


def _candidate_ids_from_mapping(mapping: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in (
        "structure_id",
        "candidate_id",
        "source_structure_id",
        "source_candidate_id",
        "selected_candidate_id",
    ):
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            ids.add(value.strip())
    return ids


def _first_int(*values: Any) -> int | None:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _one_based_index(value: Any) -> int | None:
    index = _first_int(value)
    if index is None:
        return None
    return index + 1


def _first_nonempty_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _check_rmsd_recompute(check: DeterministicCheck, submission_dir: Path,
                          metrics: dict, task_dir: Optional[Path], **_):
    if task_dir is None:
        return False, 0.0, "task_dir not supplied; cannot resolve reference"
    if not check.reference_pdb or not check.selection:
        return False, 0.0, "reference_pdb and selection required"
    prepared_rel = (manifest_value(submission_dir, "outputs.prepared_structure")
                    or "prepared_structure.pdb")
    prepared = _resolve_relative(submission_dir, prepared_rel)
    if not prepared.exists():
        return False, 0.0, f"prepared_structure not found: {prepared}"
    reference = (task_dir / check.reference_pdb).resolve()
    rmsd, msg = integrity.recompute_ligand_rmsd(
        prepared, reference, check.selection, check.align_selection,
    )
    if rmsd is None:
        return False, 0.0, msg
    claimed = _read_json_path(submission_dir, check, metrics_default=metrics)
    if check.json_path and claimed is None:
        return False, 0.0, (
            f"agent did not report rmsd at {check.json_path!r} ({msg})")
    if claimed is not None:
        try:
            claimed_f = float(claimed)
        except (TypeError, ValueError):
            return False, 0.0, f"claimed rmsd {claimed!r} not numeric ({msg})"
        if abs(claimed_f - rmsd) > check.tolerance_angstrom:
            return (False, 0.0,
                    f"claimed rmsd {claimed_f:.3f} differs from recomputed "
                    f"{rmsd:.3f} by more than {check.tolerance_angstrom} Å")
    bound = float(check.max_value) if check.max_value is not None else 0.5
    ok = rmsd <= bound
    return ok, (1.0 if ok else 0.0), (
        f"recomputed rmsd={rmsd:.3f} Å <= {bound} ({msg})")


def _check_metrics_caption_consistency(check: DeterministicCheck,
                                       submission_dir: Path,
                                       metrics: dict, evidence: dict, **_):
    captions = []
    for entry in evidence.get("figure_captions", []) or []:
        if isinstance(entry, dict) and "caption" in entry:
            captions.append(entry["caption"])
    if not captions:
        return False, 0.0, "evidence_report.figure_captions[] is empty"
    ok, issues = integrity.metrics_caption_consistency(
        metrics, captions, check.relative_tolerance,
    )
    if ok:
        return True, 1.0, f"checked {len(captions)} captions, all consistent"
    return False, 0.0, f"{len(issues)} caption(s) with mismatched values"


_DETERMINISTIC_DISPATCH = {
    "required_files": _check_required_files,
    "forbidden_files": _check_forbidden_files,
    "json_equals": _check_json_equals,
    "json_max": _check_json_max,
    "json_min": _check_json_min,
    "json_min_length": _check_json_min_length,
    "json_allowed_values": _check_json_allowed_values,
    "trajectory_rescan": _check_trajectory_rescan,
    "topology_solvent_rescan": _check_topology_solvent_rescan,
    "structure_component_rescan": _check_structure_component_rescan,
    "pdb_no_deuterium_atoms": _check_pdb_no_deuterium_atoms,
    "pdb_residue_state": _check_pdb_residue_state,
    "rmsd_recompute": _check_rmsd_recompute,
    "assembly_identity_check": _check_assembly_identity,
    "candidate_selection_check": _check_candidate_selection,
    "artifact_provenance_text": _check_artifact_provenance_text,
    "topology_artifact_bundle": _check_topology_artifact_bundle,
    "openmm_system_load": _check_openmm_system_load,
    "openmm_energy_rescan": _check_openmm_energy_rescan,
    "minimization_report_check": _check_minimization_report,
    "minimized_structure_component_rescan": _check_minimized_structure_component_rescan,
    "metrics_caption_consistency": _check_metrics_caption_consistency,
}


# ---------------------------------------------------------------------------
# Ground truth dispatch


def _run_ground_truth(check: GroundTruthCheck, submission_dir: Path,
                      task_dir: Path, evidence: dict) -> CheckResult:
    truth_payload = integrity.read_json_safe(task_dir / check.truth_file)
    expected = integrity._safe_path(truth_payload, check.truth_path)
    submission_payload = (
        evidence if check.submission_file == "evidence_report.json"
        else integrity.read_json_safe(submission_dir / check.submission_file)
    )
    submitted = integrity._safe_path(submission_payload, check.submission_path)

    if expected is None:
        return CheckResult(
            check_id=check.check_id, check_type="ground_truth",
            passed=False, score=0.0, weight=check.weight,
            message=f"truth path {check.truth_path!r} missing in {check.truth_file}",
        )
    if submitted is None:
        return CheckResult(
            check_id=check.check_id, check_type="ground_truth",
            passed=False, score=0.0, weight=check.weight,
            message=f"submission path {check.submission_path!r} missing in {check.submission_file}",
        )
    if check.allowed_values is not None and submitted not in check.allowed_values:
        return CheckResult(
            check_id=check.check_id, check_type="ground_truth",
            passed=False, score=0.0, weight=check.weight,
            message=f"submitted {submitted!r} not in allowed_values {check.allowed_values}",
        )
    ok = (submitted == expected)
    return CheckResult(
        check_id=check.check_id, check_type="ground_truth",
        passed=ok, score=(1.0 if ok else 0.0), weight=check.weight,
        message=f"submitted={submitted!r} expected={expected!r}",
    )


# ---------------------------------------------------------------------------
# Aggregation


def _assemble_axis_scores(
    task: Task,
    deterministic: list[CheckResult],
    ground_truth: list[CheckResult],
    llm_judge_payload: Optional[dict[str, Any]],
) -> dict[str, Optional[float]]:
    """Compute per-axis score for this task.

    Mapping:
    - primary axis: weighted mean of deterministic + ground_truth check scores
      (weights from the Task definition)
    - secondary axes: filled by LLM judge when available; otherwise None
      (axis is "not evaluable in deterministic mode" — distinct from 0.0).
    """
    axes: dict[str, Optional[float]] = {a: None for a in SCORE_AXES}
    primary_value = _weighted_mean(deterministic, ground_truth)
    axes[task.primary_score] = primary_value

    if llm_judge_payload:
        judge_scores = (llm_judge_payload.get("scores") or {})
        for axis in task.secondary_scores:
            v = judge_scores.get(axis)
            if isinstance(v, (int, float)):
                axes[axis] = max(0.0, min(1.0, float(v)))
    # task.secondary_scores axes without a judge value remain None.
    return axes


def _weighted_total(task: Task, axes: dict[str, Optional[float]]) -> float:
    """v1.0 unified weighted_total: 0.8 * primary + 0.2 * mean(secondaries)
    when secondaries exist; otherwise primary alone. Both yield 1.0 at
    perfect performance."""
    primary = axes.get(task.primary_score)
    if primary is None:
        primary = 0.0
    if not task.secondary_scores:
        return float(primary)
    secondary_values = [axes.get(a) for a in task.secondary_scores
                        if axes.get(a) is not None]
    if not secondary_values:
        # All secondary axes are unevaluable: fall back to primary alone.
        return float(primary)
    return 0.8 * float(primary) + 0.2 * statistics.fmean(secondary_values)


def _apply_status_modifier(
    status: str, weighted_total: float,
    axes: dict[str, Optional[float]],
    ground_truth: list[CheckResult],
) -> float:
    """Status semantics (v1.0):

    - completed: weighted_total stands.
    - partial: × 0.6.
    - blocked: 0.
    - failed: only if a ground-truth (e.g., guardrail-equivalent) check passes,
      keep score; else 0.
    """
    if status == "completed":
        return weighted_total
    if status == "partial":
        return weighted_total * 0.6
    if status == "blocked":
        return 0.0
    if status == "failed":
        if ground_truth and all(r.passed for r in ground_truth):
            return weighted_total
        return 0.0
    return weighted_total


def _score_status(weighted_total: float,
                  deterministic: list[CheckResult],
                  ground_truth: list[CheckResult]):
    if weighted_total >= 0.8:
        return "passed"
    any_passed = any(r.passed for r in deterministic + ground_truth)
    if any_passed:
        return "partial"
    return "failed"


def _build_llm_judge_record(task: Task, payload: Optional[dict[str, Any]]
                            ) -> LLMJudgeResult:
    if not payload:
        return LLMJudgeResult(enabled=False)
    return LLMJudgeResult(
        enabled=True,
        judge_model=payload.get("judge_model"),
        temperature=float(payload.get("temperature", 0.0)),
        rubric_version=payload.get("rubric_version", "1.0"),
        prompt_hash=payload.get("prompt_hash"),
        raw_response_file=payload.get("raw_response_file"),
        scores=dict(payload.get("scores") or {}),
        violations=list(payload.get("violations") or []),
    )


def _extract_runtime(manifest: dict, metrics: dict) -> RuntimeRecord:
    runtime = manifest.get("runtime") or metrics.get("runtime") or {}
    return RuntimeRecord(
        walltime_minutes=float(runtime.get("walltime_minutes", 0.0) or 0.0),
        tokens=int(runtime.get("tokens", 0) or 0),
        gpu_hours=float(runtime.get("gpu_hours", 0.0) or 0.0),
    )


# ---------------------------------------------------------------------------
# Run-level aggregation (replaces the buggy v0.1 _aggregate_scores)


def aggregate_run_scores(scores: list[dict[str, Any]],
                         tasks: list[dict[str, Any]],
                         ) -> dict[str, Any]:
    """Aggregate per-task scores into run-level summary.

    Axis score is the mean of per-task axis values across the tasks where the
    axis is in scope (primary OR secondary), excluding ``None``. Empty axes
    return ``None``, signalling "not evaluable for this run".

    Note: this replaces the v0.1 bug where every axis was divided by the
    total task count, capping perfect runs at 0.25 per axis.
    """
    by_axis: dict[str, Optional[float]] = {a: None for a in SCORE_AXES}
    for axis in SCORE_AXES:
        relevant: list[float] = []
        for score, task in zip(scores, tasks):
            in_scope = (
                task.get("primary_score") == axis
                or axis in (task.get("secondary_scores") or [])
            )
            if not in_scope:
                continue
            v = (score.get("scores") or {}).get(axis)
            if isinstance(v, (int, float)):
                relevant.append(float(v))
        by_axis[axis] = round(statistics.fmean(relevant), 4) if relevant else None

    totals = [float(s.get("weighted_total", 0.0) or 0.0) for s in scores]
    overall = round(statistics.fmean(totals), 4) if totals else 0.0
    n_failed = sum(1 for s in scores if s.get("status") != "passed")

    runtime = {
        "total_tokens": sum(int((s.get("runtime") or {}).get("tokens", 0) or 0)
                            for s in scores),
        "total_walltime_minutes": round(
            sum(float((s.get("runtime") or {}).get("walltime_minutes", 0.0) or 0.0)
                for s in scores), 4),
        "total_gpu_hours": round(
            sum(float((s.get("runtime") or {}).get("gpu_hours", 0.0) or 0.0)
                for s in scores), 4),
    }

    task_score_records = []
    for s in scores:
        det = s.get("deterministic_checks") or []
        gt = s.get("ground_truth_checks") or []
        passed_ids = [c.get("check_id") for c in det + gt if c.get("passed")]
        failed_ids = [c.get("check_id") for c in det + gt if not c.get("passed")]
        task_score_records.append({
            "task_id": s.get("task_id"),
            "status": s.get("status"),
            "weighted_total": s.get("weighted_total", 0.0),
            "scores": s.get("scores", {}),
            "passed_check_ids": passed_ids,
            "failed_check_ids": failed_ids,
            "integrity_warnings": s.get("integrity_warnings", []),
        })

    return {
        "n_tasks": len(scores),
        "n_failed_tasks": n_failed,
        "overall_score": overall,
        "scores": by_axis,
        "task_scores": task_score_records,
        "runtime": runtime,
    }


# ---------------------------------------------------------------------------
# Helpers


def _weighted_mean(deterministic: list[CheckResult],
                   ground_truth: list[CheckResult]) -> float:
    pool = deterministic + ground_truth
    if not pool:
        return 0.0
    num = sum(r.score * r.weight for r in pool)
    den = sum(r.weight for r in pool)
    if den == 0:
        return 0.0
    return num / den


def _resolve_relative(submission_dir: Path, rel: str) -> Path:
    return (submission_dir / rel).resolve()


def _recursive_text_fragments(value: Any) -> list[str]:
    fragments: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            fragments.append(str(key))
            fragments.extend(_recursive_text_fragments(item))
    elif isinstance(value, list):
        for item in value:
            fragments.extend(_recursive_text_fragments(item))
    elif value is not None:
        fragments.append(str(value))
    return fragments


def manifest_value(submission_dir: Path, dotted: str) -> Optional[Any]:
    manifest = integrity.read_json_safe(submission_dir / "manifest.json")
    return integrity._safe_path(manifest, dotted)


def _manifest_artifact_path(
    manifest: dict, preferred_path: Optional[str], fallback_path: str,
) -> Optional[str]:
    value = _safe_path_with_index(manifest, preferred_path or fallback_path)
    if isinstance(value, str):
        return value
    return None


def _manifest_artifact_paths(
    manifest: dict, preferred_path: Optional[str], fallback_path: str,
) -> list[str]:
    value = _safe_path_with_index(manifest, preferred_path or fallback_path)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _safe_path_with_index(obj: Any, dotted: str) -> Any:
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, list):
            try:
                idx = int(part)
            except ValueError:
                return None
            if idx < 0 or idx >= len(cur):
                return None
            cur = cur[idx]
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _read_json_path(submission_dir: Path, check: DeterministicCheck,
                    metrics_default: dict,
                    manifest: Optional[dict] = None) -> Any:
    """Fetch the value at ``check.json_path`` from the right submission file.

    json_file defaults to ``metrics.json``. ``manifest.json`` is special-cased
    so callers can pass it pre-loaded. Other files are loaded fresh.
    """
    if not check.json_path:
        return None
    return _read_submission_json_path(
        submission_dir,
        check.json_file or "metrics.json",
        check.json_path,
        metrics_default=metrics_default,
        manifest=manifest,
    )


def _read_submission_json_path(submission_dir: Path, json_file: str | None,
                               json_path: str | None, metrics_default: dict,
                               manifest: Optional[dict] = None) -> Any:
    if not json_path:
        return None
    target_file = json_file or "metrics.json"
    if target_file == "manifest.json" and manifest is not None:
        payload = manifest
    elif target_file == "metrics.json":
        payload = metrics_default
    else:
        # paths in check.json_file are relative to submission_dir; allow
        # bare filenames (manifest.json, evidence_report.json, etc.) and
        # also the "submission/<file>" prefix used in v0.1 task.json.
        rel = target_file
        if rel.startswith("submission/"):
            rel = rel.split("/", 1)[1]
        payload = integrity.read_json_safe(submission_dir / rel)
    return integrity._safe_path(payload, json_path)


__all__ = [
    "score_submission",
    "aggregate_run_scores",
]
