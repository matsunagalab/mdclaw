"""Deployment script and package-metadata smoke tests."""

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_amd64_toolchain_smoke_parses_cpp_library_as_cpp():
    dockerfile = (REPO_ROOT / "container" / "Dockerfile").read_text()
    command = next(line.strip() for line in dockerfile.splitlines()
                   if "swig " in line and "/tmp/swigcheck.i" in line)
    # std_vector.i is a C++ library; C mode fails even with the correct swiglib.
    assert "-c++" in command.split()


def test_release_versions_stay_in_sync():
    versions = {
        "pyproject.toml": tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"],
        "mdclaw/__init__.py": re.search(
            r'__version__\s*=\s*"([^"]+)"',
            (REPO_ROOT / "mdclaw" / "__init__.py").read_text(),
        ).group(1),
        ".claude-plugin/plugin.json": json.loads(
            (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text()
        )["version"],
        "package.json": json.loads((REPO_ROOT / "package.json").read_text())["version"],
    }
    marketplace = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text())
    versions[".claude-plugin/marketplace.json metadata.version"] = marketplace["metadata"]["version"]
    versions[".claude-plugin/marketplace.json plugins[0].version"] = marketplace["plugins"][0]["version"]

    assert len(set(versions.values())) == 1, versions


def test_container_definition_includes_dev_tools_for_sif_workflows():
    """The image env comes from the lock; the dev feature must carry the tools."""
    dockerfile = (REPO_ROOT / "container" / "Dockerfile").read_text()
    pixi_manifest = (REPO_ROOT / "pixi.toml").read_text()
    test_script = (REPO_ROOT / "container" / "scripts" / "test-container.sh").read_text()

    assert "pixi install --locked" in dockerfile
    dev_feature = pixi_manifest.split("[feature.dev.dependencies]", 1)[1]
    for tool in ("pytest", "pytest-asyncio", "ruff"):
        assert tool in dev_feature.split("[", 1)[0]
    assert "python -m ruff --version" in test_script
    assert "python -m pytest --version" in test_script
    assert "import pytest_asyncio" in test_script


def test_container_build_is_gated_on_the_lockfile():
    """Stage 1 must install from pixi.lock, never re-solve, and must not
    reintroduce the editable install into the packed env."""
    dockerfile = (REPO_ROOT / "container" / "Dockerfile").read_text()

    assert "COPY pixi.toml pixi.lock" in dockerfile
    assert "pixi install --locked" in dockerfile
    assert "pip install --no-deps --force-reinstall ." in dockerfile
    assert "--ignore-editable-packages" in dockerfile
    assert (REPO_ROOT / "pixi.lock").exists()


def test_container_setup_skips_when_pixi_env_exists(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / "setup-container.sh", tmp_path / "scripts")
    shutil.copytree(REPO_ROOT / "scripts/_internal", tmp_path / "scripts/_internal")
    (tmp_path / "pixi.toml").write_text("[workspace]\n")
    (tmp_path / ".pixi" / "envs" / "default").mkdir(parents=True)
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    (stub_bin / "pixi").symlink_to(shutil.which("echo"))
    runtime = stub_bin / "singularity"
    runtime.write_text('#!/bin/sh\necho "unexpected runtime call" >&2\nexit 99\n')
    runtime.chmod(0o755)

    result = subprocess.run(
        ["bash", str(tmp_path / "scripts" / "setup-container.sh")],
        env={**os.environ, "PATH": f"{stub_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Using pixi" in result.stderr
    assert "unexpected runtime call" not in result.stderr


def test_container_setup_preserves_explicit_existing_sif(tmp_path):
    shared = tmp_path / "shared images"
    shared.mkdir()
    image = shared / "managed image.sif"
    image.write_bytes(b"operator-managed SIF")
    runtime = tmp_path / "singularity"
    runtime.write_text('#!/bin/sh\necho "unexpected runtime call" >&2\nexit 99\n')
    runtime.chmod(0o755)
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/setup-container.sh"), "different-version"],
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin",
             "MDCLAW_SIF": str(image), "MDCLAW_FORCE_CONTAINER_SETUP": "1"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Using configured MDClaw SIF" in result.stderr
    assert "unexpected runtime call" not in result.stderr
    assert image.read_bytes() == b"operator-managed SIF"
    assert not (shared / ".mdclaw-version").exists()


def test_gen_cli_contract_imports_its_checkout_before_installed_package(tmp_path):
    checkout = tmp_path / "checkout"
    installed = tmp_path / "installed"
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy2(
        REPO_ROOT / "scripts" / "gen_cli_contract.py",
        checkout / "scripts" / "gen_cli_contract.py",
    )

    for root, origin in ((checkout, "checkout"), (installed, "installed")):
        package = root / "mdclaw"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "_cli.py").write_text(
            "def _discover_tools():\n"
            f"    return {origin!r}\n\n"
            "def _tool_list_json(tools):\n"
            "    return {}\n"
        )

    script = checkout / "scripts" / "gen_cli_contract.py"
    probe = (
        "import runpy\n"
        f"namespace = runpy.run_path({str(script)!r}, run_name='contract_probe')\n"
        "print(namespace['_discover_tools']())\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(installed)
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "checkout"


def test_tracked_skill_mirrors_have_common_and_no_broken_symlinks():
    for root_name in (".agents/skills", ".claude/skills"):
        root = REPO_ROOT / root_name
        assert (root / "common" / "run-loop.md").exists()
        for path in root.rglob("*"):
            if path.is_symlink():
                assert path.exists(), f"{path.relative_to(REPO_ROOT)} -> {os.readlink(path)}"


def test_every_skill_is_mirrored_and_declares_frontmatter():
    """The tracked mirrors must cover every skill, and every skill must carry
    the Agent Skills frontmatter the `npx skills` installer reads."""
    skills = sorted(p.name for p in (REPO_ROOT / "skills").iterdir() if p.is_dir())
    assert skills, "skills/ is empty?"
    for name in skills:
        head = (REPO_ROOT / "skills" / name / "SKILL.md").read_text().split("\n", 5)
        assert head[0] == "---", f"skills/{name}/SKILL.md lacks YAML frontmatter"
        for mirror in (".agents/skills", ".claude/skills"):
            assert (REPO_ROOT / mirror / name).exists(), f"{mirror}/{name} missing"
