"""Exercise conda-pack relocation with hardlinks and executable scripts.

Run with the builder's Python. Optional arguments ``tar.gz /path/to/gtar``
reproduce the old archive route for diagnosis; the default tests the directory
route used by the Dockerfile.
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def check_relocation(pack_format="no-archive", tar="tar"):
    with tempfile.TemporaryDirectory(prefix="mdclaw-relocation-") as directory:
        root = Path(directory)
        source = root / "source"
        target = root / "target"
        (source / "conda-meta").mkdir(parents=True)
        (source / "conda-meta/history").write_text("", encoding="utf-8")
        (source / "bin").mkdir()
        program = source / "bin/first"
        original = f'#!/bin/sh\nprintf "%s\\n" "{source}/data"\n'
        program.write_text(original, encoding="utf-8")
        program.chmod(0o755)
        os.link(program, source / "bin/second")
        (source / "bin/alias").symlink_to("first")
        (source / "data").write_bytes(b"\x00\xffpayload")
        output = target if pack_format == "no-archive" else root / "environment.tar.gz"
        subprocess.run(
            [sys.executable, "-m", "conda_pack.cli", "-p", str(source), "-o", str(output),
             "--format", pack_format, "--dest-prefix", str(target),
             "--ignore-editable-packages", "--ignore-missing-files"],
            check=True,
        )
        if pack_format != "no-archive":
            target.mkdir()
            subprocess.run([tar, "-xzf", str(output), "-C", str(target)], check=True)
        # Both names must work after relocation, with executable bits preserved.
        # Prefix replacement must not modify either source hardlink.
        for name in ("first", "second", "alias"):
            result = subprocess.check_output([str(target / "bin" / name)], text=True)
            assert result.strip() == str(target / "data"), result
            assert (source / "bin" / name).read_text(encoding="utf-8") == original
        assert (target / "bin/alias").is_symlink()
        assert (target / "data").read_bytes() == b"\x00\xffpayload"
        assert (target / "bin/activate").is_file()
    print("PASS: relocated hardlinks, symlink, executable mode, data, and source integrity")


if __name__ == "__main__":
    check_relocation(*sys.argv[1:])
