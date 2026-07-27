"""Standalone preflight tests for the runner-finalized v2 envelope."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mdclaw.benchmark import cli


REPO_ROOT = Path(__file__).resolve().parents[2]
STUDY_DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"
TASK_ID = "S01_pressure_hydration_t4l_l99a"


@pytest.fixture(scope="module")
def public_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output_dir = tmp_path_factory.mktemp("mdstudybench-public") / "package"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(STUDY_DATASET_DIR),
        output_dir=str(output_dir),
    )
    assert result["success"], result
    return output_dir


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_finalized_submission(root: Path) -> dict[str, dict]:
    plan = {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "runs": [
            {
                "run_id": "ambient-1",
                "condition_role": "reference",
                "job_dir": "jobs/ambient",
                "node_id": "prod_001",
                "simulation_time_ns": 10.0,
            },
            {
                "run_id": "pressure-1",
                "condition_role": "variant",
                "job_dir": "jobs/pressure",
                "node_id": "prod_001",
                "simulation_time_ns": 10.0,
            },
        ],
    }
    claim = {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "status": "resolved",
        "outcome": "increased_hydration",
    }
    _write_json(root / "confirmatory_plan.json", plan)
    _write_json(root / "claim.json", claim)

    plan_hash = _sha256(root / "confirmatory_plan.json")
    input_names = ("base_system", "topology", "start_state")
    output_names = (
        "trajectory",
        "state",
        "energy",
        "runtime_system",
        "integrator",
    )
    events = []
    for sequence, run in enumerate(plan["runs"], start=1):
        records: dict[str, dict[str, dict]] = {}
        for group, names in (
            ("input_artifacts", input_names),
            ("output_artifacts", output_names),
        ):
            direction = "input" if group == "input_artifacts" else "output"
            records[group] = {}
            for name in names:
                relative = (
                    Path("artifacts")
                    / f"{sequence:03d}"
                    / direction
                    / f"{name}.dat"
                )
                artifact = root / "episode" / relative
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_bytes(
                    f"runner-{sequence}-{direction}-{name}".encode()
                )
                records[group][name] = {
                    "path": relative.as_posix(),
                    "sha256": _sha256(artifact),
                    "bytes": artifact.stat().st_size,
                }
        events.append(
            {
                "run_id": run["run_id"],
                "condition_role": run["condition_role"],
                "node_id": run["node_id"],
                "event_id": f"runner-prod-{sequence:03d}",
                "plan_sha256": plan_hash,
                **records,
            }
        )
    episode = {
        "schema_version": "1.0",
        "kind": "mdstudybench_runner_episode_v2",
        "recorded_by": "mdclaw_benchmark_runner",
        "task_id": TASK_ID,
        "plan_sha256": plan_hash,
        "within_task_budget": True,
        "success": True,
        "errors": [],
        "events": events,
    }
    manifest = {
        "schema_version": "1.0",
        "generated_by": {"tool": "mdclaw_benchmark_runner"},
        "task_id": TASK_ID,
        "status": "completed",
        "outputs": {
            "confirmatory_plan": "confirmatory_plan.json",
            "claim": "claim.json",
            "episode": "episode/episode.json",
        },
    }
    _write_json(root / "episode" / "episode.json", episode)
    _write_json(root / "manifest.json", manifest)
    return {
        "plan": plan,
        "claim": claim,
        "episode": episode,
        "manifest": manifest,
    }


def _run_preflight(
    *,
    public_package: Path,
    submission_dir: Path,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    tool = public_package / "tools" / "validate_submission.py"
    contract = (
        public_package
        / "tasks"
        / TASK_ID
        / "submission_contract.json"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(tool),
            "--submission-dir",
            str(submission_dir),
            "--submission-contract",
            str(contract),
            "--skip-openmm",
        ],
        cwd=submission_dir.parent,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed, json.loads(completed.stdout)


def _v2_checks(result: dict) -> dict:
    manifest_check = next(
        check
        for check in result["checks"]
        if check["name"] == "completed_manifest_contract"
    )
    return manifest_check["v2_truth_blind_checks"]


def test_exported_preflight_validates_runner_finalized_envelope(
    tmp_path: Path,
    public_package: Path,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    _build_finalized_submission(submission)

    for internal_tool in (
        "study_evidence_v2.py",
        "study_identity_v2.py",
        "preregistration_v2.py",
        "study_execution_v2.py",
    ):
        assert not (public_package / "tools" / internal_tool).exists()

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 0, completed.stderr
    assert result["success"] is True
    assert _v2_checks(result) == {"passed": True, "errors": []}


def test_agent_plan_stage_is_not_a_final_public_preflight_package(
    tmp_path: Path,
    public_package: Path,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_finalized_submission(submission)
    (submission / "claim.json").unlink()
    (submission / "manifest.json").unlink()
    (submission / "episode" / "episode.json").unlink()
    assert payloads["plan"]["runs"]

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 1
    assert result["success"] is False
    assert any("claim.json" in error for error in result["errors"])


def test_public_preflight_rejects_agent_authored_manifest(
    tmp_path: Path,
    public_package: Path,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_finalized_submission(submission)
    payloads["manifest"]["generated_by"]["tool"] = "test-agent"
    _write_json(submission / "manifest.json", payloads["manifest"])

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 1
    assert "v2 manifest must be generated" in " ".join(_v2_checks(result)["errors"])


def test_public_preflight_rejects_plan_hash_mismatch(
    tmp_path: Path,
    public_package: Path,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_finalized_submission(submission)
    payloads["plan"]["runs"][0]["simulation_time_ns"] = 11.0
    _write_json(submission / "confirmatory_plan.json", payloads["plan"])

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 1
    assert "confirmatory plan hash differs" in " ".join(
        _v2_checks(result)["errors"]
    )


def test_public_preflight_rejects_unknown_claim_outcome(
    tmp_path: Path,
    public_package: Path,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_finalized_submission(submission)
    payloads["claim"]["outcome"] = "literature_guess"
    _write_json(submission / "claim.json", payloads["claim"])

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 1
    assert "claim.outcome must be one of" in " ".join(
        _v2_checks(result)["errors"]
    )


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("plan_extra", "confirmatory_plan has unexpected field"),
        ("run_extra", "confirmatory_plan.runs[0] has unexpected field"),
        ("plan_schema", "confirmatory_plan.schema_version"),
        ("run_id_type", "run_id must be a non-empty string"),
        ("job_dir_type", "job_dir must be a non-empty string"),
        ("duration_type", "simulation_time_ns must be finite and positive"),
        ("claim_extra", "claim has unexpected field"),
        ("claim_schema", "claim.schema_version"),
        ("claim_outcome_missing", "claim is missing required field"),
        ("claim_reasoning", "claim has unexpected field"),
        ("claim_limitations", "claim has unexpected field"),
    ],
)
def test_public_preflight_rejects_v2_schema_and_strict_type_mismatches(
    tmp_path: Path,
    public_package: Path,
    case: str,
    expected_error: str,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_finalized_submission(submission)

    if case == "plan_extra":
        payloads["plan"]["unexpected"] = True
    elif case == "run_extra":
        payloads["plan"]["runs"][0]["unexpected"] = True
    elif case == "plan_schema":
        payloads["plan"]["schema_version"] = "2.0"
    elif case == "run_id_type":
        payloads["plan"]["runs"][0]["run_id"] = 1
    elif case == "job_dir_type":
        payloads["plan"]["runs"][0]["job_dir"] = 1
    elif case == "duration_type":
        payloads["plan"]["runs"][0]["simulation_time_ns"] = "10"
    elif case == "claim_extra":
        payloads["claim"]["unexpected"] = True
    elif case == "claim_schema":
        payloads["claim"]["schema_version"] = "2.0"
    elif case == "claim_outcome_missing":
        payloads["claim"].pop("outcome")
    elif case == "claim_reasoning":
        payloads["claim"]["reasoning"] = "unscored prose"
    elif case == "claim_limitations":
        payloads["claim"]["limitations"] = ["unscored prose"]
    else:  # pragma: no cover - parametrization is exhaustive.
        raise AssertionError(case)

    target = "claim.json" if case.startswith("claim") else (
        "confirmatory_plan.json"
    )
    payload = payloads["claim"] if target == "claim.json" else payloads["plan"]
    _write_json(submission / target, payload)

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 1
    assert expected_error in " ".join(_v2_checks(result)["errors"])


def test_public_preflight_accepts_shared_schema_defaults(
    tmp_path: Path,
    public_package: Path,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_finalized_submission(submission)
    payloads["plan"].pop("schema_version")
    payloads["claim"].pop("schema_version")
    _write_json(submission / "confirmatory_plan.json", payloads["plan"])
    _write_json(submission / "claim.json", payloads["claim"])
    plan_hash = _sha256(submission / "confirmatory_plan.json")
    payloads["episode"]["plan_sha256"] = plan_hash
    for event in payloads["episode"]["events"]:
        event["plan_sha256"] = plan_hash
    _write_json(
        submission / "episode" / "episode.json",
        payloads["episode"],
    )

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 0, result["errors"]
    assert _v2_checks(result) == {"passed": True, "errors": []}


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("manifest_extra", "manifest.outputs must exactly declare"),
        ("manifest_path", "manifest.outputs must exactly declare"),
        ("episode_success", "episode.success must be true"),
        ("episode_budget", "episode.within_task_budget must be true"),
        ("episode_errors", "episode.errors must be an empty list"),
        ("input_missing", "input_artifacts must contain exactly"),
        ("output_extra", "output_artifacts must contain exactly"),
        ("record_fields", "must contain exactly path, sha256, and bytes"),
        ("bad_sha", "artifact SHA-256 is invalid"),
        ("bad_bytes_type", "artifact bytes is invalid"),
        ("bad_bytes_value", "artifact size mismatch"),
    ],
)
def test_public_preflight_rejects_runner_envelope_mismatches(
    tmp_path: Path,
    public_package: Path,
    case: str,
    expected_error: str,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_finalized_submission(submission)
    manifest = payloads["manifest"]
    episode = payloads["episode"]
    event = episode["events"][0]
    trajectory = event["output_artifacts"]["trajectory"]

    if case == "manifest_extra":
        manifest["outputs"]["extra"] = "extra.json"
        _write_json(submission / "extra.json", {"extra": True})
    elif case == "manifest_path":
        manifest["outputs"]["claim"] = "other-claim.json"
        _write_json(submission / "other-claim.json", payloads["claim"])
    elif case == "episode_success":
        episode["success"] = False
    elif case == "episode_budget":
        episode["within_task_budget"] = False
    elif case == "episode_errors":
        episode["errors"] = ["adapter failed"]
    elif case == "input_missing":
        event["input_artifacts"].pop("start_state")
    elif case == "output_extra":
        event["output_artifacts"]["unexpected"] = dict(trajectory)
    elif case == "record_fields":
        trajectory["unexpected"] = True
    elif case == "bad_sha":
        trajectory["sha256"] = "not-a-digest"
    elif case == "bad_bytes_type":
        trajectory["bytes"] = "25"
    elif case == "bad_bytes_value":
        trajectory["bytes"] += 1
    else:  # pragma: no cover - parametrization is exhaustive.
        raise AssertionError(case)

    _write_json(submission / "manifest.json", manifest)
    _write_json(submission / "episode" / "episode.json", episode)

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 1
    assert expected_error in " ".join(_v2_checks(result)["errors"])


def test_public_preflight_rejects_unsafe_episode_artifact_path(
    tmp_path: Path,
    public_package: Path,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_finalized_submission(submission)
    event = payloads["episode"]["events"][0]
    event["output_artifacts"]["trajectory"]["path"] = "../outside.json"
    _write_json(submission / "episode" / "episode.json", payloads["episode"])

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 1
    assert "episode artifact path" in " ".join(_v2_checks(result)["errors"])


def test_exported_preflight_hashes_artifacts_without_reading_whole_files(
    public_package: Path,
):
    tool = public_package / "tools" / "validate_submission.py"
    assert ".read_bytes()" not in tool.read_text()
