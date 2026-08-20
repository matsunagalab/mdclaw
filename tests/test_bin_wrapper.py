"""Guards for the `bin/mdclaw` container wrapper and the agent guides.

A user namespace silently strips the setuid bit from Singularity's starter and
from fusermount3, so the SIF cannot be mounted and every invocation re-extracts
it instead (measured: 0.4 s -> 66 s with a 5.1 GB SIF). Agents reach for
`unshare -Ur` when they hit the NIS/LDAP `unknown userid` warning, so the
wrapper says so out loud.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "bin" / "mdclaw"
WARNING_MARKER = "user namespace"


def _run_wrapper(
    tmp_path: Path,
    *,
    user_namespace: bool,
) -> subprocess.CompletedProcess:
    """Run against a system executable with a deterministic simulated uid_map."""
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "singularity"
    echo = shutil.which("echo")
    if echo is None:
        pytest.skip("echo is unavailable on this host")
    # A symlink to a system executable still works when tmp_path is on a
    # noexec mount; a generated executable stub would not.
    stub.symlink_to(echo)

    sif = tmp_path / "mdclaw.sif"
    sif.write_text("stub sif")
    uid_map = tmp_path / "uid_map"
    uid_map.write_text(
        "         0     100000      65536\n"
        if user_namespace
        else "         0          0 4294967295\n"
    )

    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    env["MDCLAW_RUNTIME"] = "singularity"
    env["MDCLAW_SIF"] = str(sif)
    env["MDCLAW_UID_MAP_FILE"] = str(uid_map)

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable on this host")
    # Invoke through Bash so a noexec checkout or lost archive mode bits do
    # not prevent the behavior under test from running.
    command = [bash, str(WRAPPER), "solvate_structure"]
    return subprocess.run(
        command, env=env, cwd=tmp_path, capture_output=True, text=True, check=False
    )


def test_wrapper_is_quiet_outside_a_user_namespace(tmp_path: Path):
    result = _run_wrapper(tmp_path, user_namespace=False)

    assert result.returncode == 0, result.stderr
    assert WARNING_MARKER not in result.stderr
    assert str(tmp_path / "mdclaw.sif") in result.stdout


def test_wrapper_warns_inside_a_user_namespace(tmp_path: Path):
    result = _run_wrapper(tmp_path, user_namespace=True)

    assert WARNING_MARKER in result.stderr
    assert "unshare" in result.stderr
    # The warning must not contaminate stdout: agents parse `--list-json`.
    assert str(tmp_path / "mdclaw.sif") in result.stdout
    assert WARNING_MARKER not in result.stdout


def test_agent_guides_stay_identical():
    """CLAUDE.md and AGENTS.md are mirrors; the guide itself requires it."""
    claude = (REPO_ROOT / "CLAUDE.md").read_text()
    agents = (REPO_ROOT / "AGENTS.md").read_text()

    assert claude == agents, "CLAUDE.md and AGENTS.md have drifted apart"


def test_agent_guides_warn_against_user_namespaces():
    """The `unknown userid` fix must not read as an invitation to unshare."""
    guide = (REPO_ROOT / "CLAUDE.md").read_text()
    userid_section = guide.split("unknown userid", 1)
    assert len(userid_section) == 2, "the unknown-userid guidance disappeared"

    following = userid_section[1]
    assert "unshare" in following
    assert "--no-home" in following
