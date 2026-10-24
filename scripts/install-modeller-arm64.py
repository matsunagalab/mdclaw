"""Install official MODELLER ARM64 binaries into the active Pixi/Conda prefix.

Usage: pixi run python scripts/install-modeller-arm64.py
No license key is stored: MDClaw supplies KEY_MODELLER* at execution time.
"""

import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import urllib.request


VERSION = "10.8"
URL = f"https://salilab.org/modeller/{VERSION}/modeller-{VERSION}.tar.gz"
SHA256 = "dde99dcad8e2945d48f9d87ea21addfad90f61ade14bbd7a7ad38b72530c5927"


def install_archive(archive, prefix, site_packages):
    """Verify and lay out the same ARM distribution used by the RIKYU image."""
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise ValueError("MODELLER archive checksum mismatch")
    destination = prefix / "lib" / f"modeller-{VERSION}"
    links = {
        site_packages / "modeller": destination / "modlib/modeller",
        site_packages / "_modeller.so": destination / "lib/armv8-gnu/python3.3/_modeller.so",
    }
    marker = destination / ".mdclaw-source-sha256"
    if marker.is_file() and marker.read_text().strip() == SHA256:
        if all(link.is_symlink() and link.resolve() == target.resolve() and link.exists()
               for link, target in links.items()):
            return
        raise RuntimeError("MODELLER installation is incomplete; inspect its paths before reinstalling")
    for path in (destination, *links):
        if os.path.lexists(path):
            raise FileExistsError(f"Will not overwrite existing MODELLER files: {path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".modeller-install-") as temporary:
        work = Path(temporary)
        with tarfile.open(archive) as tar:
            tar.extractall(work, filter="data")
        source = work / f"modeller-{VERSION}"
        staged = work / "installed"
        staged.mkdir()
        for name in ("modlib", "src"):
            shutil.copytree(source / name, staged / name, symlinks=True)
        (staged / "bin").mkdir()
        shutil.copytree(source / "bin/lib", staged / "bin/lib", symlinks=True)
        for path in (source / "bin").glob("*.top"):
            shutil.copy2(path, staged / "bin" / path.name)
        shutil.copytree(source / "lib/armv8-gnu", staged / "lib/armv8-gnu", symlinks=True)
        (staged / "modlib/modeller/config.py").write_text(
            f"install_dir = {str(destination) + '/'!r}\nlicense = 'XXXX'\n"
        )
        (staged / marker.name).write_text(SHA256 + "\n")
        staged.rename(destination)
    site_packages.mkdir(parents=True, exist_ok=True)
    for link, target in links.items():
        link.symlink_to(os.path.relpath(target, link.parent))


def main():
    if sys.platform != "linux" or platform.machine() not in {"aarch64", "arm64"}:
        raise RuntimeError("This installer is for Linux ARM64; other platforms use the salilab package")
    prefix = Path(sys.prefix)
    if not (prefix / "conda-meta").is_dir():
        raise RuntimeError("Run with the Pixi environment's Python: pixi run python scripts/install-modeller-arm64.py")
    with tempfile.TemporaryDirectory(prefix="mdclaw-modeller-") as temporary:
        archive = Path(temporary) / "modeller.tar.gz"
        urllib.request.urlretrieve(URL, archive)
        install_archive(archive, prefix, Path(sysconfig.get_path("purelib")))
    # A child process tests the real loader search path supplied by Pixi's ARM
    # activation. Importing _modeller loads the binary without using a license.
    subprocess.run([sys.executable, "-c", "import _modeller; print(_modeller.__file__)"], check=True)
    print(f"MODELLER {VERSION} ARM64 installed; supply KEY_MODELLER* when running MDClaw")


if __name__ == "__main__":
    main()
