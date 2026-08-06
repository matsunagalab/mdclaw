"""Guards for the MDPrepBench positive fixtures.

Two levels, because re-scoring 40 bundles takes tens of minutes:

- The manifest checks run always. They need no bundles and catch the cheap
  mistakes: a task contract edited without re-recording its witness, a suite task
  with no witness at all, a malformed entry.
- The re-scoring check is opt-in through `MDPREPBENCH_WITNESS_DIR`, since the
  ~1.8 GB of bundles live outside the repository. That is the one that actually
  detects the scorer regressing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "benchmarks" / "mdprepbench" / "witnesses" / "manifest.json"
DATASET = REPO_ROOT / "benchmarks" / "mdprepbench" / "dataset.json"
TOOL = REPO_ROOT / "benchmarks" / "tools" / "witness.py"

REQUIRED_FIELDS = {
    "run_id", "recorded_at", "produced_by", "repository_head", "contract_sha256",
    "files",
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _witness_tool():
    spec = importlib.util.spec_from_file_location("witness_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def manifest() -> dict:
    # Deliberately not a skip: a missing manifest means every task lost its
    # regression guard, which is exactly what this file exists to notice.
    assert MANIFEST.is_file(), f"no witness manifest at {MANIFEST}"
    return json.loads(MANIFEST.read_text())


def test_every_task_has_a_witness(manifest: dict):
    task_ids = set(json.loads(DATASET.read_text())["task_ids"])
    recorded = set(manifest["witnesses"])

    assert recorded == task_ids, (
        f"missing witnesses: {sorted(task_ids - recorded)}; "
        f"stale witnesses: {sorted(recorded - task_ids)}"
    )


def test_witness_entries_are_well_formed(manifest: dict):
    for task_id, entry in manifest["witnesses"].items():
        missing = REQUIRED_FIELDS - set(entry)
        assert not missing, f"{task_id} lacks {sorted(missing)}"
        assert isinstance(entry["run_id"], str) and entry["run_id"], task_id
        assert SHA256.match(entry["contract_sha256"]), task_id

        files = entry["files"]
        assert isinstance(files, dict) and files, f"{task_id} records no files"
        assert "harness_execution.json" in files, task_id
        assert any(f.startswith("submission/") for f in files), task_id
        for rel, digest in files.items():
            assert SHA256.match(digest), f"{task_id}:{rel}"
            # A recorded path is joined onto a bundle root at verify time.
            assert not Path(rel).is_absolute(), f"{task_id}:{rel}"
            assert ".." not in Path(rel).parts, f"{task_id}:{rel}"


def test_witnesses_match_the_contract_they_were_recorded_against(manifest: dict):
    """A task edited after recording invalidates its witness silently.

    The hash covers every file the scorer reads for the task, not just
    `task.json`: five tasks keep a private reference structure under `truth/`,
    and swapping one changes what "correct" means without touching task.json.
    """
    tool = _witness_tool()
    stale = [
        task_id
        for task_id, entry in manifest["witnesses"].items()
        if tool._contract_sha256(task_id) != entry["contract_sha256"]
    ]

    assert not stale, (
        f"task contract changed since recording; re-record these: {stale}"
    )


@pytest.mark.parametrize(
    "score, accepted",
    [
        ({"status": "passed", "weighted_total": 1.0,
          "scores": {"preparation": 1.0}, "integrity_warnings": []}, True),
        ({"status": "failed", "weighted_total": 1.0,
          "scores": {"preparation": 1.0}, "integrity_warnings": []}, False),
        ({"status": "passed", "weighted_total": 0.9,
          "scores": {"preparation": 1.0}, "integrity_warnings": []}, False),
        ({"status": "passed", "weighted_total": 1.0,
          "scores": {"preparation": 0.9}, "integrity_warnings": []}, False),
        ({"status": "passed", "weighted_total": 1.0,
          "scores": {"preparation": 1.0}, "integrity_warnings": ["x"]}, False),
    ],
)
def test_acceptance_requires_every_axis(score: dict, accepted: bool):
    """One axis passing is not enough; a witness must be clean on all of them."""
    ok, _why = _witness_tool()._accepted(score)

    assert ok is accepted


def test_unknown_task_is_rejected_rather_than_ignored(tmp_path: Path):
    tool = _witness_tool()
    (tmp_path / "P01_prep_simple_monomer_t4l" / "submission").mkdir(parents=True)

    with pytest.raises(SystemExit):
        tool._select(tmp_path, ["P99_does_not_exist"])


def test_empty_bundle_root_is_rejected(tmp_path: Path):
    tool = _witness_tool()

    with pytest.raises(SystemExit):
        tool._select(tmp_path, None)


@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("MDPREPBENCH_WITNESS_DIR"),
    reason="set MDPREPBENCH_WITNESS_DIR to the bundle directory to re-score",
)
def test_recorded_bundles_still_score_one(manifest: dict):
    """Re-score every bundle: this is the scorer-regression detector."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(TOOL), "verify"],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
