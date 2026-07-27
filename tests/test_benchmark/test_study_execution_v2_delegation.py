"""Routing tests for MDStudyBench v2 artifact inspection.

The benchmark runner often lives in a thin orchestration venv while the
confirmatory MD runs inside the MDClaw container. Inspection has to reach the
same science stack that wrote the artifacts, otherwise a missing import is
reported as if the artifacts themselves were untrustworthy. These tests cover
the routing only, so they must not require openmm/mdtraj.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mdclaw.benchmark import study_execution_v2 as sx


def _paths(tmp_path: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    inputs = {name: tmp_path / "in" / name for name in ("base_system", "topology")}
    outputs = {name: tmp_path / "out" / name for name in ("trajectory", "state")}
    for path in (*inputs.values(), *outputs.values()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("artifact")
    return inputs, outputs


def _echo_argv() -> list[str]:
    """A stand-in container command that answers like the delegated child."""
    script = (
        "import json,sys\n"
        "payload=json.load(sys.stdin)\n"
        "print('incidental container chatter', file=sys.stderr)\n"
        "print('warning: some loader banner')\n"
        f"print({sx._INSPECTION_RESULT_SENTINEL!r} + json.dumps("
        "{'runtime_facts': {'engine': 'OpenMM',"
        " 'condition_role': payload['condition_role']}, 'errors': []}))\n"
    )
    return [sys.executable, "-c", script]


def _inspect(tmp_path: Path) -> tuple[dict, list[str]]:
    inputs, outputs = _paths(tmp_path)
    return sx._run_openmm_artifact_inspection(
        input_paths=dict(inputs),
        output_paths=dict(outputs),
        metadata={},
        condition_role="variant",
        scientific_target={},
    )


def test_inspection_is_delegated_when_science_stack_is_missing(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv(sx._INSPECTION_INPROCESS_ENV, raising=False)
    monkeypatch.setattr(sx, "_missing_inspection_dependencies", lambda: ["mdtraj"])
    monkeypatch.setattr(sx, "_inspection_delegate_argv", lambda _paths: _echo_argv())

    def _fail(**_kwargs):
        raise AssertionError("in-process inspection must not run without mdtraj")

    monkeypatch.setattr(sx, "_inspect_openmm_artifacts", _fail)

    facts, errors = _inspect(tmp_path)

    assert errors == []
    # The payload made the round trip, and unrelated stdout did not confuse it.
    assert facts == {"engine": "OpenMM", "condition_role": "variant"}


def test_inspection_stays_in_process_when_stack_is_available(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv(sx._INSPECTION_INPROCESS_ENV, raising=False)
    monkeypatch.setattr(sx, "_missing_inspection_dependencies", list)
    monkeypatch.setattr(
        sx,
        "_inspect_openmm_artifacts",
        lambda **_kwargs: ({"engine": "in-process"}, []),
    )
    monkeypatch.setattr(
        sx,
        "_inspection_delegate_argv",
        lambda _paths: (_ for _ in ()).throw(
            AssertionError("must not delegate when the stack is importable")
        ),
    )

    facts, errors = _inspect(tmp_path)

    assert (facts, errors) == ({"engine": "in-process"}, [])


def test_delegated_child_does_not_delegate_again(tmp_path: Path, monkeypatch):
    """The container child sets the env flag, so it inspects in process."""
    monkeypatch.setenv(sx._INSPECTION_INPROCESS_ENV, "1")
    monkeypatch.setattr(sx, "_missing_inspection_dependencies", lambda: ["mdtraj"])
    monkeypatch.setattr(
        sx,
        "_inspect_openmm_artifacts",
        lambda **_kwargs: ({"engine": "in-process"}, []),
    )

    facts, errors = _inspect(tmp_path)

    assert (facts, errors) == ({"engine": "in-process"}, [])


def test_missing_container_is_reported_as_unavailable_not_untrusted(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv(sx._INSPECTION_INPROCESS_ENV, raising=False)
    monkeypatch.setattr(sx, "_missing_inspection_dependencies", lambda: ["mdtraj"])
    monkeypatch.setattr(sx, "_inspection_delegate_argv", lambda _paths: None)

    facts, errors = _inspect(tmp_path)

    # Still fail-closed, but distinguishable from a bad artifact.
    assert facts == {}
    assert errors == ["openmm_artifact_inspection_unavailable"]


def test_unparsable_child_output_is_unavailable(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(sx._INSPECTION_INPROCESS_ENV, raising=False)
    monkeypatch.setattr(sx, "_missing_inspection_dependencies", lambda: ["mdtraj"])
    monkeypatch.setattr(
        sx,
        "_inspection_delegate_argv",
        lambda _paths: [sys.executable, "-c", "print('no sentinel here')"],
    )

    facts, errors = _inspect(tmp_path)

    assert facts == {}
    assert errors == ["openmm_artifact_inspection_unavailable"]


def test_delegate_argv_binds_repo_root_and_artifact_directories(
    tmp_path: Path,
    monkeypatch,
):
    sif = tmp_path / "mdclaw.sif"
    sif.write_text("stub")
    monkeypatch.setenv("MDCLAW_SIF", str(sif))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(
        sx.shutil,
        "which",
        lambda name: "/usr/bin/singularity" if name == "singularity" else None,
    )
    artifact_dir = tmp_path / "nodes" / "prod_001" / "artifacts"
    artifact_dir.mkdir(parents=True)

    argv = sx._inspection_delegate_argv([artifact_dir, artifact_dir])

    assert argv is not None
    assert argv[0] == "singularity"
    assert str(sif) in argv
    assert "--bind" in argv
    assert f"{sx._REPO_ROOT}:{sx._REPO_ROOT}" in argv
    assert f"{artifact_dir}:{artifact_dir}" in argv
    # Repeated directories are bound once.
    assert argv.count(f"{artifact_dir}:{artifact_dir}") == 1
    assert f"PYTHONPATH={sx._REPO_ROOT}" in argv
    assert f"{sx._INSPECTION_INPROCESS_ENV}=1" in argv
    assert argv[-3:] == ["python", "-m", "mdclaw.benchmark.study_execution_v2"]


def test_delegate_argv_is_none_without_a_container_runtime(
    tmp_path: Path,
    monkeypatch,
):
    sif = tmp_path / "mdclaw.sif"
    sif.write_text("stub")
    monkeypatch.setenv("MDCLAW_SIF", str(sif))
    monkeypatch.setattr(sx.shutil, "which", lambda _name: None)

    assert sx._inspection_delegate_argv([]) is None


def test_parse_delegated_result_rejects_malformed_payloads():
    sentinel = sx._INSPECTION_RESULT_SENTINEL
    assert sx._parse_delegated_inspection_result("") is None
    assert sx._parse_delegated_inspection_result(sentinel + "{oops") is None
    assert sx._parse_delegated_inspection_result(sentinel + '["list"]') is None
    assert sx._parse_delegated_inspection_result(sentinel + '{"errors": []}') is None
    parsed = sx._parse_delegated_inspection_result(
        sentinel + json.dumps({"runtime_facts": {"a": 1}, "errors": ["boom"]})
    )
    assert parsed == ({"a": 1}, ["boom"])
