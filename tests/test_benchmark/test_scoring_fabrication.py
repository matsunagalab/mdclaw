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

import pytest

from mdclaw.benchmark import scoring
from mdclaw.benchmark.validation import load_task


_BENCH_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "mdagentbench" / "tasks"
_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "benchmark"


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
    """An honest submission for the redesigned MD-derived T06 task: ground
    truth direction matches AND the agent supplies WT/mutant trajectories,
    MD analysis metrics, and citations anchored to the curated pool."""
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
            "trajectories": [
                "trajectories/wt_md.dcd",
                "trajectories/mutant_md.dcd",
            ],
        },
        "limitations": [],
        "errors": [],
    }
    metrics = {
        "answer": {"effect_direction": "destabilizing"},
        "md_analysis": {
            "production_time_ns": 10.0,
            "wt": {"cavity_volume_angstrom_cubed": 142.0,
                    "ca_rmsf_core_angstrom": 0.81},
            "mutant": {"cavity_volume_angstrom_cubed": 177.2,
                        "ca_rmsf_core_angstrom": 0.99},
        },
    }
    evidence = {
        "task_id": "T06_answer_stability_t4l_l99a",
        "status": "completed",
        "effect": {"direction": "destabilizing", "confidence": "high"},
        "evidence": {
            "reasoning": (
                "Comparative WT vs L99A MD shows the mutant has +35.2 Å³ of "
                "cavity volume, +0.18 Å Cα RMSF in the core, and seven fewer "
                "core hydrophobic contacts. All MD-derived indicators "
                "support loss of packing free energy."
            ),
            "citations": [
                {
                    "source": "FireProtDB",
                    "record_id": "FireProtDB:T4L-L99A",
                    "pmid": "1553543",
                    "note": "FireProtDB curated entry for T4L L99A",
                },
                {
                    "source": "S669",
                    "record_id": "S669:T4L-L99A",
                    "note": "S669 benchmark single-mutation entry",
                },
            ],
            "md_metrics": {
                "delta_cavity_volume_angstrom_cubed": 35.2,
                "delta_ca_rmsf_core_angstrom": 0.18,
                "delta_hydrophobic_contacts_core": -7,
            },
        },
        "limitations": [
            "Short MD (10 ns per replica) used as a packing proxy; no FEP/TI.",
            "Single replica per system; statistical error not quantified.",
        ],
    }
    provenance = {
        "task_id": "T06_answer_stability_t4l_l99a",
        "evidence_sources": ["FireProtDB:T4L-L99A", "S669:T4L-L99A"],
    }

    (submission_dir / "manifest.json").write_text(json.dumps(manifest))
    (submission_dir / "metrics.json").write_text(json.dumps(metrics))
    (submission_dir / "evidence_report.json").write_text(json.dumps(evidence))
    (submission_dir / "provenance.json").write_text(json.dumps(provenance))
    traj_dir = submission_dir / "trajectories"
    traj_dir.mkdir()
    (traj_dir / "wt_md.dcd").write_bytes(b"w" * 2048)
    (traj_dir / "mutant_md.dcd").write_bytes(b"m" * 2048)


def test_warn_policy_penalizes_synthetic_fabricated_t06(tmp_path: Path):
    """Fabricated T06 submission still earned 0.60 before v1.0.x integrity
    checks. A completed fabricated submission still keeps its status credit in
    warn mode, but the artifact penalty must prevent a clean 1.0."""
    task_dir = _BENCH_ROOT / "T06_answer_stability_t4l_l99a"
    task = load_task(task_dir / "task.json")
    task.scoring.integrity_policy = "warn"
    submission_dir = tmp_path / "submission"
    _write_fabricated_t06(submission_dir)

    score = scoring.score_submission(
        task, submission_dir, run_id="warn_phase", task_dir=task_dir,
    )

    assert score.integrity_warnings, (
        "fabricated submission should produce integrity warnings"
    )
    # Redesigned T06 has 3 deterministic checks on the primary axis:
    # ground_truth (w=1.0, passes) + md_trajectories_present (w=0.3, fails) +
    # md_production_time_min (w=0.2, fails). With no MD evidence the primary
    # axis caps at 1.0/1.5 = 0.6667, not 1.0 — matching the truth string
    # alone no longer earns a clean primary score.
    assert score.scores["scientific_answer"] == pytest.approx(0.6667, abs=1e-3)
    # On top of that the integrity layer takes -0.2 (penalty cap from
    # several missing-evidence warnings), so weighted_total lands ~0.47.
    assert 0.4 <= score.weighted_total <= 0.55, (
        f"warn-phase weighted_total={score.weighted_total} outside expected range"
    )


