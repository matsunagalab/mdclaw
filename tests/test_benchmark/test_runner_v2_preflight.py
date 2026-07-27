"""Runner-side binding tests for MDStudyBench v2 confirmatory inputs."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mdclaw.benchmark.run import (
    _directory_sha256,
    _execute_v2_confirmatory_plan,
    _materialize_v2_evaluator_submission,
    _normalize_v2_confirmatory_plan_runs,
    _preflight_v2_pending_inputs,
    _snapshot_v2_event_artifacts,
    _v2_preflight_input_binding_errors,
    _write_mdclaw_runtime_wrapper,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_preflight_hashes_are_bound_to_post_run_inspection(
    tmp_path: Path,
    monkeypatch,
):
    work_dir = tmp_path / "work"
    inputs_by_job: dict[Path, dict[str, Path]] = {}
    for role in ("reference", "variant"):
        job_dir = work_dir / role
        inputs = job_dir / "trusted-inputs"
        inputs.mkdir(parents=True)
        paths = {
            "base_system": inputs / "system.xml",
            "topology": inputs / "topology.pdb",
            "start_state": inputs / "state.xml",
        }
        paths["base_system"].write_text("base-system")
        paths["topology"].write_text("topology")
        paths["start_state"].write_text("start-state")
        inputs_by_job[job_dir.resolve()] = paths

    def fake_resolve_node_inputs(
        job_dir: str,
        node_id: str,
        node_type: str,
    ) -> dict[str, str]:
        assert Path(job_dir).is_absolute()
        assert node_id in {"prod_001", "prod_002"}
        assert node_type == "prod"
        paths = inputs_by_job[Path(job_dir)]
        return {
            "system_xml_file": str(paths["base_system"]),
            "topology_pdb_file": str(paths["topology"]),
            "restart_from": str(paths["start_state"]),
        }

    monkeypatch.setattr(
        "mdclaw.node.inputs.resolve_node_inputs",
        fake_resolve_node_inputs,
    )
    runs = [
        {
            "run_id": "reference-1",
            "job_dir": work_dir / "reference",
            "node_id": "prod_001",
        },
        {
            "run_id": "variant-1",
            "job_dir": work_dir / "variant",
            "node_id": "prod_002",
        },
    ]
    errors: list[dict[str, str]] = []

    _preflight_v2_pending_inputs(
        runs,
        errors,
        solver_work_dir=work_dir,
    )

    assert errors == []
    reference_paths = inputs_by_job[(work_dir / "reference").resolve()]
    expected = {
        name: _sha256(path)
        for name, path in reference_paths.items()
    }
    assert all(run["preflight_input_sha256"] == expected for run in runs)
    event = {
        "input_artifacts": {
            name: {"sha256": digest}
            for name, digest in expected.items()
        }
    }
    assert _v2_preflight_input_binding_errors(
        spec=runs[0],
        event=event,
    ) == []

    event["input_artifacts"]["base_system"]["sha256"] = "0" * 64
    assert _v2_preflight_input_binding_errors(
        spec=runs[0],
        event=event,
    ) == ["confirmatory_base_system_changed_after_preflight"]


def test_preflight_requires_a_resolved_start_state(
    tmp_path: Path,
    monkeypatch,
):
    base = tmp_path / "system.xml"
    topology = tmp_path / "topology.pdb"
    base.write_text("base-system")
    topology.write_text("topology")

    monkeypatch.setattr(
        "mdclaw.node.inputs.resolve_node_inputs",
        lambda *_args: {
            "system_xml_file": str(base),
            "topology_pdb_file": str(topology),
        },
    )
    runs = [
        {
            "run_id": "reference-1",
            "job_dir": tmp_path,
            "node_id": "prod_001",
        }
    ]
    errors: list[dict[str, str]] = []

    _preflight_v2_pending_inputs(
        runs,
        errors,
        solver_work_dir=tmp_path,
    )

    assert {error["code"] for error in errors} == {
        "confirmatory_start_state_missing"
    }


def test_confirmatory_commands_use_empty_runner_owned_cwd(
    tmp_path: Path,
    monkeypatch,
):
    task_id = "S01"
    private_dir = tmp_path / "private"
    solver_task_dir = tmp_path / "solver" / "tasks" / task_id
    solver_work_dir = tmp_path / "solver" / "work"
    solver_submission = solver_task_dir / "submission"
    task_run_dir = tmp_path / "run" / "tasks" / task_id
    runner_source = task_run_dir / "runner_source"
    for directory in (
        private_dir / "tasks" / task_id,
        solver_task_dir,
        solver_work_dir,
        solver_submission,
        task_run_dir,
        runner_source,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (private_dir / "tasks" / task_id / "task.json").write_text(
        json.dumps(
            {
                "scientific_target": {
                    "required_conditions": {
                        "temperature_k": 300.0,
                        "reference_pressure_mpa": 0.1,
                        "test_pressure_mpa": 100.0,
                    },
                    "primary_evidence_contract": {
                        "fixed_observable_parameters": {
                            "minimum_confirmatory_time_ns_per_condition": 0.0,
                        }
                    },
                }
            }
        )
    )
    plan_path = solver_submission / "confirmatory_plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "task_id": task_id,
                "runs": [
                    {
                        "run_id": f"{role}-1",
                        "condition_role": role,
                        "job_dir": role,
                        "node_id": "prod_001",
                        "simulation_time_ns": 0.01,
                    }
                    for role in ("reference", "variant")
                ],
            }
        )
    )
    (runner_source / "adapter.py").write_text("# trusted runner source\n")
    runner_wrapper = task_run_dir / "runner_bin" / "mdclaw"
    runner_wrapper.parent.mkdir(parents=True)
    runner_wrapper.write_text("#!/bin/sh\nexit 0\n")

    normalized_runs = [
        {
            "run_id": f"{role}-1",
            "production_event_id": f"runner-prod-{index:03d}",
            "condition_role": role,
            "job_dir": solver_work_dir / role,
            "node_id": "prod_001",
            "simulation_time_ns": 0.01,
        }
        for index, role in enumerate(("reference", "variant"), start=1)
    ]
    monkeypatch.setattr(
        "mdclaw.benchmark.run._normalize_v2_confirmatory_plan_runs",
        lambda *_args, **_kwargs: normalized_runs,
    )
    monkeypatch.setattr(
        "mdclaw.benchmark.run._preflight_v2_pending_inputs",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "mdclaw.benchmark.run._v2_preflight_input_binding_errors",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        "mdclaw.benchmark.study_execution_v2.inspect_mdclaw_production_node_v2",
        lambda **_kwargs: {
            "valid": True,
            "reason_codes": [],
            "input_artifacts": {},
            "output_artifacts": {},
        },
    )
    monkeypatch.setattr(
        "mdclaw.benchmark.run._snapshot_v2_event_artifacts",
        lambda **_kwargs: [],
    )
    observed_cwds: list[Path] = []

    def fake_run_runner_owned_command(
        _command,
        *,
        cwd,
        **_kwargs,
    ):
        observed_cwds.append(cwd)
        assert cwd.parent == task_run_dir
        assert cwd.name.startswith("confirmatory_runner_cwd_")
        assert list(cwd.iterdir()) == []
        return {"exit_code": 0, "timed_out": False}

    monkeypatch.setattr(
        "mdclaw.benchmark.run._run_runner_owned_command",
        fake_run_runner_owned_command,
    )

    result = _execute_v2_confirmatory_plan(
        task_id=task_id,
        run_id="run-1",
        private_dir=private_dir,
        solver_task_dir=solver_task_dir,
        solver_work_dir=solver_work_dir,
        solver_submission=solver_submission,
        task_run_dir=task_run_dir,
        mdclaw_wrapper_path=runner_wrapper,
        runner_source_path=runner_source,
        runner_source_sha256=_directory_sha256(runner_source),
        run_env={},
        task_started_wall=0.0,
        timeout_seconds=None,
    )

    assert result is not None
    assert result["success"] is True
    assert len(observed_cwds) == 2
    assert len(set(observed_cwds)) == 2
    assert all(not cwd.exists() for cwd in observed_cwds)
    assert all(not cwd.is_relative_to(solver_task_dir) for cwd in observed_cwds)
    assert all(not cwd.is_relative_to(solver_work_dir) for cwd in observed_cwds)
    assert (task_run_dir / "frozen_confirmatory_plan.json").read_bytes() == (
        plan_path.read_bytes()
    )
    assert (solver_task_dir / "confirmatory_result.json").is_file()


def test_confirmatory_job_dirs_must_be_relative_to_work_dir(tmp_path: Path):
    work_dir = tmp_path / "work"
    for role in ("reference", "variant"):
        node_dir = work_dir / role / "nodes" / "prod_001"
        node_dir.mkdir(parents=True)
        (node_dir / "node.json").write_text(
            json.dumps({"node_type": "prod", "status": "pending"})
        )
    runs = [
        {
            "run_id": f"{role}-1",
            "condition_role": role,
            "job_dir": (
                str((work_dir / role).resolve())
                if role == "reference"
                else role
            ),
            "node_id": "prod_001",
            "simulation_time_ns": 0.01,
        }
        for role in ("reference", "variant")
    ]
    errors: list[dict[str, str]] = []

    normalized = _normalize_v2_confirmatory_plan_runs(
        runs,
        solver_work_dir=work_dir,
        errors=errors,
    )

    assert [item["condition_role"] for item in normalized] == ["variant"]
    assert "confirmatory_job_dir_must_be_relative" in {
        item["code"] for item in errors
    }


@pytest.mark.parametrize("job_dir", [".", "./reference", "other/../reference"])
def test_confirmatory_job_dirs_reject_dot_path_components(
    tmp_path: Path,
    job_dir: str,
):
    work_dir = tmp_path / "work"
    node_dir = work_dir / "reference" / "nodes" / "prod_001"
    node_dir.mkdir(parents=True)
    (node_dir / "node.json").write_text(
        json.dumps({"node_type": "prod", "status": "pending"})
    )
    errors: list[dict[str, str]] = []

    normalized = _normalize_v2_confirmatory_plan_runs(
        [
            {
                "run_id": "reference-1",
                "condition_role": "reference",
                "job_dir": job_dir,
                "node_id": "prod_001",
                "simulation_time_ns": 0.01,
            }
        ],
        solver_work_dir=work_dir,
        errors=errors,
    )

    assert normalized == []
    assert "confirmatory_job_dir_invalid" in {
        item["code"] for item in errors
    }


@pytest.mark.parametrize(
    ("link_level", "expected_code"),
    [
        ("job", "confirmatory_job_path_symlink"),
        ("nodes", "confirmatory_node_path_symlink"),
        ("node", "confirmatory_node_path_symlink"),
        ("node_json", "confirmatory_node_path_symlink"),
    ],
)
def test_confirmatory_node_lexical_paths_must_not_use_symlinks(
    tmp_path: Path,
    link_level: str,
    expected_code: str,
):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    outside = tmp_path / "outside"
    outside_node = outside / "nodes" / "prod_001"
    outside_node.mkdir(parents=True)
    outside_node_json = outside_node / "node.json"
    outside_node_json.write_text(
        json.dumps({"node_type": "prod", "status": "pending"})
    )
    job_dir = work_dir / "reference"

    if link_level == "job":
        job_dir.symlink_to(outside, target_is_directory=True)
    else:
        job_dir.mkdir()
        nodes_dir = job_dir / "nodes"
        if link_level == "nodes":
            nodes_dir.symlink_to(
                outside / "nodes",
                target_is_directory=True,
            )
        else:
            nodes_dir.mkdir()
            node_dir = nodes_dir / "prod_001"
            if link_level == "node":
                node_dir.symlink_to(
                    outside_node,
                    target_is_directory=True,
                )
            else:
                node_dir.mkdir()
                (node_dir / "node.json").symlink_to(outside_node_json)

    errors: list[dict[str, str]] = []
    normalized = _normalize_v2_confirmatory_plan_runs(
        [
            {
                "run_id": "reference-1",
                "condition_role": "reference",
                "job_dir": "reference",
                "node_id": "prod_001",
                "simulation_time_ns": 0.01,
            }
        ],
        solver_work_dir=work_dir,
        errors=errors,
    )

    assert normalized == []
    assert expected_code in {item["code"] for item in errors}


@pytest.mark.parametrize(
    "artifact_name",
    ["base_system", "topology", "start_state"],
)
@pytest.mark.parametrize("escape_kind", ["symlink", "outside"])
def test_preflight_inputs_must_stay_in_canonical_job_without_symlinks(
    tmp_path: Path,
    monkeypatch,
    artifact_name: str,
    escape_kind: str,
):
    work_dir = tmp_path / "work"
    job_dir = work_dir / "reference"
    inputs = job_dir / "trusted-inputs"
    inputs.mkdir(parents=True)
    paths = {
        "base_system": inputs / "system.xml",
        "topology": inputs / "topology.pdb",
        "start_state": inputs / "state.xml",
    }
    for name, path in paths.items():
        path.write_text(f"trusted-{name}")

    outside = tmp_path / "outside" / paths[artifact_name].name
    outside.parent.mkdir()
    outside.write_text(f"outside-{artifact_name}")
    if escape_kind == "symlink":
        paths[artifact_name].unlink()
        paths[artifact_name].symlink_to(outside)
    else:
        paths[artifact_name] = outside

    monkeypatch.setattr(
        "mdclaw.node.inputs.resolve_node_inputs",
        lambda *_args: {
            "system_xml_file": str(paths["base_system"]),
            "topology_pdb_file": str(paths["topology"]),
            "restart_from": str(paths["start_state"]),
        },
    )
    runs = [
        {
            "run_id": "reference-1",
            "job_dir": job_dir.resolve(),
            "node_id": "prod_001",
        }
    ]
    errors: list[dict[str, str]] = []

    _preflight_v2_pending_inputs(
        runs,
        errors,
        solver_work_dir=work_dir,
    )

    suffix = "symlink" if escape_kind == "symlink" else "outside_job_dir"
    assert f"confirmatory_{artifact_name}_{suffix}" in {
        item["code"] for item in errors
    }


def test_event_artifacts_are_snapshotted_with_episode_relative_paths(
    tmp_path: Path,
):
    sources = tmp_path / "sources"
    sources.mkdir()
    input_names = ("base_system", "topology", "start_state")
    output_names = (
        "trajectory",
        "state",
        "energy",
        "runtime_system",
        "integrator",
    )
    paths: dict[str, Path] = {}
    for name in (*input_names, *output_names):
        path = sources / f"{name}.dat"
        path.write_bytes(f"trusted-{name}".encode())
        paths[name] = path
    event = {
        "job_dir": str(tmp_path / "agent-job"),
        "input_artifacts": {
            name: {
                "path": str(paths[name]),
                "sha256": _sha256(paths[name]),
                "bytes": paths[name].stat().st_size,
            }
            for name in input_names
        },
        "output_artifacts": {
            name: {
                "path": str(paths[name]),
                "sha256": _sha256(paths[name]),
                "bytes": paths[name].stat().st_size,
            }
            for name in output_names
        },
    }
    episode_root = tmp_path / "episode"

    errors = _snapshot_v2_event_artifacts(
        event=event,
        episode_root=episode_root,
        sequence=1,
    )

    assert errors == []
    assert "job_dir" not in event
    for group in ("input_artifacts", "output_artifacts"):
        for record in event[group].values():
            relative = Path(record["path"])
            assert not relative.is_absolute()
            captured = episode_root / relative
            assert captured.is_file()
            assert _sha256(captured) == record["sha256"]
            assert captured.stat().st_size == record["bytes"]


def test_v2_materialization_ignores_agent_manifest_episode_and_changed_plan(
    tmp_path: Path,
):
    task_run_dir = tmp_path / "task-run"
    solver_submission = tmp_path / "solver-submission"
    evaluator_submission = task_run_dir / "submission"
    episode_root = task_run_dir / "episode_work"
    (episode_root / "artifacts" / "001").mkdir(parents=True)
    solver_submission.mkdir(parents=True)
    frozen = b'{"schema_version":"1.0","task_id":"S01","runs":[]}\n'
    (task_run_dir / "frozen_confirmatory_plan.json").write_bytes(frozen)
    (episode_root / "episode.json").write_text(
        json.dumps({"recorded_by": "mdclaw_benchmark_runner"})
    )
    (episode_root / "artifacts" / "001" / "runner.dat").write_text("runner")
    (solver_submission / "confirmatory_plan.json").write_text('{"changed":true}')
    (solver_submission / "claim.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "task_id": "S01",
                "status": "unresolved",
                "outcome": None,
            }
        )
    )
    (solver_submission / "manifest.json").write_text('{"agent":"owned"}')
    (solver_submission / "episode").mkdir()
    (solver_submission / "episode" / "episode.json").write_text(
        '{"recorded_by":"agent"}'
    )

    _materialize_v2_evaluator_submission(
        task_id="S01",
        run_id="run-1",
        solver_submission=solver_submission,
        task_run_dir=task_run_dir,
        evaluator_submission=evaluator_submission,
    )

    assert (evaluator_submission / "confirmatory_plan.json").read_bytes() == frozen
    assert json.loads(
        (evaluator_submission / "episode" / "episode.json").read_text()
    ) == {"recorded_by": "mdclaw_benchmark_runner"}
    manifest = json.loads((evaluator_submission / "manifest.json").read_text())
    assert manifest["generated_by"]["tool"] == "mdclaw_benchmark_runner"
    assert manifest["outputs"] == {
        "confirmatory_plan": "confirmatory_plan.json",
        "claim": "claim.json",
        "episode": "episode/episode.json",
    }


def test_runtime_wrapper_cannot_import_solver_cwd_mdclaw_shadow(
    tmp_path: Path,
):
    trusted_source = tmp_path / "trusted_source"
    trusted_package = trusted_source / "mdclaw"
    trusted_package.mkdir(parents=True)
    (trusted_package / "__init__.py").write_text("")
    (trusted_package / "_cli.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['TRUSTED_MARKER']).write_text('trusted')\n"
    )

    solver_cwd = tmp_path / "solver_cwd"
    fake_package = solver_cwd / "mdclaw"
    fake_package.mkdir(parents=True)
    (fake_package / "__init__.py").write_text("")
    (fake_package / "_cli.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['FAKE_MARKER']).write_text('fake')\n"
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_conda = fake_bin / "conda"
    fake_conda.write_text(
        "#!/usr/bin/env bash\n"
        "while [[ \"$#\" -gt 0 && \"$1\" != \"python\" ]]; do shift; done\n"
        "[[ \"$#\" -gt 0 ]] || exit 2\n"
        "shift\n"
        f"exec {sys.executable} \"$@\"\n"
    )
    fake_conda.chmod(0o755)

    wrapper = tmp_path / "runner_bin" / "mdclaw"
    _write_mdclaw_runtime_wrapper(
        wrapper,
        mdclaw_runtime="conda",
        source_root=trusted_source,
        work_root=solver_cwd,
    )
    trusted_marker = tmp_path / "trusted.marker"
    fake_marker = tmp_path / "fake.marker"
    user_site_marker = tmp_path / "user-site.marker"
    user_base = tmp_path / "user-base"
    user_site = (
        user_base
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    user_site.mkdir(parents=True)
    (user_site / "sitecustomize.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['USER_SITE_MARKER']).write_text('imported')\n"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "PYTHONPATH": str(solver_cwd),
            "TRUSTED_MARKER": str(trusted_marker),
            "FAKE_MARKER": str(fake_marker),
            "PYTHONUSERBASE": str(user_base),
            "USER_SITE_MARKER": str(user_site_marker),
        }
    )

    completed = subprocess.run(
        [str(wrapper)],
        cwd=solver_cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert trusted_marker.read_text() == "trusted"
    assert not fake_marker.exists()
    assert not user_site_marker.exists()
