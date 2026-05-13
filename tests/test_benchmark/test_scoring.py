"""Scoring arithmetic tests for v1.0.

These cover the three places where v0.1 was wrong:
- weighted_total formula and ceiling
- per-axis aggregation divisor
- manifest.status semantics
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mdclaw.benchmark import scoring, validation
from mdclaw.benchmark.models import (
    DeterministicCheck,
    GroundTruthCheck,
    Task,
    TaskScoring,
)


def _make_task(primary, secondaries=None, det_checks=None, gt_checks=None):
    return Task(
        schema_version="1.0",
        task_id="t",
        category="engine_sanity",
        primary_score=primary,
        secondary_scores=secondaries or [],
        execution_mode="lite",
        time_limit_minutes=30,
        scoring=TaskScoring(
            deterministic_checks=det_checks or [],
            ground_truth_checks=gt_checks or [],
        ),
        task_intent="x",
    )


def _write_submission(tmp: Path, manifest: dict, metrics: dict | None = None,
                      provenance: dict | None = None,
                      evidence: dict | None = None):
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "manifest.json").write_text(json.dumps(manifest))
    if metrics is not None:
        (tmp / "metrics.json").write_text(json.dumps(metrics))
    if provenance is not None:
        (tmp / "provenance.json").write_text(json.dumps(provenance))
    if evidence is not None:
        (tmp / "evidence_report.json").write_text(json.dumps(evidence))


def test_weighted_total_no_secondary_caps_at_one(tmp_path: Path):
    """A perfect task with no secondaries should hit weighted_total = 1.0,
    not v0.1's 0.8 ceiling."""
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(check_id="ok", check_type="json_equals",
                                        json_path="execution.completed",
                                        equals=True, weight=1.0)],
    )
    _write_submission(tmp_path,
                      manifest={"task_id": "t", "status": "completed",
                                "outputs": {"trajectories": ["traj.dcd"]}},
                      metrics={"execution": {"completed": True}})
    score = scoring.score_submission(task, tmp_path)
    assert score.scores["execution"] == 1.0
    assert score.weighted_total == 1.0


def test_weighted_total_with_secondary_uses_blended_formula(tmp_path: Path):
    """secondaries pull weighted_total = 0.8 * primary + 0.2 * mean(secondary).
    With LLM judge supplying secondary=1.0, weighted_total should still be 1.0
    (not 0.8) — a perfect performance reaches 1.0 regardless of secondary
    presence."""
    task = _make_task(
        primary="execution",
        secondaries=["evidence_communication"],
        det_checks=[DeterministicCheck(check_id="ok", check_type="json_equals",
                                        json_path="execution.completed",
                                        equals=True, weight=1.0)],
    )
    _write_submission(tmp_path,
                      manifest={"task_id": "t", "status": "completed",
                                "outputs": {"trajectories": ["traj.dcd"]}},
                      metrics={"execution": {"completed": True}})
    score = scoring.score_submission(
        task, tmp_path,
        llm_judge_payload={"scores": {"evidence_communication": 1.0}},
    )
    assert score.weighted_total == pytest.approx(1.0)


def test_weighted_total_falls_back_when_secondary_unevaluable(tmp_path: Path):
    """No LLM judge file: secondary axis is None and falls out of the
    weighted_total formula. weighted_total reduces to primary alone."""
    task = _make_task(
        primary="execution",
        secondaries=["evidence_communication"],
        det_checks=[DeterministicCheck(check_id="ok", check_type="json_equals",
                                        json_path="execution.completed",
                                        equals=True, weight=1.0)],
    )
    _write_submission(tmp_path,
                      manifest={"task_id": "t", "status": "completed",
                                "outputs": {"trajectories": ["traj.dcd"]}},
                      metrics={"execution": {"completed": True}})
    score = scoring.score_submission(task, tmp_path)
    assert score.scores["evidence_communication"] is None
    assert score.weighted_total == 1.0


def test_status_partial_multiplies_weighted_total_by_0_6(tmp_path: Path):
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(check_id="ok", check_type="json_equals",
                                        json_path="execution.completed",
                                        equals=True, weight=1.0)],
    )
    _write_submission(tmp_path,
                      manifest={"task_id": "t", "status": "partial",
                                "outputs": {"trajectories": ["traj.dcd"]}},
                      metrics={"execution": {"completed": True}})
    score = scoring.score_submission(task, tmp_path)
    assert score.weighted_total == pytest.approx(0.6)


def test_status_blocked_zeros_weighted_total(tmp_path: Path):
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(check_id="ok", check_type="json_equals",
                                        json_path="execution.completed",
                                        equals=True, weight=1.0)],
    )
    _write_submission(tmp_path, manifest={"task_id": "t", "status": "blocked"},
                      metrics={"execution": {"completed": True}})
    score = scoring.score_submission(task, tmp_path)
    assert score.weighted_total == 0.0
    assert score.status == "failed"
    assert any("does not allow blocked" in warning
               for warning in score.integrity_warnings)


