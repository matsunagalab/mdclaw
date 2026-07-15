"""Integrity-layer regressions for prep benchmark submissions."""

from __future__ import annotations

from pathlib import Path

from mdclaw.benchmark import normalization, scoring
from mdclaw.benchmark.validation import load_task
from tests.test_benchmark import _fake_submissions


_BENCH_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "mdprepbench" / "tasks"
TASK_ID = "P11_prep_site_protonation_t4l_glu11"


def _write_normalized_p11(task, submission_dir: Path, *, run_id: str) -> None:
    raw_dir = submission_dir.parent / "raw_submission"
    _fake_submissions.GENERATORS[TASK_ID](
        raw_dir, run_id=run_id, mode="honest",
    )
    result = normalization.normalize_preparation_submission(
        task=task,
        raw_submission_dir=raw_dir,
        normalized_submission_dir=submission_dir,
        run_id=run_id,
    )
    assert result["success"], result


def _write_template_like_p11(task, submission_dir: Path, *, run_id: str) -> None:
    # Mutate the evaluator-normalized fixture so this remains a scorer-layer
    # integrity-policy test rather than an agent submission example.
    _write_normalized_p11(task, submission_dir, run_id=run_id)
    (submission_dir / "prepared_structure.pdb").write_text(
        "REMARK template placeholder\nEND\n"
    )


def test_warn_policy_penalizes_template_like_prep_submission(tmp_path: Path):
    task_dir = _BENCH_ROOT / TASK_ID
    task = load_task(task_dir / "task.json")
    task.scoring.integrity_policy = "warn"
    submission_dir = tmp_path / "submission"
    _write_template_like_p11(task, submission_dir, run_id="warn_phase")

    score = scoring.score_submission(
        task, submission_dir, run_id="warn_phase", task_dir=task_dir,
    )

    assert score.deterministic_checks
    # The undersized prepared structure trips the status_artifact_floor
    # integrity check under warn policy: a warning plus a penalty, not a zero.
    assert score.integrity_warnings
    assert 0.0 < score.weighted_total < 1.0


def test_reject_policy_clamps_template_like_prep_submission(tmp_path: Path):
    task_dir = _BENCH_ROOT / TASK_ID
    task = load_task(task_dir / "task.json")
    task.scoring.integrity_policy = "reject"
    submission_dir = tmp_path / "submission"
    _write_template_like_p11(task, submission_dir, run_id="reject_phase")

    score = scoring.score_submission(
        task, submission_dir, run_id="reject_phase", task_dir=task_dir,
    )

    assert score.integrity_warnings
    assert score.weighted_total == 0.0
    assert score.status == "failed"


def test_reject_policy_leaves_honest_prep_submission_untouched(tmp_path: Path):
    task_dir = _BENCH_ROOT / TASK_ID
    task = load_task(task_dir / "task.json")
    task.scoring.integrity_policy = "reject"
    submission_dir = tmp_path / "submission"
    _write_normalized_p11(task, submission_dir, run_id="honest")

    score = scoring.score_submission(
        task, submission_dir, run_id="honest", task_dir=task_dir,
    )

    artifact_warnings = [w for w in score.integrity_warnings if w.startswith("[")]
    assert artifact_warnings == []
    assert score.weighted_total == 1.0
