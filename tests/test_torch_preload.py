import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_mdclaw_preloads_torch_before_openmm_plugin_scan(tmp_path: Path):
    order_file = tmp_path / "order.txt"
    fake_modules = tmp_path / "fake_modules"
    fake_modules.mkdir()
    (fake_modules / "torch.py").write_text(
        textwrap.dedent(
            """
            import os
            from pathlib import Path

            path = Path(os.environ["MDCLAW_TEST_IMPORT_ORDER_FILE"])
            path.write_text(path.read_text() + "torch\\n" if path.exists() else "torch\\n")
            """
        )
    )
    (fake_modules / "openmm.py").write_text(
        textwrap.dedent(
            """
            import os
            from pathlib import Path

            path = Path(os.environ["MDCLAW_TEST_IMPORT_ORDER_FILE"])
            order = path.read_text() if path.exists() else ""
            if "torch\\n" not in order:
                raise RuntimeError("OpenMM plugin scan happened before torch preload")
            path.write_text(order + "openmm\\n")
            """
        )
    )

    env = os.environ.copy()
    env["MDCLAW_TEST_IMPORT_ORDER_FILE"] = str(order_file)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(fake_modules), str(Path.cwd()), env.get("PYTHONPATH", "")]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import mdclaw; import openmm",
        ],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert order_file.read_text().splitlines() == ["torch", "openmm"]


def test_mdclaw_torch_preload_can_be_disabled(tmp_path: Path):
    order_file = tmp_path / "order.txt"
    fake_modules = tmp_path / "fake_modules"
    fake_modules.mkdir()
    (fake_modules / "torch.py").write_text(
        textwrap.dedent(
            """
            import os
            from pathlib import Path

            Path(os.environ["MDCLAW_TEST_IMPORT_ORDER_FILE"]).write_text("torch\\n")
            """
        )
    )

    env = os.environ.copy()
    env["MDCLAW_PRELOAD_TORCH_FOR_OPENMM"] = "0"
    env["MDCLAW_TEST_IMPORT_ORDER_FILE"] = str(order_file)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(fake_modules), str(Path.cwd()), env.get("PYTHONPATH", "")]
    )
    result = subprocess.run(
        [sys.executable, "-c", "import mdclaw"],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not order_file.exists()
