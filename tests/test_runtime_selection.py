"""Exercise setup, launcher and doctor with multiple installed runtimes.

Fake runtimes log argv and simulate process failures; no containers are pulled
and no scientific calculations or Slurm jobs are submitted by these tests.
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = ("bin/mdclaw", "scripts/setup-container.sh", "scripts/mdclaw-doctor.sh")


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / "checkout with spaces"
    (root / "bin").mkdir(parents=True)
    (root / "scripts").mkdir()
    for name in ENTRYPOINTS:
        shutil.copy2(ROOT / name, root / name)
    shutil.copytree(ROOT / "scripts/_internal", root / "scripts/_internal")
    (root / "pixi.toml").write_text('[workspace]\nname="test"\n')
    (root / ".pixi/envs/default").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin/plugin.json").write_text('{"version":"0.6.8"}')
    (root / "mdclaw.sif").write_text("existing SIF")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "calls.jsonl"
    env = {key: val for key, val in os.environ.items()
           if not key.startswith(("MDCLAW_", "CLAUDE_PLUGIN_"))}
    env.update(PATH=f"{stubs}:/usr/bin:/bin", CALL_LOG=str(calls))
    stub = f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
name, args = Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as out:
    out.write(json.dumps([name, args]) + '\\n')
if name == 'conda' and args == ['env', 'list']:
    print('mdclaw /fake/mdclaw')
elif name == 'docker' and args == ['info']:
    sys.exit(int(os.environ.get('DOCKER_OFFLINE', '0')))
elif name == 'docker' and args[:2] == ['image', 'inspect']:
    sys.exit(int(os.environ.get('IMAGE_MISSING', '0')))
elif name in ('singularity', 'apptainer') and args[0] == 'pull':
    Path(args[2]).write_text('downloaded SIF')
else:
    if any(arg.endswith('/scripts/_internal/doctor.py') for arg in args):
        print('[optional] PLUMED: unavailable')
        sys.exit(int(os.environ.get('PROBE_EXIT', '0')))
    print(name + ': ' + json.dumps(args))
'''
    for name in ("pixi", "conda", "singularity", "apptainer", "docker"):
        path = stubs / name
        path.write_text(stub)
        path.chmod(0o755)

    def run(entry, *args, **overrides):
        calls.write_text("")
        proc = subprocess.run(["/bin/bash", str(root / entry), *args],
                              env={**env, **overrides}, cwd=tmp_path,
                              text=True, capture_output=True, timeout=20)
        records = [json.loads(line) for line in calls.read_text().splitlines()]
        return proc, records

    return root, stubs, run


@pytest.mark.parametrize("runtime", ["", "pixi", "conda", "singularity", "apptainer", "docker"])
def test_same_choice_for_setup_launcher_and_doctor(deployment, runtime):
    root, _, run = deployment
    expected = runtime or "pixi"
    for entry in ENTRYPOINTS:
        proc, calls = run(entry, *(('--version',) if entry == 'bin/mdclaw' else ()),
                          MDCLAW_RUNTIME=runtime, MDCLAW_SIF=str(root / 'mdclaw.sif'),
                          MDCLAW_DOCKER_IMAGE="example/mdclaw:custom")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        if entry == "scripts/setup-container.sh" and expected in {"pixi", "conda"}:
            assert f"Using {expected}" in proc.stderr
            assert not any(name in {"docker", "singularity", "apptainer"} for name, _ in calls)
        elif entry == "scripts/setup-container.sh" and expected in {"singularity", "apptainer"}:
            assert "Using configured MDClaw SIF" in proc.stderr
            assert not calls  # shared image is preserved, even with another image configured
        else:
            assert calls and all(name == expected for name, _ in calls), calls
        if entry == "scripts/mdclaw-doctor.sh":
            assert f"Runtime: {expected}" in proc.stdout
            assert 'conda env' not in proc.stdout
            assert any(any(arg.endswith('/scripts/_internal/doctor.py') for arg in args)
                       for _, args in calls)
        if expected == "docker":
            assert any("example/mdclaw:custom" in args for _, args in calls)


@pytest.mark.parametrize("entry", ENTRYPOINTS)
def test_invalid_override_never_falls_back(deployment, entry):
    _, _, run = deployment
    proc, calls = run(entry, MDCLAW_RUNTIME="doker")
    assert proc.returncode != 0
    assert "unknown MDCLAW_RUNTIME" in proc.stderr
    assert "Next:" in proc.stderr
    assert not calls


@pytest.mark.parametrize("runtime", ["pixi", "conda", "singularity", "apptainer", "docker"])
@pytest.mark.parametrize("entry", ENTRYPOINTS)
def test_missing_selected_command_never_falls_back(deployment, runtime, entry):
    _, stubs, run = deployment
    (stubs / runtime).unlink()
    proc, calls = run(entry, MDCLAW_RUNTIME=runtime)
    assert proc.returncode != 0
    assert "Next:" in proc.stderr
    assert not calls


def test_missing_explicit_sif_does_not_use_existing_checkout_image(deployment):
    root, _, run = deployment
    selected = root / "shared/new image.sif"
    for entry in ("bin/mdclaw", "scripts/mdclaw-doctor.sh"):
        proc, calls = run(entry, MDCLAW_RUNTIME="singularity", MDCLAW_SIF=str(selected))
        assert proc.returncode != 0
        assert str(selected) in proc.stderr
        assert not calls
    proc, calls = run("scripts/setup-container.sh", MDCLAW_RUNTIME="singularity",
                      MDCLAW_SIF=str(selected))
    assert proc.returncode == 0, proc.stderr
    assert calls == [["singularity", ["pull", "--force", str(selected),
                                    "docker://ghcr.io/matsunagalab/mdclaw:0.6.8"]]]
    assert selected.read_text() == 'downloaded SIF'
    assert (root / 'mdclaw.sif').read_text() == 'existing SIF'


def test_auto_selects_same_container_before_and_after_setup(deployment):
    root, stubs, run = deployment
    (stubs / 'pixi').unlink()
    (stubs / 'conda').unlink()
    (root / 'mdclaw.sif').unlink()
    proc, _ = run('bin/mdclaw', '--version')
    assert proc.returncode != 0
    assert 'SIF is missing' in proc.stderr
    proc, calls = run('scripts/setup-container.sh')
    assert proc.returncode == 0, proc.stderr
    assert calls[0][0] == 'singularity'
    proc, calls = run('bin/mdclaw', '--version')
    assert proc.returncode == 0
    assert calls[0][0] == 'singularity'


@pytest.mark.parametrize('entry', ENTRYPOINTS)
def test_docker_daemon_failure_is_an_error(deployment, entry):
    _, _, run = deployment
    proc, calls = run(entry, MDCLAW_RUNTIME='docker', DOCKER_OFFLINE='1')
    assert proc.returncode != 0
    assert 'install/start Docker' in proc.stderr
    assert calls == [['docker', ['info']]]


@pytest.mark.parametrize('entry', ['bin/mdclaw', 'scripts/mdclaw-doctor.sh'])
def test_execution_does_not_download_an_absent_docker_image(deployment, entry):
    _, _, run = deployment
    proc, calls = run(entry, MDCLAW_RUNTIME='docker', IMAGE_MISSING='1')
    assert proc.returncode != 0
    assert 'setup-container.sh' in proc.stderr
    assert [args[0] for _, args in calls] == ['info', 'image']


@pytest.mark.parametrize('runtime', ['pixi', 'conda', 'singularity', 'apptainer', 'docker'])
def test_doctor_preserves_core_probe_failure(deployment, runtime):
    _, _, run = deployment
    proc, _ = run('scripts/mdclaw-doctor.sh', MDCLAW_RUNTIME=runtime, PROBE_EXIT='7')
    assert proc.returncode != 0
    assert f'Runtime: {runtime}' in proc.stdout
    assert '[optional] PLUMED: unavailable' in proc.stdout


def test_force_setup_does_not_override_explicit_runtime(deployment):
    _, _, run = deployment
    proc, calls = run('scripts/setup-container.sh', '9.9.9',
                      MDCLAW_RUNTIME='pixi', MDCLAW_FORCE_CONTAINER_SETUP='1')
    assert proc.returncode == 0
    assert 'Using pixi' in proc.stderr
    assert not calls


def test_force_setup_without_runtime_still_prepares_container(deployment):
    _, _, run = deployment
    proc, calls = run('scripts/setup-container.sh', '9.9.9')
    assert proc.returncode == 0
    assert calls[0][1][-1] == 'docker://ghcr.io/matsunagalab/mdclaw:9.9.9'
    assert 'export MDCLAW_RUNTIME=singularity' in proc.stderr


def test_local_wrapper_on_path_is_not_a_fallback_to_itself(deployment):
    root, stubs, run = deployment
    for path in stubs.iterdir():
        path.unlink()
    # File identity also covers aliases/symlinks to the same wrapper.
    (stubs / 'mdclaw').symlink_to(root / 'bin/mdclaw')
    proc, calls = run('bin/mdclaw', '--version')
    assert proc.returncode != 0
    assert 'no MDClaw runtime found' in proc.stderr
    assert not calls


def _probe_module():
    spec = importlib.util.spec_from_file_location('runtime_doctor', ROOT / 'scripts/_internal/doctor.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ambertools_broken_executable_is_not_ready(monkeypatch, capsys):
    module = _probe_module()
    monkeypatch.setattr(module.shutil, 'which', lambda name: f'/env/bin/{name}')

    def broken(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(module.subprocess, 'run', broken)
    assert module.check_ambertools() is False
    assert '[fail] AmberTools: cpptraj cannot start' in capsys.readouterr().out


def test_core_probe_failure_survives_optional_feature_report(monkeypatch):
    module = _probe_module()
    monkeypatch.setattr(module, 'report_version_sync', lambda: None)
    monkeypatch.setattr(module, 'check_openmm', lambda: False)
    monkeypatch.setattr(module, 'check_ambertools', lambda: True)
    monkeypatch.setattr(module, 'report_optional_features', lambda: None)
    assert module.main() == 1


def test_registered_openmm_platforms_that_cannot_run_are_not_ready(monkeypatch, capsys):
    from types import SimpleNamespace

    module = _probe_module()

    class Platform:
        @staticmethod
        def getNumPlatforms():
            return 1

        @staticmethod
        def getPlatform(index):
            return SimpleNamespace(getName=lambda: 'CUDA')

        @staticmethod
        def getPlatformByName(name):
            return name

    def unavailable(*args):
        raise RuntimeError('driver unavailable')

    system = SimpleNamespace(addParticle=lambda *a: None, addForce=lambda *a: None)
    force = SimpleNamespace(addParticle=lambda *a: None)
    monkeypatch.setitem(sys.modules, 'openmm', SimpleNamespace(
        Platform=Platform, System=lambda: system, CustomExternalForce=lambda *a: force,
        VerletIntegrator=lambda *a: object(), Context=unavailable))
    assert module.check_openmm() is False
    output = capsys.readouterr().out
    assert 'CUDA unavailable: driver unavailable' in output
    assert '[fail] No OpenMM platform' in output


def test_optional_features_do_not_fail_core_readiness(monkeypatch):
    module = _probe_module()
    monkeypatch.setattr(module, 'report_version_sync', lambda: None)
    monkeypatch.setattr(module, 'check_openmm', lambda: True)
    monkeypatch.setattr(module, 'check_ambertools', lambda: True)

    def missing(*args):
        raise ImportError('optional dependency absent')

    monkeypatch.setattr(module.importlib, 'import_module', missing)
    monkeypatch.setattr(module.shutil, 'which', lambda *a: None)
    assert module.main() == 0