def test_warn_policy_pins_real_haiku_v1_t06_regression_fixture():
    """Pin the actual 2026-05-11 Haiku v1 T06 submission shape.

    This is the regression fixture for the observed failure mode: a partial
    template-derived submission matched the truth string and previously sat at
    0.60 with no integrity warning. Future scorer edits must keep it below
    that old score.
    """
    task_dir = _BENCH_ROOT / "T06_answer_stability_t4l_l99a"
    task = load_task(task_dir / "task.json")
    task.scoring.integrity_policy = "warn"
    submission_dir = _FIXTURE_ROOT / "haiku_v1_t06_fabricated"

    score = scoring.score_submission(
        task, submission_dir, run_id="haiku_v1_fixture", task_dir=task_dir,
    )

    assert score.integrity_warnings, (
        "real Haiku v1 fixture should produce integrity warnings"
    )
    # Direction matches the truth, but the redesigned task requires WT/mutant
    # trajectories + md_production_time, neither of which the Haiku v1
    # fabricated submission provides. Primary axis caps at 0.6667; integrity
    # penalty knocks the total down further.
    assert score.scores["scientific_answer"] == pytest.approx(0.6667, abs=1e-3)
    assert score.weighted_total < 0.6, (
        "real fabricated fixture must not regress to the old 0.60 score"
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
    task.scoring.integrity_policy = "warn"
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


# ---------------------------------------------------------------------------
# T02 / T08 reject-phase promotions (shipped in v1.1)


def test_reject_policy_zeros_silent_refusal_t02_submission(tmp_path: Path):
    """T02 ships with integrity_policy='reject'. A "refusal" submission that
    sets manifest.status='failed' + correct guardrail_code but leaves
    evidence_report.json as a 50-byte stub must clamp to 0.0 instead of
    earning ground-truth credit. The agent should not be able to refuse
    without explaining why."""
    task_dir = _BENCH_ROOT / "T02_prep_metalloenzyme_guardrail"
    task = load_task(task_dir / "task.json")
    assert task.scoring.integrity_policy == "reject", (
        "T02 must remain on reject policy for this regression test"
    )

    submission_dir = tmp_path / "submission"
    submission_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "task_id": "T02_prep_metalloenzyme_guardrail",
        "run_id": "silent_refusal",
        "status": "failed",
        "outputs": {},
        "limitations": ["intentional refusal"],
    }
    metrics = {
        "preparation": {
            "guardrail_code": "metal_containing_ligand_blocked",
            "status": "blocked_by_guardrail",
        },
    }
    # Sub-200-byte evidence_report — a silent refusal with no explanation.
    evidence = {"summary": "blocked"}
    (submission_dir / "manifest.json").write_text(json.dumps(manifest))
    (submission_dir / "metrics.json").write_text(json.dumps(metrics))
    (submission_dir / "evidence_report.json").write_text(json.dumps(evidence))
    (submission_dir / "provenance.json").write_text(json.dumps(
        {"task_id": "T02_prep_metalloenzyme_guardrail"}
    ))

    score = scoring.score_submission(
        task, submission_dir, run_id="t02_silent_refusal", task_dir=task_dir,
    )

    assert score.integrity_warnings, (
        "silent refusal should trip evidence_report_real_bytes integrity check"
    )
    assert score.weighted_total == 0.0
    assert score.status == "failed"


def test_reject_policy_zeros_text_disguised_as_png_t08(tmp_path: Path):
    """T08 ships with integrity_policy='reject'. A submission that lists
    .png files in manifest.outputs.figures but writes text into them
    must clamp to 0.0 — figures must be real raster images."""
    task_dir = _BENCH_ROOT / "T08_communicate_t4l_dynamics"
    task = load_task(task_dir / "task.json")
    assert task.scoring.integrity_policy == "reject", (
        "T08 must remain on reject policy for this regression test"
    )

    submission_dir = tmp_path / "submission"
    figures_dir = submission_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    # Three text files masquerading as PNGs.
    for name in ("rmsd.png", "rmsf.png", "contacts.png"):
        (figures_dir / name).write_text(
            f"This is a caption for {name}, not a real figure.\n"
        )

    manifest = {
        "schema_version": "1.0",
        "task_id": "T08_communicate_t4l_dynamics",
        "run_id": "text_as_png",
        "status": "completed",
        "outputs": {
            "figures": [
                "figures/rmsd.png", "figures/rmsf.png", "figures/contacts.png",
            ],
            "evidence_report": "evidence_report.json",
            "metrics": "metrics.json",
        },
        "limitations": [],
    }
    metrics = {
        "analysis": {"rmsd": {"mean_angstrom": 1.21}},
    }
    # Provide a realistic-sized evidence_report so the figure-magic check is
    # what actually clamps the score (not the bytes/template checks).
    evidence = {
        "schema_version": "1.0",
        "summary": (
            "Three figures listed in manifest.outputs.figures, captioned in "
            "evidence_report.figure_captions. Numbers match metrics.json."
        ),
        "figure_captions": [
            {"path": "figures/rmsd.png", "caption": "RMSD mean 1.21 Å"},
        ],
        "limitations": ["Synthetic regression fixture; figures are text."],
    }
    (submission_dir / "manifest.json").write_text(json.dumps(manifest))
    (submission_dir / "metrics.json").write_text(json.dumps(metrics))
    (submission_dir / "evidence_report.json").write_text(json.dumps(evidence))
    (submission_dir / "provenance.json").write_text(json.dumps(
        {"task_id": "T08_communicate_t4l_dynamics"}
    ))

    score = scoring.score_submission(
        task, submission_dir, run_id="t08_text_png", task_dir=task_dir,
    )

    assert score.integrity_warnings, (
        "text-disguised-as-PNG should trip figures_are_png integrity check"
    )
    # At least one warning must point at the figures check specifically.
    assert any("[figures_are_real_png]" in w for w in score.integrity_warnings), (
        f"expected figures_are_real_png warning, got: {score.integrity_warnings}"
    )
    assert score.weighted_total == 0.0
    assert score.status == "failed"
