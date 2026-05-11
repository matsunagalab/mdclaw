"""End-to-end scoring tests that exercise the integrity layer against
Haiku-style fabricated submissions.

These regression tests pin the behavior that:
- under "warn" policy, a fabricated submission still loses ground via the
  -0.05/check (capped -0.2) integrity penalty
- under "reject" policy, the same submission is clamped to weighted_total=0
- a well-formed honest submission is unaffected by either policy
"""

from __future__ import annotations

import json
from pathlib import Path

from mdclaw.benchmark import scoring
from mdclaw.benchmark.validation import load_task


_BENCH_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "mdagentbench" / "tasks"


def _write_fabricated_t06(submission_dir: Path):
    """Reproduce the shape of the Haiku 20260511 T06 submission: matches the
    truth string but supplies no real evidence (no citations, template-bytes
    evidence_report)."""
    submission_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "task_id": "T06_answer_stability_t4l_l99a",
        "run_id": "fabricated",
        "status": "completed",
        "outputs": {
            "metrics": "metrics.json",
            "provenance": "provenance.json",
            "evidence_report": "evidence_report.json",
        },
        "limitations": [],
        "errors": [],
    }
    metrics = {"answer": {"effect_direction": "destabilizing"}}
    # Mirrors Haiku's tiny ~360-byte evidence_report — fits the truth string
    # but no citations, no real reasoning, no real limitations.
    evidence = {
        "task_id": "T06_answer_stability_t4l_l99a",
        "status": "completed",
        "effect": {"direction": "destabilizing", "confidence": "moderate"},
        "evidence": "ΔΔG ≈ +2.3 kcal/mol from structure",
    }
    provenance = {"task_id": "T06_answer_stability_t4l_l99a"}

    (submission_dir / "manifest.json").write_text(json.dumps(manifest))
    (submission_dir / "metrics.json").write_text(json.dumps(metrics))
    (submission_dir / "evidence_report.json").write_text(json.dumps(evidence))
    (submission_dir / "provenance.json").write_text(json.dumps(provenance))


def _write_honest_t06(submission_dir: Path):
    """An honest submission: matches the truth and has real citations
    drawn from input/references.json, with real reasoning and limitations."""
    submission_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "task_id": "T06_answer_stability_t4l_l99a",
        "run_id": "honest",
        "status": "completed",
        "outputs": {
            "metrics": "metrics.json",
            "provenance": "provenance.json",
            "evidence_report": "evidence_report.json",
        },
        "limitations": [],
        "errors": [],
    }
    metrics = {"answer": {"effect_direction": "destabilizing"}}
    # ~600 bytes, real citations from FireProtDB + Eriksson 1992 (the primary
    # reference DOI), real reasoning, real limitations.
    evidence = {
        "task_id": "T06_answer_stability_t4l_l99a",
        "status": "completed",
        "effect": {
            "direction": "destabilizing",
            "confidence": "high",
        },
        "evidence": {
            "reasoning": (
                "L99A removes a packing leucine from the buried cavity of T4 "
                "lysozyme. Cavity-creating mutations in hydrophobic cores "
                "destabilize the fold by ~2-4 kcal/mol unless rescued by a "
                "bound ligand. FireProtDB curates this and related "
                "single-site ΔΔG values."
            ),
            "citations": [
                {
                    "doi": "10.1126/science.1553543",
                    "citation": (
                        "Eriksson AE et al. Science 1992 — cavity-creating "
                        "mutation, +4-5 kcal/mol destabilization."
                    ),
                },
                {"source": "FireProtDB", "note": "single-mutation ΔΔG records"},
            ],
        },
        "limitations": [
            "Plan-only task; no fresh MD run performed.",
            "Confidence reflects literature evidence, not new simulation.",
        ],
    }
    provenance = {
        "task_id": "T06_answer_stability_t4l_l99a",
        "evidence_sources": ["FireProtDB", "doi:10.1126/science.1553543"],
    }

    (submission_dir / "manifest.json").write_text(json.dumps(manifest))
    (submission_dir / "metrics.json").write_text(json.dumps(metrics))
    (submission_dir / "evidence_report.json").write_text(json.dumps(evidence))
    (submission_dir / "provenance.json").write_text(json.dumps(provenance))


