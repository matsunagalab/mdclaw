"""Test the official ARM archive layout without executing ARM code on the host."""

import hashlib
import importlib.util
from pathlib import Path
import tarfile

import pytest


SPEC = importlib.util.spec_from_file_location(
    "modeller_arm_install", Path(__file__).resolve().parents[1] / "scripts/install-modeller-arm64.py"
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


@pytest.fixture
def distribution(tmp_path, monkeypatch):
    source = tmp_path / "modeller-10.8"
    for name in ("modlib/modeller/__init__.py", "src/main.py", "bin/lib/data.lib",
                 "bin/modpy.sh.top", "lib/armv8-gnu/python3.3/_modeller.so",
                 "lib/armv8-gnu/libmodeller.so.14"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    archive = tmp_path / "modeller.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source, arcname=source.name)
    monkeypatch.setattr(installer, "SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    prefix = tmp_path / "prefix"
    return archive, prefix, prefix / "lib/python3.12/site-packages"


def test_install_preserves_layout_and_is_idempotent(distribution):
    archive, prefix, site = distribution
    installer.install_archive(archive, prefix, site)
    assert (site / "_modeller.so").read_text() == "fixture"
    config = {}
    config_path = site / "modeller/config.py"
    exec(config_path.read_text(), config)
    assert config["license"] == "XXXX"
    assert (Path(config["install_dir"]) / "bin/lib/data.lib").is_file()
    config_path.write_text("user-maintained config")
    installer.install_archive(archive, prefix, site)
    assert config_path.read_text() == "user-maintained config"


def test_install_refuses_wrong_checksum(distribution, monkeypatch):
    archive, prefix, site = distribution
    monkeypatch.setattr(installer, "SHA256", "invalid")
    with pytest.raises(ValueError, match="checksum"):
        installer.install_archive(archive, prefix, site)
    assert not prefix.exists()


def test_install_refuses_existing_modeller(distribution):
    archive, prefix, site = distribution
    (site / "modeller").mkdir(parents=True)
    (site / "modeller/config.py").write_text("keep existing config")
    with pytest.raises(FileExistsError, match="overwrite"):
        installer.install_archive(archive, prefix, site)
    assert (site / "modeller/config.py").read_text() == "keep existing config"