def test_validate_submission_rejects_disallowed_blocked_status(tmp_path: Path):
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(check_id="ok", check_type="json_equals",
                                        json_path="execution.completed",
                                        equals=True, weight=1.0)],
    )
    task_file = tmp_path / "task.json"
    task_file.write_text(task.model_dump_json())
    submission_dir = tmp_path / "submission"
    _write_submission(
        submission_dir,
        manifest={"task_id": "t", "status": "blocked"},
        metrics={"execution": {"completed": False}},
    )

    result = validation.validate_submission(task_file, submission_dir)
    assert result["success"] is False
    assert any("does not allow blocked" in err for err in result["errors"])


def test_status_failed_keeps_score_when_guardrail_passes(tmp_path: Path):
    """T02-style intentional refusal: status='failed' but a guardrail-equivalent
    ground_truth check passes → keep the score."""
    truth_dir = tmp_path / "task" / "truth"
    truth_dir.mkdir(parents=True)
    (truth_dir / "expected_guardrail.json").write_text(
        json.dumps({"expected_guardrail_code": "metal_containing_ligand_blocked"})
    )
    task = _make_task(
        primary="preparation",
        gt_checks=[GroundTruthCheck(
            check_id="g", truth_file="truth/expected_guardrail.json",
            truth_path="expected_guardrail_code",
            submission_file="metrics.json",
            submission_path="preparation.guardrail_code",
        )],
    )
    sub_dir = tmp_path / "submission"
    _write_submission(
        sub_dir, manifest={"task_id": "t", "status": "failed"},
        metrics={"preparation": {"guardrail_code": "metal_containing_ligand_blocked"}},
    )
    score = scoring.score_submission(task, sub_dir, task_dir=tmp_path / "task")
    assert score.weighted_total == 1.0


def test_status_failed_zeros_when_no_ground_truth_passes(tmp_path: Path):
    """Random failed status with no compensating ground_truth → weighted = 0."""
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(check_id="ok", check_type="json_equals",
                                        json_path="execution.completed",
                                        equals=True, weight=1.0)],
    )
    _write_submission(tmp_path, manifest={"task_id": "t", "status": "failed"},
                      metrics={"execution": {"completed": True}})
    score = scoring.score_submission(task, tmp_path)
    assert score.weighted_total == 0.0


def test_axis_aggregation_divides_by_in_scope_tasks_only():
    """The v0.1 bug: dividing by total task count caps perfect runs at
    1/n_tasks per axis. v1.0 must divide by tasks where the axis is in scope.
    """
    scores = [
        {"weighted_total": 1.0,
         "scores": {"execution": 1.0, "preparation": None,
                    "scientific_answer": None, "evidence_communication": None},
         "runtime": {}},
        {"weighted_total": 1.0,
         "scores": {"execution": 1.0, "preparation": None,
                    "scientific_answer": None, "evidence_communication": None},
         "runtime": {}},
        {"weighted_total": 1.0,
         "scores": {"execution": None, "preparation": 1.0,
                    "scientific_answer": None, "evidence_communication": None},
         "runtime": {}},
    ]
    tasks = [
        {"task_id": "T01", "primary_score": "execution", "secondary_scores": []},
        {"task_id": "T02", "primary_score": "execution", "secondary_scores": []},
        {"task_id": "T03", "primary_score": "preparation", "secondary_scores": []},
    ]
    aggregate = scoring.aggregate_run_scores(scores, tasks)
    assert aggregate["scores"]["execution"] == pytest.approx(1.0)
    assert aggregate["scores"]["preparation"] == pytest.approx(1.0)
    assert aggregate["scores"]["scientific_answer"] is None
    assert aggregate["scores"]["evidence_communication"] is None
    assert aggregate["overall_score"] == pytest.approx(1.0)


def test_required_files_check_fails_on_missing(tmp_path: Path):
    task = _make_task(
        primary="evidence_communication",
        det_checks=[DeterministicCheck(check_id="rf", check_type="required_files",
                                        required_outputs=["methods.md", "evidence_report.json"],
                                        weight=1.0)],
    )
    _write_submission(tmp_path, manifest={"task_id": "t", "status": "completed"},
                      evidence={"summary": "x"})
    score = scoring.score_submission(task, tmp_path)
    failed = next(c for c in score.deterministic_checks if c.check_id == "rf")
    assert failed.passed is False
    assert "methods.md" in failed.message


def test_json_min_length_check(tmp_path: Path):
    task = _make_task(
        primary="evidence_communication",
        det_checks=[DeterministicCheck(check_id="figs", check_type="json_min_length",
                                        json_file="manifest.json",
                                        json_path="outputs.figures",
                                        min_length=2, weight=1.0)],
    )
    _write_submission(
        tmp_path,
        manifest={"task_id": "t", "status": "completed",
                  "outputs": {"figures": ["a.png", "b.png", "c.png"]}},
    )
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is True


