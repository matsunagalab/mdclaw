"""Score the committed MDStudyBench reference example.

This locks in a committed, inspectable submission so an external party can
reproduce and self-validate the StudyBench scorer without reverse-engineering it
(the prep suite has its baseline runners; this is the study analogue). The S03
evidence bundle is a dry-run task, so the example ships no trajectories and needs
no GPU.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mdclaw.benchmark import cli


REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = (
    REPO_ROOT
    / "benchmarks"
    / "mdstudybench"
    / "examples"
    / "S03_ppi_evidence_bundle_barnase"
)
TASK_FILE = (
    REPO_ROOT
    / "benchmarks"
    / "mdstudybench"
    / "tasks"
    / "S03_ppi_evidence_bundle_barnase"
    / "task.json"
)


def test_committed_reference_example_scores_full(tmp_path: Path):
    scored = cli.score_benchmark_submission(
        task_file=str(TASK_FILE),
        submission_dir=str(EXAMPLE_DIR / "submission"),
        run_id="reference",
        output_file=str(tmp_path / "score.json"),
    )
    assert scored["success"], scored
    score = scored["score"]
    assert score["status"] == "passed"
    assert score["weighted_total"] == pytest.approx(1.0)
    assert not score["integrity_warnings"]
    assert score["scores"]["evidence_communication"] == pytest.approx(1.0)