def test_warn_policy_penalizes_fabricated_t06(tmp_path: Path):
    """Fabricated T06 submission still earned 0.60 before v1.0.x integrity
    checks. After the new checks fire, the integrity penalty knocks it down
    by 0.2 (the cap) so weighted_total drops to ~0.4."""
    task_dir = _BENCH_ROOT / "T06_answer_stability_t4l_l99a"
    task = load_task(task_dir / "task.json")
    submission_dir = tmp_path / "submission"
    _write_fabricated_t06(submission_dir)

    score = scoring.score_submission(
        task, submission_dir, run_id="warn_phase", task_dir=task_dir,
    )

    assert score.integrity_warnings, (
        "fabricated submission should produce integrity warnings"
    )
    # Truth still matches => primary axis = 1.0 from ground_truth check
    assert score.scores["scientific_answer"] == 1.0
    # Penalty floor is -0.2 (4 warnings × 0.05, capped); weighted_total
    # was 1.0 pre-penalty, so it should sit at ~0.8.
    assert 0.75 <= score.weighted_total <= 0.85, (
        f"warn-phase weighted_total={score.weighted_total} outside expected range"
    )


def test_reject_policy_clamps_fabricated_t06_to_zero(tmp_path: Path,
                                                     monkeypatch):
    """Same fabricated T06, but with integrity_policy='reject'. The score is
    clamped to 0.0 regardless of the ground-truth string match."""
    task_dir = _BENCH_ROOT / "T06_answer_stability_t4l_l99a"
    task = load_task(task_dir / "task.json")
    # Flip the policy on the in-memory task; do not touch the on-disk JSON.
    task.scoring.integrity_policy = "reject"
    submission_dir = tmp_path / "submission"
    _write_fabricated_t06(submission_dir)

    score = scoring.score_submission(
        task, submission_dir, run_id="reject_phase", task_dir=task_dir,
    )

    assert score.integrity_warnings, "reject-phase still records warnings"
    assert score.weighted_total == 0.0
    assert score.status == "failed"


def test_warn_policy_leaves_honest_t06_untouched(tmp_path: Path):
    """The honest submission satisfies every integrity check; no warning
    should fire from the artifact layer, and weighted_total should hit 1.0."""
    task_dir = _BENCH_ROOT / "T06_answer_stability_t4l_l99a"
    task = load_task(task_dir / "task.json")
    submission_dir = tmp_path / "submission"
    _write_honest_t06(submission_dir)

    score = scoring.score_submission(
        task, submission_dir, run_id="honest", task_dir=task_dir,
    )

    # Filter to artifact-level warnings (the only thing this test should care
    # about — provenance/metrics consistency is exercised elsewhere).
    artifact_warnings = [w for w in score.integrity_warnings if w.startswith("[")]
    assert artifact_warnings == [], (
        f"honest submission triggered unexpected artifact warnings: {artifact_warnings}"
    )
    assert score.scores["scientific_answer"] == 1.0
    assert score.weighted_total == 1.0


def test_reject_policy_leaves_honest_t06_untouched(tmp_path: Path):
    """Reject policy should be a no-op for an honest submission."""
    task_dir = _BENCH_ROOT / "T06_answer_stability_t4l_l99a"
    task = load_task(task_dir / "task.json")
    task.scoring.integrity_policy = "reject"
    submission_dir = tmp_path / "submission"
    _write_honest_t06(submission_dir)

    score = scoring.score_submission(
        task, submission_dir, run_id="honest_reject", task_dir=task_dir,
    )
    assert score.weighted_total == 1.0
