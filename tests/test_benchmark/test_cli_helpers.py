"""Unit tests for the small `mdclaw.benchmark.cli` runner helpers.

These cover regression cases for the runner-side hardening:

* ``_coerce_capture`` / ``_run_agent_command`` timeout path tolerates
  ``bytes`` stdout/stderr from ``subprocess.TimeoutExpired``.
* ``_copy_public_task_files`` stages ``task_dir/input/`` recursively into
  ``run_task_dir/input/`` and reports the destination in the return dict.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from mdclaw.benchmark import cli


class TestCoerceCapture:
    def test_none_returns_empty_string(self):
        assert cli._coerce_capture(None) == ""

    def test_str_pass_through(self):
        assert cli._coerce_capture("hello") == "hello"

    def test_bytes_decoded_as_utf8(self):
        assert cli._coerce_capture(b"hello world") == "hello world"

    def test_bytes_with_invalid_utf8_replaced(self):
        # 0xFF is not valid UTF-8; should not raise.
        out = cli._coerce_capture(b"abc\xffdef")
        assert "abc" in out and "def" in out


class TestRunAgentCommandTimeout:
    def test_timeout_writes_bytes_capture_without_crash(self, tmp_path: Path):
        """``TimeoutExpired.stdout/stderr`` may be ``bytes`` even when
        ``text=True``. The runner must coerce to str so ``write_text`` works
        and the timed_out record still lands on disk for the blocked
        submission writer downstream."""
        stdout_file = tmp_path / "agent_stdout.log"
        stderr_file = tmp_path / "agent_stderr.log"

        fake_exc = subprocess.TimeoutExpired(
            cmd="sleep 999",
            timeout=1,
            output=b"partial stdout bytes",
            stderr=b"partial stderr bytes",
        )

        with patch.object(cli.subprocess, "run", side_effect=fake_exc):
            record = cli._run_agent_command(
                command="sleep 999",
                cwd=tmp_path,
                timeout_seconds=1,
                stdout_file=stdout_file,
                stderr_file=stderr_file,
            )

        assert record["timed_out"] is True
        assert record["returncode"] is None
        assert stdout_file.read_text() == "partial stdout bytes"
        assert stderr_file.read_text() == "partial stderr bytes"

    def test_timeout_with_none_capture_writes_empty(self, tmp_path: Path):
        stdout_file = tmp_path / "out.log"
        stderr_file = tmp_path / "err.log"

        fake_exc = subprocess.TimeoutExpired(
            cmd="x", timeout=1, output=None, stderr=None
        )

        with patch.object(cli.subprocess, "run", side_effect=fake_exc):
            record = cli._run_agent_command(
                command="x",
                cwd=tmp_path,
                timeout_seconds=1,
                stdout_file=stdout_file,
                stderr_file=stderr_file,
            )

        assert record["timed_out"] is True
        assert stdout_file.read_text() == ""
        assert stderr_file.read_text() == ""

    def test_success_path_records_returncode(self, tmp_path: Path):
        """Regression guard: the timeout fix must not break the happy path."""
        stdout_file = tmp_path / "out.log"
        stderr_file = tmp_path / "err.log"

        record = cli._run_agent_command(
            command=f"{sys.executable} -c \"print('ok')\"",
            cwd=tmp_path,
            timeout_seconds=30,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
        )

        assert record["timed_out"] is False
        assert record["returncode"] == 0
        assert "ok" in stdout_file.read_text()


class TestCopyPublicTaskFiles:
    def test_copies_prompt_and_task_json(self, tmp_path: Path):
        task_dir = tmp_path / "tasks" / "T_demo"
        task_dir.mkdir(parents=True)
        (task_dir / "prompt.md").write_text("hello\n")
        (task_dir / "task.json").write_text("{}\n")

        run_task_dir = tmp_path / "run" / "T_demo"
        copied = cli._copy_public_task_files(task_dir, run_task_dir)

        assert (run_task_dir / "prompt.md").read_text() == "hello\n"
        assert (run_task_dir / "task.json").read_text() == "{}\n"
        assert copied["prompt.md"] == str(run_task_dir / "prompt.md")
        assert copied["task.json"] == str(run_task_dir / "task.json")
        # No input/ in source -> no input/ key in result.
        assert "input" not in copied
        assert not (run_task_dir / "input").exists()

    def test_copies_input_directory_recursively(self, tmp_path: Path):
        task_dir = tmp_path / "tasks" / "T_demo"
        (task_dir / "input" / "nested").mkdir(parents=True)
        (task_dir / "prompt.md").write_text("p\n")
        (task_dir / "task.json").write_text("{}\n")
        (task_dir / "input" / "data.csv").write_text("col\n1\n")
        (task_dir / "input" / "nested" / "ref.pdb").write_text("ATOM\n")

        run_task_dir = tmp_path / "run" / "T_demo"
        copied = cli._copy_public_task_files(task_dir, run_task_dir)

        # input/ files preserved exactly under run_task_dir/input/.
        assert (run_task_dir / "input" / "data.csv").read_text() == "col\n1\n"
        assert (run_task_dir / "input" / "nested" / "ref.pdb").read_text() == "ATOM\n"
        assert copied["input"] == str(run_task_dir / "input")

    def test_input_copy_merges_into_existing_run_dir(self, tmp_path: Path):
        """Re-running the same task must not crash when run_task_dir/input/
        already exists from a prior run."""
        task_dir = tmp_path / "tasks" / "T_demo"
        (task_dir / "input").mkdir(parents=True)
        (task_dir / "input" / "fresh.txt").write_text("new\n")

        run_task_dir = tmp_path / "run" / "T_demo"
        (run_task_dir / "input").mkdir(parents=True)
        (run_task_dir / "input" / "leftover.txt").write_text("stale\n")

        copied = cli._copy_public_task_files(task_dir, run_task_dir)

        # Existing leftover file is preserved (merge), new file lands on top.
        assert (run_task_dir / "input" / "fresh.txt").read_text() == "new\n"
        assert (run_task_dir / "input" / "leftover.txt").read_text() == "stale\n"
        assert copied["input"] == str(run_task_dir / "input")

    def test_input_file_not_dir_is_ignored(self, tmp_path: Path):
        """If task_dir/input happens to be a *file*, don't try to copytree
        and don't claim we copied a directory."""
        task_dir = tmp_path / "tasks" / "T_demo"
        task_dir.mkdir(parents=True)
        (task_dir / "input").write_text("oops, a file\n")

        run_task_dir = tmp_path / "run" / "T_demo"
        copied = cli._copy_public_task_files(task_dir, run_task_dir)

        assert "input" not in copied
        assert not (run_task_dir / "input").exists()
