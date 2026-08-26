"""Which mdclaw a compute node runs.

bin/mdclaw binds its package root and exports PYTHONPATH so the container runs
the same source as the host, treating the image as a dependency layer. The
sbatch that submit_job generates did not, so a fix landed on the login node and
the job kept running the baked package -- silently, because both are called
mdclaw and both work.

Default is the image, so a queued job is unaffected by later edits and an
install with no bindable source root still works. Overlay is opt-in, and is
refused rather than quietly downgraded where there is nothing safe to bind.
"""

import json
from types import SimpleNamespace

import pytest

from mdclaw.slurm.cluster import configure_container
from mdclaw.slurm.config import (
    _build_singularity_command,
    resolve_container_source,
    resolve_overlay_source_root,
)

CONTAINER = {"image": "/images/mdclaw.sif", "extra_flags": "--nv"}
COMMAND = "mdclaw --job-dir /work/job --node-id min_001 run_minimization"


def build(container, tmp_path):
    return _build_singularity_command(COMMAND, container, str(tmp_path))


def test_the_default_runs_the_image(tmp_path):
    # No source_mode at all: the command must look exactly as it did before
    # this option existed.
    cmd = build(dict(CONTAINER), tmp_path)

    assert "--env PYTHONPATH" not in cmd
    assert cmd.count("singularity exec") == 1


def test_image_mode_runs_the_image(tmp_path):
    cmd = build({**CONTAINER, "source_mode": "image"}, tmp_path)

    assert "--env PYTHONPATH" not in cmd


def test_overlay_binds_the_source_root_and_puts_it_on_pythonpath(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    cmd = build({**CONTAINER, "source_mode": "overlay", "source_root": str(root)},
                tmp_path)

    assert f"--env PYTHONPATH={root}" in cmd
    assert str(root) in cmd.split("--bind ")[1].split(" ")[0]


def test_building_overlay_without_a_resolved_root_raises(tmp_path):
    # Every submission path resolves the root first. If one ever stops doing
    # so, emitting an image-mode command silently would hide it.
    with pytest.raises(ValueError, match="not resolved"):
        build({**CONTAINER, "source_mode": "overlay"}, tmp_path)


def test_the_root_is_resolved_per_submission_not_read_from_config(tmp_path):
    # A config written from one checkout must not bind that checkout into a job
    # submitted from another: the stored value is ignored and replaced.
    if resolve_overlay_source_root() is None:
        pytest.skip("this mdclaw is not installed as a checkout or plugin")
    container = {**CONTAINER, "source_mode": "overlay",
                 "source_root": "/somewhere/else/entirely"}

    assert resolve_container_source(container) is None
    assert container["source_root"] == resolve_overlay_source_root()


def test_resolution_refuses_when_there_is_no_source_root(monkeypatch):
    monkeypatch.setattr("mdclaw.slurm.config.resolve_overlay_source_root",
                        lambda: None)
    container = {**CONTAINER, "source_mode": "overlay"}

    error = resolve_container_source(container)

    assert error is not None
    assert error["code"] == "container_overlay_source_unavailable"
    assert "source_root" not in container


def test_image_mode_needs_no_resolution():
    container = {**CONTAINER, "source_mode": "image"}

    assert resolve_container_source(container) is None
    assert "source_root" not in container


def test_the_overlay_root_is_the_directory_holding_bin_mdclaw():
    # .git is absent from a plugin install and pyproject.toml is not guaranteed
    # there either; bin/mdclaw is the overlay contract itself.
    root = resolve_overlay_source_root()
    if root is None:
        pytest.skip("this mdclaw is not installed as a checkout or plugin")
    from pathlib import Path
    assert (Path(root) / "bin" / "mdclaw").is_file()
    assert (Path(root) / "mdclaw" / "__init__.py").is_file()


def _config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path / ".mdclaw_cluster.json"


def test_configure_container_records_the_mode(tmp_path, monkeypatch):
    path = _config(tmp_path, monkeypatch)
    result = configure_container(image="/images/mdclaw.sif", source_mode="image")

    assert result["success"], result["errors"]
    assert result["container"]["source_mode"] == "image"
    assert json.loads(path.read_text())["container"]["source_mode"] == "image"


def test_an_unknown_mode_is_refused(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    result = configure_container(image="/images/mdclaw.sif", source_mode="checkout")

    assert not result["success"]
    assert result["code"] == "container_source_mode_invalid"


def test_configure_stores_the_mode_but_never_a_source_root(tmp_path, monkeypatch):
    # Storing the root is what would let a config written from checkout A bind
    # A into a job submitted from checkout B.
    path = _config(tmp_path, monkeypatch)
    result = configure_container(image="/images/mdclaw.sif", source_mode="overlay")

    assert result["success"], result["errors"]
    assert result["container"]["source_mode"] == "overlay"
    assert "source_root" not in result["container"]
    assert "source_root" not in json.loads(path.read_text())["container"]


def test_configure_accepts_overlay_even_where_it_could_not_run_here(
        tmp_path, monkeypatch):
    # The config may be written on a machine with no checkout and submitted
    # from one that has it, so configure must not reject on local eligibility.
    _config(tmp_path, monkeypatch)
    monkeypatch.setattr("mdclaw.slurm.config.resolve_overlay_source_root",
                        lambda: None)
    result = configure_container(image="/images/mdclaw.sif", source_mode="overlay")

    assert result["success"], result["errors"]


def _submit_with(monkeypatch, tmp_path, container, **kwargs):
    """Run submit_job far enough to reach the container block, never sbatch."""
    from mdclaw.slurm import submit as submit_mod

    calls = []
    real = submit_mod.resolve_container_source

    def spy(c):
        calls.append(c)
        return real(c)

    monkeypatch.setattr(submit_mod, "resolve_container_source", spy)
    monkeypatch.setattr(submit_mod, "_get_container_config",
                        lambda config=None: container)
    monkeypatch.setattr("mdclaw.slurm._base.check_external_tool",
                        lambda *a, **k: True)
    monkeypatch.setattr("mdclaw.slurm._base.run_command",
                        lambda *a, **k: SimpleNamespace(
                            returncode=0, stdout="Submitted batch job 1", stderr=""))
    monkeypatch.chdir(tmp_path)
    result = submit_mod.submit_job(script="echo hello", job_name="j",
                                   output_dir=str(tmp_path), **kwargs)
    return result, calls


def test_a_container_job_resolves_the_source(tmp_path, monkeypatch):
    # Positive control: without this the next test could pass vacuously.
    _result, calls = _submit_with(
        monkeypatch, tmp_path, {**CONTAINER, "source_mode": "image"})

    assert calls, "submit_job never reached container source resolution"


def test_an_explicit_environment_skips_container_resolution(tmp_path, monkeypatch):
    # An explicit environment takes precedence over container execution, so the
    # job never enters the container. An overlay setting left in the config must
    # not reject it -- which it would for anyone without a bindable checkout.
    monkeypatch.setattr("mdclaw.slurm.config.resolve_overlay_source_root",
                        lambda: None)
    result, calls = _submit_with(
        monkeypatch, tmp_path, {**CONTAINER, "source_mode": "overlay"},
        environment="module load cuda")

    assert result.get("code") != "container_overlay_source_unavailable"
    assert not calls, "resolution ran even though the container is not used"
