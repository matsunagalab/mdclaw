"""Deterministic check execution and run-level aggregation for v1.0.

Public entry points:

- :func:`score_submission` — runs every check on a submission and produces a
  :class:`Score` (still a pydantic model; serialized to score.json by callers).
- :func:`aggregate_run_scores` — combines per-task score.json files into a
  run-level summary, using axis aggregation that divides by the number of
  tasks where the axis is in scope (NOT total task count).
"""

from __future__ import annotations

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
            evidence=evidence, task_dir=task_dir,
        )
        deterministic_results.append(result)

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

    score_status = _score_status(weighted_total, deterministic_results,
                                 ground_truth_results)
    if manifest_status == "blocked":
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
            evidence=evidence, task_dir=task_dir,
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


def _check_structure_component_rescan(check: DeterministicCheck,
                                      submission_dir: Path,
                                      manifest: dict, **_):
    structure_rel = _manifest_artifact_path(
        manifest, check.structure_manifest_path, "outputs.prepared_structure",
    ) or check.structure_path or "prepared_structure.pdb"
    structure_path = _resolve_relative(submission_dir, structure_rel)
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
    "pdb_residue_state": _check_pdb_residue_state,
    "rmsd_recompute": _check_rmsd_recompute,
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
    n_failed = sum(1 for s in scores if s.get("status") in {"failed", "errored"})

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
    target_file = check.json_file or "metrics.json"
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
    return integrity._safe_path(payload, check.json_path)


__all__ = [
    "score_submission",
    "aggregate_run_scores",
]
