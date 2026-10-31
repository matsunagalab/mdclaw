"""Deployment script and package-metadata smoke tests."""

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml


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
        ".codex-plugin/plugin.json": json.loads(
            (REPO_ROOT / ".codex-plugin" / "plugin.json").read_text()
        )["version"],
        "package.json": json.loads((REPO_ROOT / "package.json").read_text())["version"],
    }
    marketplace = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text())
    versions[".claude-plugin/marketplace.json metadata.version"] = marketplace["metadata"]["version"]
    versions[".claude-plugin/marketplace.json plugins[0].version"] = marketplace["plugins"][0]["version"]

    assert len(set(versions.values())) == 1, versions


def test_container_definition_includes_dev_tools_for_sif_workflows():
    """The image env comes from the lock; the dev feature must carry the tools."""
    dockerfile = (REPO_ROOT / "container" / "Dockerfile.pixi").read_text()
    pixi_manifest = (REPO_ROOT / "pixi.toml").read_text()
    test_script = (REPO_ROOT / "container" / "scripts" / "test-container.sh").read_text()

    assert "pixi install -e ${PIXI_ENV} --locked" in dockerfile and "ARG PIXI_ENV=cuda118" in dockerfile
    assert "dev" in tomllib.loads(pixi_manifest)["environments"]["cuda118"]["features"]
    dev_feature = pixi_manifest.split("[feature.dev.dependencies]", 1)[1]
    for tool in ("pytest", "pytest-asyncio", "ruff"):
        assert tool in dev_feature.split("[", 1)[0]
    # Conda's Ruff package ships an executable, not a Python module.
    assert 'check "ruff" ruff --version' in test_script
    assert "python -m pytest --version" in test_script
    assert "import pytest_asyncio" in test_script


def test_container_build_is_gated_on_the_lockfile():
    """The image installs exactly pixi.lock (never re-solves), with the recipes
    its source packages need, and replaces the editable checkout install."""
    dockerfile = (REPO_ROOT / "container" / "Dockerfile.pixi").read_text()

    assert "COPY pixi.toml pixi.lock" in dockerfile
    assert "COPY recipes/ recipes/" in dockerfile
    assert "pixi install -e ${PIXI_ENV} --locked" in dockerfile
    assert "pip install --no-cache-dir --no-deps --force-reinstall ." in dockerfile
    # Only the environment ships: .pixi/bld added 8 GB to the v0.6.13 image.
    assert "find .pixi -mindepth 1 -maxdepth 1 ! -name envs -exec rm -rf {} +" in dockerfile
    assert (REPO_ROOT / "pixi.lock").exists()


