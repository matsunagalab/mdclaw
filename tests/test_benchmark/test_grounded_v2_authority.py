from __future__ import annotations

import json
from pathlib import Path

from mdclaw.benchmark.scoring import score_submission
from tests.test_benchmark.test_grounded_v2_scoring import (
    _case,
)
from tests.test_benchmark.test_public_preflight_v2 import _write_json


def test_v2_truth_check_uses_manifest_declared_evidence_report(
    tmp_path: Path,
):
    task, task_dir, submission, harness_path, _bundle = _case(
        tmp_path,
        expected_outcome="decreased_hydration",
    )

    declared_report = json.loads(
        (submission / "evidence_report.json").read_text()
    )
    _write_json(submission / "reports" / "declared.json", declared_report)
    manifest = json.loads((submission / "manifest.json").read_text())
    manifest["outputs"]["evidence_report"] = "reports/declared.json"
    _write_json(submission / "manifest.json", manifest)

    # This conventional-path decoy agrees with truth, but it is not the report
    # presented to the truth-blind verifier and judge.
    decoy = dict(declared_report)
    decoy["md_verdict"] = dict(declared_report["md_verdict"])
    decoy["md_verdict"]["outcome"] = "decreased_hydration"
    _write_json(submission / "evidence_report.json", decoy)

    score = score_submission(
        task,
        submission,
        task_dir=task_dir,
        harness_record_file=harness_path,
    )

    assert score.study_verdict.truth_agreement is False
    assert score.study_verdict.result_class == "grounded_wrong"
