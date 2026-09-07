"""Exercise Slurm client discovery and subprocess environments without Slurm."""

import json
import os
import sys

import pytest

from mdclaw.slurm import _base


@pytest.fixture(autouse=True)
def no_inherited_slurm_path(monkeypatch):
    monkeypatch.delenv("MDCLAW_SLURM_PATH", raising=False)
    monkeypatch.delenv("SINGULARITY_CONTAINER", raising=False)
    monkeypatch.delenv("APPTAINER_CONTAINER", raising=False)


def _client(directory, name="sbatch"):
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / name
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'executable': sys.argv[0], 'args': sys.argv[1:], 'env': dict(os.environ)}))\n"
    )
    executable.chmod(0o755)
    return executable


@pytest.mark.parametrize("name", sorted(_base._SLURM_CLIENTS))
def test_discovers_arbitrary_path_and_preserves_arguments(tmp_path, monkeypatch, name):
    selected = _client(tmp_path / "first installation with spaces", name)
    other = _client(tmp_path / "second installation", name)
    monkeypatch.setenv("PATH", os.pathsep.join((str(selected.parent), str(other.parent))))

    assert _base.check_external_tool(name)
    command = [name, "a script with spaces.sh", "literal; $not_a_shell"]
    result = json.loads(_base.run_command(command).stdout)
    assert result["executable"] == str(selected)
    assert result["args"] == command[1:]
    assert command[0] == name


def test_explicit_path_takes_priority_over_path(tmp_path, monkeypatch):
    default = _client(tmp_path / "default")
    selected = _client(tmp_path / "explicit location")
    monkeypatch.setenv("PATH", str(default.parent))
    monkeypatch.setenv("MDCLAW_SLURM_PATH", str(selected.parent))

    assert _base.check_external_tool("sbatch")
    assert json.loads(_base.run_command(["sbatch"]).stdout)["executable"] == str(selected)


@pytest.mark.parametrize("explicit_path", ["", "missing directory"])
def test_explicit_path_never_falls_back(tmp_path, monkeypatch, explicit_path):
    default = _client(tmp_path / "default")
    monkeypatch.setenv("PATH", str(default.parent))
    monkeypatch.setenv("MDCLAW_SLURM_PATH", explicit_path)

    assert not _base.check_external_tool("sbatch")
    with pytest.raises(FileNotFoundError, match="sbatch"):
        _base.run_command(["sbatch"])


@pytest.mark.parametrize("env_key", ["PATH", "MDCLAW_SLURM_PATH"])
def test_subprocess_search_path_override_is_used(tmp_path, monkeypatch, env_key):
    default = _client(tmp_path / "default")
    selected = _client(tmp_path / "subprocess override")
    monkeypatch.setenv("PATH", str(default.parent))
    supplied_env = {env_key: str(selected.parent)}

    result = json.loads(_base.run_command(["sbatch"], env=supplied_env).stdout)
    assert result["executable"] == str(selected)
    assert supplied_env == {env_key: str(selected.parent)}


@pytest.mark.parametrize("name", ["sbatch", "squeue", "sacct", "scancel", "sinfo", "scontrol"])
@pytest.mark.parametrize("container_variable", ["SINGULARITY_CONTAINER", "APPTAINER_CONTAINER"])
def test_only_container_sbatch_clears_bind_environment(tmp_path, monkeypatch, name, container_variable):
    selected = _client(tmp_path / "clients", name)
    monkeypatch.setenv("PATH", str(selected.parent))
    monkeypatch.setenv(container_variable, "/images/mdclaw.sif")
    for key in _base._CONTAINER_BIND_ENV:
        monkeypatch.setenv(key, "/submission/host/library:/container/library")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "2")
    monkeypatch.setenv("EXAMPLE_TOKEN", "not-a-secret-test-value")
    supplied_env = {"SINGULARITY_BIND": "/explicit:/explicit", "ADDED_VARIABLE": "preserved"}

    result = json.loads(_base.run_command([name], env=supplied_env).stdout)["env"]
    for key in _base._CONTAINER_BIND_ENV:
        expected = "" if name == "sbatch" else supplied_env.get(key, os.environ[key])
        assert result[key] == expected
        assert os.environ[key] == "/submission/host/library:/container/library"
    assert result["CUDA_VISIBLE_DEVICES"] == "2"
    assert result["EXAMPLE_TOKEN"] == "not-a-secret-test-value"
    assert result["ADDED_VARIABLE"] == "preserved"
    assert supplied_env["SINGULARITY_BIND"] == "/explicit:/explicit"


def test_native_sbatch_preserves_explicit_bind_environment(tmp_path, monkeypatch):
    selected = _client(tmp_path / "native clients")
    monkeypatch.setenv("PATH", str(selected.parent))
    for key in _base._CONTAINER_BIND_ENV:
        monkeypatch.setenv(key, "/native/host:/container")
    supplied_env = {"SINGULARITY_BIND": "/override:/override"}

    result = json.loads(_base.run_command(["sbatch"], env=supplied_env).stdout)["env"]
    for key in _base._CONTAINER_BIND_ENV:
        assert result[key] == supplied_env.get(key, os.environ[key])


def test_relative_search_path_is_resolved_before_changing_cwd(tmp_path, monkeypatch):
    selected = _client(tmp_path / "relative clients")
    other_cwd = tmp_path / "other cwd"
    other_cwd.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "relative clients")

    assert _base.check_external_tool("sbatch")
    assert json.loads(_base.run_command(["sbatch"], cwd=other_cwd).stdout)["executable"] == str(selected)


def test_non_slurm_commands_keep_common_behavior(tmp_path, monkeypatch):
    selected = _client(tmp_path / "ordinary clients", "ordinary-tool")
    monkeypatch.setenv("PATH", str(selected.parent))
    monkeypatch.setenv("MDCLAW_SLURM_PATH", "/missing/slurm")
    monkeypatch.setenv("SINGULARITY_BIND", "/retained:/retained")
    checks = []
    monkeypatch.setattr(_base, "_check_external_tool", lambda name: checks.append(name) or True)

    assert _base.check_external_tool("ordinary-tool")
    assert checks == ["ordinary-tool"]
    result = json.loads(_base.run_command(["ordinary-tool", "space in arg"]).stdout)
    assert result["executable"] == str(selected)
    assert result["args"] == ["space in arg"]
    assert result["env"]["SINGULARITY_BIND"] == "/retained:/retained"
