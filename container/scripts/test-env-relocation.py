"""Exercise conda-pack relocation with hardlinks and executable scripts.

Run with the builder's Python and optionally the Dockerfile path. The test runs
the actual relocation block, so fixes to it cannot diverge from the test.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def check_relocation(dockerfile=Path(__file__).resolve().parents[1] / "Dockerfile"):
    block = Path(dockerfile).read_text(encoding="utf-8").split("<<'PY_RELOCATE'\n", 1)[1]
    block = block.split("\nPY_RELOCATE", 1)[0]
    with tempfile.TemporaryDirectory(prefix="mdclaw-relocation-") as directory:
        root = Path(directory)
        source = root / "source-environment"
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
        binary = b"\x00" + str(source).encode() + b"/lib\x00\xff"
        (source / "binary-prefix").write_bytes(binary)
        # Real environments can have overlapping ownership (e.g. LICENSE).
        for name in ("package-a", "package-b"):
            metadata = {"name": name, "version": "1", "url": "",
                        "link": None,
                        "files": ["data", "bin/alias", "bin/second", "binary-prefix"],
                        "paths_data": {"paths": [{"_path": "binary-prefix",
                            "file_mode": "binary", "prefix_placeholder": "/old-build"}]}}
            (source / "conda-meta" / f"{name}.json").write_text(
                json.dumps(metadata), encoding="utf-8")
        subprocess.run(
            [sys.executable, "-", str(source), str(target)],
            input=block, text=True, check=True,
        )
        # Both names must work after relocation, with executable bits preserved.
        # Prefix replacement must not modify either source hardlink.
        for name in ("first", "second", "alias"):
            result = subprocess.check_output([str(target / "bin" / name)], text=True)
            assert result.strip() == str(target / "data"), result
            assert (source / "bin" / name).read_text(encoding="utf-8") == original
        assert (target / "bin/alias").is_symlink()
        assert (target / "data").read_bytes() == b"\x00\xffpayload"
        assert (target / "bin/activate").is_file()
        relocated = (target / "binary-prefix").read_bytes()
        assert str(source).encode() not in relocated, relocated
        assert str(target).encode() + b"/lib" in relocated, relocated
        assert len(relocated) == len(binary)
        assert (source / "binary-prefix").read_bytes() == binary
    print("PASS: relocated hardlinks, duplicate paths, symlink, modes, data, and source integrity")


if __name__ == "__main__":
    check_relocation(*sys.argv[1:])