def test_forbidden_files_check(tmp_path: Path):
    task = _make_task(
        primary="preparation",
        det_checks=[DeterministicCheck(
            check_id="no_bad_file",
            check_type="forbidden_files",
            forbidden_outputs=["prepared_structure.pdb"],
            weight=1.0,
        )],
    )
    _write_submission(tmp_path, manifest={"task_id": "t", "status": "completed"})
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is True

    (tmp_path / "prepared_structure.pdb").write_text("END\n")
    score_with_file = scoring.score_submission(task, tmp_path)
    failed = score_with_file.deterministic_checks[0]
    assert failed.passed is False
    assert "forbidden files present" in failed.message


def test_trajectory_rescan_uses_manifest_outputs(tmp_path: Path, monkeypatch):
    observed = {}

    def fake_rescan(traj_path: Path, top_path: Path):
        observed["traj_path"] = traj_path
        observed["top_path"] = top_path
        return 8, False, "fake loaded 8 frames"

    monkeypatch.setattr(scoring.integrity, "rescan_trajectory_for_nan", fake_rescan)
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(
            check_id="traj",
            check_type="trajectory_rescan",
            trajectory_path="../work/default_traj.dcd",
            topology_path="../work/default_topology.pdb",
            trajectory_manifest_path="outputs.trajectories.0",
            topology_manifest_path="outputs.topology.0",
            require_min_frames=4,
            weight=1.0,
        )],
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {
                "trajectories": ["mdcrow/traj.dcd"],
                "topology": ["mdcrow/topology.pdb"],
            },
        },
    )
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is True
    assert observed["traj_path"] == (tmp_path / "mdcrow/traj.dcd").resolve()
    assert observed["top_path"] == (tmp_path / "mdcrow/topology.pdb").resolve()


def test_topology_solvent_rescan_requires_explicit_water(tmp_path: Path):
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(
            check_id="explicit_water",
            check_type="topology_solvent_rescan",
            topology_manifest_path="outputs.topology.0",
            water_residue_names=["HOH", "WAT"],
            min_water_residues=2,
            weight=1.0,
        )],
    )
    (tmp_path / "system.topology.pdb").write_text(
        "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\n"
        "HETATM    2  O   HOH B   2       1.000   0.000   0.000  1.00  0.00           O\n"
        "HETATM    3  O   HOH B   3       2.000   0.000   0.000  1.00  0.00           O\n"
        "END\n"
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"topology": ["system.topology.pdb"]},
        },
    )
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is True
    assert "found 2 water residues" in score.deterministic_checks[0].message


def test_topology_solvent_rescan_fails_for_implicit_topology(tmp_path: Path):
    task = _make_task(
        primary="execution",
        det_checks=[DeterministicCheck(
            check_id="explicit_water",
            check_type="topology_solvent_rescan",
            topology_manifest_path="outputs.topology.0",
            min_water_residues=1,
            weight=1.0,
        )],
    )
    (tmp_path / "system.topology.pdb").write_text(
        "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\n"
        "END\n"
    )
    _write_submission(
        tmp_path,
        manifest={
            "task_id": "t",
            "status": "completed",
            "outputs": {"topology": ["system.topology.pdb"]},
        },
    )
    score = scoring.score_submission(task, tmp_path)
    assert score.deterministic_checks[0].passed is False
    assert "require >= 1" in score.deterministic_checks[0].message


def test_ground_truth_check_uses_separate_truth_file(tmp_path: Path):
    truth_dir = tmp_path / "task" / "truth"
    truth_dir.mkdir(parents=True)
    (truth_dir / "experimental_truth.json").write_text(
        json.dumps({"expected_direction": "destabilizing"})
    )
    task = _make_task(
        primary="scientific_answer",
        gt_checks=[GroundTruthCheck(
            check_id="dir", truth_path="expected_direction",
            submission_path="effect.direction", weight=1.0,
        )],
    )
    sub_dir = tmp_path / "submission"
    _write_submission(
        sub_dir, manifest={"task_id": "t", "status": "completed"},
        evidence={"effect": {"direction": "destabilizing"}},
    )
    score = scoring.score_submission(task, sub_dir, task_dir=tmp_path / "task")
    gt = score.ground_truth_checks[0]
    assert gt.passed is True

    # Wrong answer → ground_truth fails and weighted_total drops.
    sub_dir2 = tmp_path / "submission_wrong"
    _write_submission(
        sub_dir2, manifest={"task_id": "t", "status": "completed"},
        evidence={"effect": {"direction": "stabilizing"}},
    )
    score_wrong = scoring.score_submission(
        task, sub_dir2, task_dir=tmp_path / "task")
    assert score_wrong.ground_truth_checks[0].passed is False
    assert score_wrong.weighted_total == 0.0
