"""Focused tests for shared utility behavior."""

import pytest

from mdclaw import _common as common_mod
from mdclaw._common import atomic_write_text_group
from mdclaw import _lock as lock_mod


def test_atomic_write_text_group_commits_all_files(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"

    atomic_write_text_group([(a, "alpha"), (b, "beta")])

    assert a.read_text() == "alpha"
    assert b.read_text() == "beta"
    assert not list(tmp_path.glob(".*.tmp.*"))


def test_atomic_write_text_group_cleans_tmp_before_replace_on_failure(tmp_path):
    a = tmp_path / "a.txt"
    missing = tmp_path / "missing" / "b.txt"

    with pytest.raises(OSError):
        atomic_write_text_group([(a, "alpha"), (missing, "beta")])

    assert not a.exists()
    assert not list(tmp_path.glob(".*.tmp.*"))


def test_atomic_write_text_group_rolls_back_after_partial_replace_failure(
    monkeypatch, tmp_path
):
    a = tmp_path / "system.xml"
    b = tmp_path / "state.xml"
    a.write_text("old-system", encoding="utf-8")
    b.write_text("old-state", encoding="utf-8")
    original_replace = common_mod.os.replace

    def flaky_replace(src, dst):
        src_path = common_mod.Path(src)
        dst_path = common_mod.Path(dst)
        if (
            dst_path == b
            and src_path.name.startswith(".state.xml.tmp.")
        ):
            raise OSError("replace failed midway")
        return original_replace(src, dst)

    monkeypatch.setattr(common_mod.os, "replace", flaky_replace)

    with pytest.raises(OSError, match="replace failed midway"):
        atomic_write_text_group([
            (a, "new-system"),
            (b, "new-state"),
        ])

    assert a.read_text(encoding="utf-8") == "old-system"
    assert b.read_text(encoding="utf-8") == "old-state"
    assert not list(tmp_path.glob(".*.tmp.*"))
    assert not list(tmp_path.glob(".*.backup.*"))


def test_file_lock_does_not_unlock_failed_lock(monkeypatch, tmp_path):
    calls = []
    original_flock = lock_mod.fcntl.flock

    def fake_flock(fd, op):
        calls.append(op)
        if op == lock_mod.fcntl.LOCK_EX:
            raise OSError("lock unavailable")
        return original_flock(fd, op)

    monkeypatch.setattr(lock_mod.fcntl, "flock", fake_flock)

    with pytest.raises(OSError, match="lock unavailable"):
        with lock_mod.file_lock(tmp_path / "node.lock"):
            pass

    assert calls == [lock_mod.fcntl.LOCK_EX]


def test_importing_mdclaw_does_not_configure_the_root_logger():
    """Library etiquette: importing mdclaw must not attach a handler to the
    root logger, or the host application's own records start appearing on
    stderr. The CLI installs the real handler in _cli._configure_logging.
    """
    import subprocess
    import sys

    script = (
        "import logging;"
        "from mdclaw._cli import _discover_tools;"
        "_discover_tools();"
        "print(len(logging.getLogger().handlers));"
        "h = logging.getLogger('mdclaw').handlers;"
        "print(len(h));"
        "print(','.join(type(x).__name__ for x in h))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    root_handlers, mdclaw_handlers, handler_types = proc.stdout.split()
    assert root_handlers == "0", "importing mdclaw added a root logger handler"
    # Exactly one NullHandler, attached once at package import rather than on
    # every setup_logger call. The types come from the child process: asserting
    # on this process's logger would pass no matter what the child did.
    assert mdclaw_handlers == "1"
    assert handler_types == "NullHandler"