def _host_free_env() -> dict[str, str]:
    """The test process's environment without the host's MDClaw selection."""
    return {key: val for key, val in os.environ.items()
            if not key.startswith(("MDCLAW_", "CLAUDE_PLUGIN_"))}


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
        env={**_host_free_env(), "PATH": f"{stub_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Using pixi" in result.stderr
    assert "unexpected runtime call" not in result.stderr


def test_plugin_setup_uses_the_pixi_checkout_on_path(tmp_path):
    """A plugin cache has no .pixi; a checkout on PATH must not trigger an image download."""
    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "setup-container.sh", plugin / "scripts")
    shutil.copytree(REPO_ROOT / "scripts/_internal", plugin / "scripts/_internal")
    checkout = tmp_path / "checkout"
    (checkout / "bin").mkdir(parents=True)
    (checkout / ".pixi" / "envs" / "cuda118").mkdir(parents=True)
    wrapper = checkout / "bin" / "mdclaw"
    wrapper.write_text("#!/bin/sh\n")
    wrapper.chmod(0o755)
    stub_bin = tmp_path / "stubs"
    stub_bin.mkdir()
    runtime = stub_bin / "singularity"
    runtime.write_text('#!/bin/sh\necho "unexpected runtime call" >&2\nexit 99\n')
    runtime.chmod(0o755)
    result = subprocess.run(
        ["bash", str(plugin / "scripts" / "setup-container.sh")],
        env={**_host_free_env(), "PATH": f"{checkout / 'bin'}:{stub_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Using the Pixi checkout on PATH" in result.stderr
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
        env={**_host_free_env(), "PATH": f"{tmp_path}:/usr/bin:/bin",
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


def test_sst2_pin_is_the_same_in_every_declaration():
    """The SST2 revision is declared four times; a drift once shipped a driver too old for run_sst2."""
    sources = {
        "pixi.toml": (REPO_ROOT / "pixi.toml").read_text(),
        "environment.yml": (REPO_ROOT / "environment.yml").read_text(),
        "container/Dockerfile.rikyu-arm64": (REPO_ROOT / "container" / "Dockerfile.rikyu-arm64").read_text(),
        "container/Dockerfile.pixi": (REPO_ROOT / "container" / "Dockerfile.pixi").read_text(),
    }
    pins = {
        "pixi.toml rev": re.findall(r'sst2 = \{ git = "https://github.com/matsunagalab/SST2.git", rev = "([0-9a-f]+)" \}', sources["pixi.toml"]),
        "environment.yml pip": re.findall(r"SST2 @ git\+https://github.com/matsunagalab/SST2.git@([0-9a-f]+)", sources["environment.yml"]),
        "Dockerfile.rikyu-arm64 MDCLAW_SST2_REVISION": re.findall(r"MDCLAW_SST2_REVISION=\"?([0-9a-f]+)", sources["container/Dockerfile.rikyu-arm64"]),
        "Dockerfile.pixi MDCLAW_SST2_REVISION": re.findall(r"MDCLAW_SST2_REVISION=\"?([0-9a-f]+)", sources["container/Dockerfile.pixi"]),
    }
    assert all(len(found) == 1 for found in pins.values()), pins
    assert len({found[0] for found in pins.values()}) == 1, pins


def test_pixi_activation_isolates_user_site_and_finds_host_opencl():
    """User site-packages once shadowed the locked pytest; cuda118 needs the host ICDs."""
    manifest = tomllib.loads((REPO_ROOT / "pixi.toml").read_text())
    assert manifest["activation"]["env"]["PYTHONNOUSERSITE"] == "1"
    cuda118 = manifest["feature"]["cuda118"]
    assert cuda118["activation"]["env"]["OCL_ICD_VENDORS"] == "/etc/OpenCL/vendors"
    assert cuda118["dependencies"]["pytorch"]["build"] == "cuda118*"


def test_environments_build_the_container_only_pieces_from_recipes():
    """PythonTorchForce and PLUMED come from recipes pinned to the container's commits."""
    manifest = tomllib.loads((REPO_ROOT / "pixi.toml").read_text())
    deps = manifest["feature"]["cuda118"]["dependencies"]
    assert deps["openmm-torch"] == {"path": "recipes/openmm-torch-cuda118"}
    assert deps["openmm-plumed"] == {"path": "recipes/openmm-plumed-cuda118"}
    # build-plumed.sh stays for conda prefixes (docs/developer/plumed.md).
    plumed_script = (REPO_ROOT / "container" / "scripts" / "build-plumed.sh").read_text()
    torch_commit = re.search(r"commit: ([0-9a-f]{40})", (REPO_ROOT / "recipes/openmm-torch/recipe/recipe.yaml").read_text()).group(1)
    plumed_commit = re.search(r"plumed_commit=([0-9a-f]{40})", plumed_script).group(1)
    plugin_commit = re.search(r"plugin_commit=([0-9a-f]{40})", plumed_script).group(1)
    recipes = REPO_ROOT / "recipes"
    assert plumed_commit in (recipes / "plumed/recipe/recipe.yaml").read_text()
    for flavour in ("-cuda118", "-cuda13"):
        assert torch_commit in (recipes / f"openmm-torch{flavour}/recipe/recipe.yaml").read_text()
    for flavour in ("", "-cuda118"):
        assert plugin_commit in (recipes / f"openmm-plumed{flavour}/recipe/recipe.yaml").read_text()
    default = manifest["feature"]["cuda129"]["dependencies"]
    assert default["openmm-torch"] == {"path": "recipes/openmm-torch"}
    assert default["openmm-plumed"] == {"path": "recipes/openmm-plumed"}


def test_a_tag_publishes_every_image():
    """amd64 and arm64 share one multi-arch tag; RIKYU's CUDA 13 image has its own."""
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/container.yml").read_text())
    images = {item["image"]: item for item in workflow["jobs"]["build"]["strategy"]["matrix"]["include"]}
    assert set(images) == {"amd64", "arm64", "rikyu"}
    assert images["amd64"]["platform"] == "linux/amd64" and images["amd64"]["runner"] == "ubuntu-latest"
    for name in ("arm64", "rikyu"):
        assert images[name]["platform"] == "linux/arm64" and images[name]["runner"] == "ubuntu-24.04-arm"
    assert "PIXI_ENV=default" in images["arm64"]["build_args"]
    assert images["rikyu"]["dockerfile"] == "container/Dockerfile.rikyu-arm64"
    publish = "\n".join(step.get("run", "") for step in workflow["jobs"]["publish"]["steps"])
    assert "imagetools create" in publish and "-amd64" in publish and "-arm64" in publish


def test_arm64_image_declares_a_digest_for_both_architectures():
    """The runtime base stays one pinned multi-arch digest across rebuilds."""
    dockerfile = (REPO_ROOT / "container" / "Dockerfile.pixi").read_text()
    workflow = (REPO_ROOT / ".github/workflows/container.yml").read_text()
    pinned = re.search(r"nvidia/cuda:12\.9\.1-base-ubuntu24\.04@sha256:[0-9a-f]{64}", workflow).group(0)
    assert pinned in dockerfile


def test_rikyu_image_is_built_from_the_cuda13_environment():
    """RIKYU's contracts survive in pixi.toml and the Dockerfile, without a second dependency list."""
    manifest = tomllib.loads((REPO_ROOT / "pixi.toml").read_text())
    assert manifest["environments"]["cuda13"]["features"] == ["dev", "modeller", "cuda13"]
    deps = manifest["feature"]["cuda13"]["dependencies"]
    assert deps["cuda-version"] == "13.1.*"
    assert deps["libcufft"] == ">=12.1.0.78"
    assert deps["pytorch"]["build"] == "cuda130*"
    assert deps["openmm-torch"] == {"path": "recipes/openmm-torch-cuda13"}
    assert deps["openmm-plumed"] == {"path": "recipes/openmm-plumed"}

    dockerfile = (REPO_ROOT / "container" / "Dockerfile.rikyu-arm64").read_text()
    assert "pixi install -e cuda13 --locked" in dockerfile
    assert "environment.yml" not in dockerfile
    # Podman 4.9 on RIKYU parses heredoc lines as Dockerfile instructions.
    assert not re.search(r"^RUN[^\n]*<<", dockerfile, re.M)
    # NVRTC 13.0 cannot share a solve with cuFFT 12.1.
    assert "COPY --from=cuda-devel /usr/local/cuda/lib64/libnvrtc.so*" in dockerfile
    # /proc/self/maps resolves the /opt/mdclaw symlink.
    assert 'MDCLAW_FUSEFIX_LIB="/opt/mdclaw-src/.pixi/envs/cuda13/lib/' in dockerfile
    assert "scripts/install-modeller-arm64.py" in dockerfile
    ignored = (REPO_ROOT / ".dockerignore").read_text()
    assert "!scripts/install-modeller-arm64.py" in ignored


def test_pixi_image_bakes_in_the_environment_activation():
    """`singularity exec` runs no activation script, so Dockerfile.pixi fixes it as ENV."""
    dockerfile = (REPO_ROOT / "container" / "Dockerfile.pixi").read_text()
    manifest = tomllib.loads((REPO_ROOT / "pixi.toml").read_text())
    assert "pixi install -e ${PIXI_ENV} --locked" in dockerfile
    expected = dict(manifest["activation"]["env"])
    expected.update(manifest["feature"]["cuda118"]["activation"]["env"])
    for name, value in expected.items():
        assert re.search(rf'^\s*{name}="?{re.escape(value)}"?', dockerfile, re.M), name
    for name in ("AMBERHOME", "MKL_INTERFACE_LAYER", "OPENMM_PLUGIN_DIR"):
        assert re.search(rf"^\s*{name}=", dockerfile, re.M), name
