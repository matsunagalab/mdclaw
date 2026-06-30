"""StudyBench anti-gaming / fabrication coverage.

These tests lock down the Tier-1 hard-fail gates that bind a scientific-answer
submission to real, loadable, correctly-built comparative MD. They are the
StudyBench analogue of ``test_scoring_fabrication.py`` for the prep suite: an
honest fixture scores high, while gamed submissions (garbage trajectories, no
real mutation, out-of-pool citations, undersized evidence) are clamped or
rejected.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from mdclaw.benchmark import cli
from tests.test_benchmark import _fake_study_submissions as fakes


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"
COMPARATIVE_TASKS = [
    "S01_stability_t4l_l99a",
    "S02_ppi_hotspot_barnase_d39a",
    "S04_stability_nuclease_h124l",
    "S05_affinity_t4l_l99a_alkylbenzene",
]


def _make(tmp_path: Path, task_id: str, mode: str = "honest") -> Path:
    sub_dir = tmp_path / task_id / "submission"
    fakes.make_study_submission(sub_dir, run_id="fab", mode=mode, task_id=task_id)
    return sub_dir


def _score(task_id: str, sub_dir: Path) -> dict:
    task_file = DATASET_DIR / "tasks" / task_id / "task.json"
    scored = cli.score_benchmark_submission(
        task_file=str(task_file),
        submission_dir=str(sub_dir),
        run_id="fab",
        output_file=str(sub_dir.parent / "score.json"),
    )
    assert scored["success"], scored
    return scored["score"]


@pytest.mark.parametrize("task_id", COMPARATIVE_TASKS)
def test_honest_comparative_submission_passes(tmp_path: Path, task_id: str):
    score = _score(task_id, _make(tmp_path, task_id))
    assert score["weighted_total"] == pytest.approx(1.0)
    assert score["status"] == "passed"
    assert not score["integrity_warnings"]


@pytest.mark.parametrize("task_id", COMPARATIVE_TASKS)
def test_garbage_trajectory_is_hard_failed(tmp_path: Path, task_id: str):
    """A DCD-magic header over junk bytes is not loadable MD -> rescan clamps."""
    sub_dir = _make(tmp_path, task_id)
    (sub_dir / "trajectories/wt.dcd").write_bytes(
        b"\x54\x00\x00\x00CORD" + b"not real md frames\n" * 64
    )
    score = _score(task_id, sub_dir)
    assert score["weighted_total"] == 0.0
    assert score["status"] == "failed"


@pytest.mark.parametrize("task_id", COMPARATIVE_TASKS)
def test_missing_mutation_is_hard_failed(tmp_path: Path, task_id: str):
    """Copying the WT system over the mutant means no real mutation was built."""
    sub_dir = _make(tmp_path, task_id)
    shutil.copy(sub_dir / "topology/wt.pdb", sub_dir / "topology/mutant.pdb")
    shutil.copy(sub_dir / "trajectories/wt.dcd", sub_dir / "trajectories/mutant.dcd")
    score = _score(task_id, sub_dir)
    assert score["weighted_total"] == 0.0
    assert score["status"] == "failed"
    failed = {c["check_id"] for c in score["deterministic_checks"] if not c["passed"]}
    assert any(cid.startswith("paired") for cid in failed), failed


@pytest.mark.parametrize("task_id", COMPARATIVE_TASKS)
def test_real_md_wrong_direction_scores_zero_answer(tmp_path: Path, task_id: str):
    """Real correct-mutation MD but the wrong direction -> gates pass, answer 0."""
    sub_dir = _make(tmp_path, task_id)
    evidence = json.loads((sub_dir / "evidence_report.json").read_text())
    truth = json.loads(
        (DATASET_DIR / "tasks" / task_id / "truth" / "experimental_truth.json").read_text()
    )
    allowed = {
        "S01_stability_t4l_l99a": ["destabilizing", "stabilizing", "neutral"],
        "S02_ppi_hotspot_barnase_d39a": [
            "weakened_binding", "strengthened_binding", "neutral",
        ],
        "S04_stability_nuclease_h124l": [
            "destabilizing", "stabilizing", "neutral",
        ],
        "S05_affinity_t4l_l99a_alkylbenzene": [
            "stronger_binding", "weaker_binding", "similar",
        ],
    }[task_id]
    wrong = next(v for v in allowed if v != truth["expected_direction"])
    evidence["effect"]["direction"] = wrong
    (sub_dir / "evidence_report.json").write_text(json.dumps(evidence))
    score = _score(task_id, sub_dir)
    assert score["scores"]["scientific_answer"] == pytest.approx(0.0)
    # gates passed (real MD + real mutation), so this is a wrong answer, not a
    # hard failure: status is partial, not failed.
    assert score["status"] == "partial"


def test_out_of_pool_citation_is_rejected(tmp_path: Path):
    sub_dir = _make(tmp_path, "S01_stability_t4l_l99a")
    evidence = json.loads((sub_dir / "evidence_report.json").read_text())
    evidence["evidence"]["citations"] = [
        {"pool": "NotARealPool", "record_id": "x", "doi": "10.0000/fake"}
    ]
    (sub_dir / "evidence_report.json").write_text(json.dumps(evidence))
    score = _score("S01_stability_t4l_l99a", sub_dir)
    assert score["weighted_total"] == 0.0
    assert score["integrity_warnings"]


def test_undersized_evidence_report_is_rejected(tmp_path: Path):
    sub_dir = _make(tmp_path, "S01_stability_t4l_l99a")
    (sub_dir / "evidence_report.json").write_text('{"effect": {"direction": "x"}}')
    score = _score("S01_stability_t4l_l99a", sub_dir)
    assert score["weighted_total"] == 0.0
    assert score["integrity_warnings"]


def test_llm_judge_rubric_scores_fill_secondary_axis(tmp_path: Path):
    """Regression: the judge reports rubric-keyed scores; the scorer must
    aggregate them into the task's secondary axis (previously they were read by
    axis name and silently dropped, zeroing the qualitative dimension)."""
    task_id = "S01_stability_t4l_l99a"
    sub_dir = _make(tmp_path, task_id)
    judge_file = sub_dir.parent / "judge.json"
    judge_file.write_text(json.dumps({
        "enabled": True,
        "judge_model": "test",
        "scores": {"confidence_calibration": 0.8, "overclaim_detection": 0.6},
    }))
    task_file = DATASET_DIR / "tasks" / task_id / "task.json"
    scored = cli.score_benchmark_submission(
        task_file=str(task_file),
        submission_dir=str(sub_dir),
        run_id="fab",
        output_file=str(sub_dir.parent / "score.json"),
        llm_judge_file=str(judge_file),
    )
    assert scored["success"], scored
    score = scored["score"]
    # mean(0.8, 0.6) = 0.7 aggregated into evidence_communication
    assert score["scores"]["evidence_communication"] == pytest.approx(0.7)
    # weighted_total = 0.8 * primary(1.0) + 0.2 * 0.7
    assert score["weighted_total"] == pytest.approx(0.94)
