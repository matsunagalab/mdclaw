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


def test_container_definition_includes_ruff_for_sif_lint_workflows():
    dockerfile = (REPO_ROOT / "container" / "Dockerfile").read_text()
    test_script = (REPO_ROOT / "container" / "scripts" / "test-container.sh").read_text()

    assert '"ruff>=0.1.0"' in dockerfile
    assert "python -m ruff --version" in test_script


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


def _copy_install_fixture(tmp_path: Path) -> Path:
    shutil.copytree(REPO_ROOT / "skills", tmp_path / "skills", symlinks=True)
    (tmp_path / "scripts").mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / "install-agent-skills.sh", tmp_path / "scripts")
    return tmp_path


def test_install_agent_skills_copy_includes_support_dirs_and_codex(tmp_path):
    root = _copy_install_fixture(tmp_path)

    subprocess.run(
        ["bash", "scripts/install-agent-skills.sh", "--copy"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )

    for mirror in (".agents/skills", ".claude/skills", ".codex/skills"):
        assert (root / mirror / "common" / "run-loop.md").exists()
        assert (root / mirror / "md-prepare" / "SKILL.md").exists()


def test_install_agent_skills_symlink_mode_prunes_stale_entries(tmp_path):
    root = _copy_install_fixture(tmp_path)
    stale = root / ".agents" / "skills" / "md-benchmark"
    stale.parent.mkdir(parents=True)
    stale.symlink_to("../../skills/md-benchmark")

    subprocess.run(
        ["bash", "scripts/install-agent-skills.sh"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )

    assert not stale.exists()
    assert not stale.is_symlink()
    assert os.readlink(root / ".agents" / "skills" / "common") == "../../skills/common"
    assert os.readlink(root / ".codex" / "skills" / "md-prepare") == "../../skills/md-prepare"
