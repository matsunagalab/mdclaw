#!/usr/bin/env python3
"""Knowledge-only discrimination baseline for MDStudyBench.

For the v0.4 S01 task this writes the published outcome into a structurally
plausible grounded-v2 submission, including a prospective-looking intent and
the full public protein sequence.  Its DCD files are deliberately junk and no
harness event attests the claimed production.  A correct scorer must therefore
classify the run as ``invalid_execution`` with zero scientific-answer credit,
even when ``--outcome`` matches held-out truth.

Legacy v0.3 task IDs retain their former submission shape so old regression
fixtures can still invoke this script.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


V2_TASK_ID = "S01_pressure_hydration_t4l_l99a"
T4L_L99A_SEQUENCE = (
    "MNIFEMLRIDEGLRLKIYKDTEGYYTIGIGHLLTKSPSLNAAKSELDKAIGRNTNGVITKDEAE"
    "KLFNQDVDAAVRGILRNAKLKPVYDSLDAVRRAAAINMVFQMGETGVAGFTNSLRMLQQKRWDEA"
    "AVNLAKSRWYNQTPNRAKRVITTFRTGTWDAYKNL"
)
ONE_TO_THREE = {
    "A": "ALA", "C": "CYS", "D": "ASP", "E": "GLU", "F": "PHE",
    "G": "GLY", "H": "HIS", "I": "ILE", "K": "LYS", "L": "LEU",
    "M": "MET", "N": "ASN", "P": "PRO", "Q": "GLN", "R": "ARG",
    "S": "SER", "T": "THR", "V": "VAL", "W": "TRP", "Y": "TYR",
}


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        path.write_text(str(payload))


def _write_public_construct(path: Path) -> None:
    lines = []
    for index, residue in enumerate(T4L_L99A_SEQUENCE, start=1):
        x = 0.15 * index
        lines.append(
            f"ATOM  {index:5d}  CA  {ONE_TO_THREE[residue]:>3} A{index:4d}    "
            f"{x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           C\n"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines) + "TER\nEND\n")


def _write_v2_submission(sub: Path, *, task_id: str, outcome: str) -> None:
    intent_id = "knowledge-only-intent"
    comparison_id = "pressure-effect"
    fake_dcd = b"\x54\x00\x00\x00CORD" + b"NOT REAL MD - LITERATURE GUESS\n" * 64
    runs = []
    for system_id, pressure in (("ambient", 0.1), ("high-pressure", 200.0)):
        topology = f"systems/{system_id}/topology.pdb"
        trajectory = f"systems/{system_id}/confirmatory.dcd"
        _write_public_construct(sub / topology)
        (sub / trajectory).parent.mkdir(parents=True, exist_ok=True)
        (sub / trajectory).write_bytes(fake_dcd)
        runs.append(
            {
                "system_id": system_id,
                "source": {"type": "knowledge_only_baseline", "id": "none"},
                "conditions": {
                    "temperature_k": 300.0,
                    "ph": 7.0,
                    "pressure_mpa": pressure,
                },
                "runs": [
                    {
                        "run_id": f"{system_id}-fake-confirmatory",
                        "phase": "confirmatory",
                        "intent_id": intent_id,
                        "production_event_id": f"fake-prod-{system_id}",
                        "topology": topology,
                        "trajectory": trajectory,
                    }
                ],
            }
        )

    analysis_intent = {
        "schema_version": "1.0",
        "task_id": task_id,
        "intent_id": intent_id,
        "target_estimand": (
            "The 200 MPa minus 0.1 MPa difference in equilibrium mean "
            "internal-cavity water occupancy while T4 lysozyme "
            "C54T/C97A/L99A remains folded."
        ),
        "primary_analyses": [
            {
                "analysis_id": "hydration-primary",
                "analysis_role": "estimand",
                "comparison_id": comparison_id,
                "verifier_id": "region_water_occupancy@1",
                "observable": {
                    "parameters": {
                        "region_selection": "resid 98",
                        "radius_nm": 0.45,
                        "discard_initial_frames": 1,
                        "n_blocks": 5,
                        "periodic": False,
                    }
                },
                "outcome_mapping": {
                    "increase": "increased_hydration",
                    "decrease": "decreased_hydration",
                    "equivalent": "no_material_change",
                    "unresolved": "unresolved",
                },
                "decision_rule": {
                    "kind": "equivalence_ci",
                    "confidence_level": 0.95,
                    "equivalence_margin": 0.25,
                    "unit": "water_count",
                },
                "estimand_link": "Purports to measure the pressure contrast.",
                "alternative_explanations": ["No real MD was run."],
            },
            {
                "analysis_id": "folded-control",
                "analysis_role": "validity_control",
                "comparison_id": comparison_id,
                "verifier_id": "folded_state_retention@1",
                "observable": {
                    "parameters": {
                        "selection": "protein and name CA",
                        "discard_initial_frames": 1,
                        "n_blocks": 5,
                    }
                },
                "outcome_mapping": {"pass": "retained", "fail": "unresolved"},
                "decision_rule": {
                    "kind": "custom",
                    "confidence_level": 0.95,
                    "parameters": {"plugin": "folded_state_retention@1"},
                },
                "estimand_link": "Purports to check folded-state validity.",
                "alternative_explanations": ["No real MD was run."],
            },
        ],
    }
    study_index = {
        "schema_version": "2.0",
        "task_id": task_id,
        "systems": runs,
        "comparisons": [
            {
                "comparison_id": comparison_id,
                "reference_system_ids": ["ambient"],
                "variant_system_ids": ["high-pressure"],
                "matched_except": ["pressure_mpa"],
            }
        ],
    }
    evidence_report = {
        "schema_version": "2.0",
        "task_id": task_id,
        "prior_expectation": {
            "outcome": outcome,
            "confidence": 1.0,
            "sources": ["public literature"],
            "rationale": "The answer was supplied to this baseline.",
        },
        "md_verdict": {
            "status": "resolved",
            "outcome": outcome,
            "basis": "direct_estimator",
            "confidence": 1.0,
            "cited_evidence_ids": ["fake-hydration", "fake-fold-control"],
            "unresolved_reasons": [],
        },
        "evidence": [
            {
                "id": "fake-hydration",
                "intent_id": intent_id,
                "analysis_id": "hydration-primary",
                "comparison_id": comparison_id,
                "verifier_id": "region_water_occupancy@1",
                "claim_role": "direct_estimator",
                "estimand_link": "Self-reported with no loadable trajectory.",
                "reported": {"estimate": 1.0, "unit": "water_count"},
                "uncertainty": 0.0,
                "artifacts": ["analysis/fake-hydration.json"],
            },
            {
                "id": "fake-fold-control",
                "intent_id": intent_id,
                "analysis_id": "folded-control",
                "comparison_id": comparison_id,
                "verifier_id": "folded_state_retention@1",
                "claim_role": "validity_control",
                "estimand_link": "Self-reported with no loadable trajectory.",
                "reported": {"folded_state_retained": True},
                "uncertainty": 0.0,
                "artifacts": ["analysis/fake-fold-control.json"],
            },
        ],
        "reasoning": "This repeats the supplied literature answer, not MD evidence.",
        "limitations": ["No real simulation was run."],
    }
    manifest = {
        "schema_version": "1.0",
        "task_id": task_id,
        "status": "completed",
        "outputs": {
            "analysis_intent": "analysis_intent.json",
            "study_index": "study_index.json",
            "evidence_report": "evidence_report.json",
        },
    }
    for filename, payload in (
        ("manifest.json", manifest),
        ("analysis_intent.json", analysis_intent),
        ("study_index.json", study_index),
        ("evidence_report.json", evidence_report),
        ("analysis/fake-hydration.json", {"fabricated": True}),
        ("analysis/fake-fold-control.json", {"fabricated": True}),
    ):
        _write(sub / filename, payload)


def _write_legacy_submission(
    sub: Path,
    *,
    task_id: str,
    run_id: str,
    direction: str,
) -> None:
    fake_dcd = b"\x54\x00\x00\x00CORD" + b"NOT REAL MD - LITERATURE GUESS\n" * 64
    trajectories = ["trajectories/wt.dcd", "trajectories/mutant.dcd"]
    for relative in trajectories:
        (sub / relative).parent.mkdir(parents=True, exist_ok=True)
        (sub / relative).write_bytes(fake_dcd)
    _write(
        sub / "manifest.json",
        {
            "schema_version": "1.0",
            "run_id": run_id,
            "task_id": task_id,
            "status": "completed",
            "outputs": {
                "metrics": "metrics.json",
                "provenance": "provenance.json",
                "evidence_report": "evidence_report.json",
                "trajectories": trajectories,
                "topology": [],
            },
        },
    )
    _write(
        sub / "metrics.json",
        {
            "schema_version": "1.0",
            "task_id": task_id,
            "md_analysis": {"production_time_ns": 100.0},
        },
    )
    _write(
        sub / "evidence_report.json",
        {
            "schema_version": "1.0",
            "task_id": task_id,
            "effect": {"direction": direction, "confidence": "high"},
            "evidence": {"citations": [], "md_metrics": {}},
            "limitations": ["No real simulation was run."],
        },
    )
    _write(
        sub / "provenance.json",
        {"schema_version": "1.0", "task_id": task_id, "command_log": []},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission-dir", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--outcome", help="v0.4 md_verdict outcome")
    parser.add_argument("--direction", help="legacy effect.direction alias")
    parser.add_argument("--run-id", default="study_literature_guess_no_md")
    args = parser.parse_args()

    answer = args.outcome or args.direction
    if not answer:
        parser.error("one of --outcome or --direction is required")
    submission = Path(args.submission_dir)
    submission.mkdir(parents=True, exist_ok=True)
    if args.task_id == V2_TASK_ID:
        _write_v2_submission(submission, task_id=args.task_id, outcome=answer)
    else:
        _write_legacy_submission(
            submission,
            task_id=args.task_id,
            run_id=args.run_id,
            direction=answer,
        )

    print(
        f"[ok] wrote knowledge-only baseline for {args.task_id} to {submission} "
        "(expected scientific-answer credit: 0)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
