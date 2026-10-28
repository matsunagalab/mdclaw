"""Paths a Slurm job reads or binds, checked on the submission host.

A compute node sees the cluster's shared filesystems, not this host's
``/tmp`` or a filesystem held in its memory. MDDataBench v5 lost 11 first
jobs to such paths: an ``--output-dir`` under ``/tmp``, a container bind of a
directory that existed only on the login side, and job directories with a
typo (``/data1/rku00161`` for ``/data1/rkp00079/rku00161``) that existed only
because the agent's sandbox root was a tmpfs it could write to. The
container then failed to start (``FATAL: container creation failed: mount
hook function failure``) seconds into the job. Each is refused here.

``MDCLAW_ALLOW_LOCAL_SUBMIT_PATHS=1`` lifts the host-local check for a
single-machine Slurm, where the "compute node" is this host.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Iterable, Optional

ALLOW_LOCAL_ENV = "MDCLAW_ALLOW_LOCAL_SUBMIT_PATHS"
LOCAL_PREFIXES = ("/tmp", "/var/tmp", "/dev/shm", "/run/user", "/private/tmp", "/private/var/tmp")
_MEMORY_FILESYSTEMS = {"tmpfs", "ramfs", "devtmpfs"}
_PATH_FLAG = re.compile(r"^--[\w-]*(?:file|dir)$")
_NOT_PATH_FLAGS = {"--profile"}
_UNEXPANDED = re.compile(r"[$`*?]")


def _unescape_mount(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def mount_table(path: str = "/proc/self/mounts") -> list[tuple[str, str]]:
    """``(mount point, fstype)`` of this process's mount namespace."""
    try:
        with open(path) as handle:
            text = handle.read()
    except OSError:
        return []
    table = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            table.append((_unescape_mount(parts[1]), parts[2]))
    return table


def _mount_of(path: str, table: list[tuple[str, str]]) -> Optional[tuple[str, str]]:
    best = None
    for point, fstype in table:
        root = point.rstrip("/") or "/"
        if root == "/" or path == root or path.startswith(root + "/"):
            if best is None or len(root) >= len(best[0]):
                best = (root, fstype)
    return best


def nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def local_paths_allowed() -> bool:
    return os.environ.get(ALLOW_LOCAL_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def unshared_reason(path, *, table: Optional[list[tuple[str, str]]] = None) -> Optional[str]:
    """Why a compute node cannot see ``path`` (None when it can, as far as
    this host can tell): under a host-local temporary directory, or on a
    filesystem held in this host's memory."""
    if local_paths_allowed():
        return None
    resolved = Path(os.path.realpath(os.path.expanduser(str(path))))
    text = str(resolved)
    for prefix in LOCAL_PREFIXES:
        if text == prefix or text.startswith(prefix + "/"):
            return f"{text} is under {prefix}, a directory local to this host"
    existing = str(nearest_existing(resolved))
    mount = _mount_of(existing, mount_table() if table is None else table)
    if mount and mount[1] in _MEMORY_FILESYSTEMS:
        where = "" if existing == text else f" (its nearest existing directory {existing} is)"
        return (f"{text} is on a {mount[1]} filesystem mounted at {mount[0]}{where}, which lives in "
                "this host's memory")
    return None


def command_path_arguments(command: str) -> list[tuple[str, str]]:
    """``(flag, value)`` of every ``--*-dir`` / ``--*-file`` argument in a
    command or script, quotes removed (``--job-dir '/data1/x'`` is the path
    ``/data1/x``, not ``'/data1/x'``)."""
    found: list[tuple[str, str]] = []
    for raw in (command or "").replace("\\\n", " ").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            tokens = [t.strip("'\"") for t in line.split()]
        for i, token in enumerate(tokens):
            head, eq, value = token.partition("=")
            if eq and _PATH_FLAG.match(head) and head not in _NOT_PATH_FLAGS:
                found.append((head, value))
            elif (_PATH_FLAG.match(token) and token not in _NOT_PATH_FLAGS and i + 1 < len(tokens)
                  and not tokens[i + 1].startswith("-")):
                found.append((token, tokens[i + 1]))
    return found


def literal_path(value: str) -> Optional[Path]:
    """The absolute path a value names, or None when the shell decides it."""
    if not value or _UNEXPANDED.search(value):
        return None
    return Path(os.path.abspath(os.path.expanduser(value)))


def bind_path_of(flag: str, value: str) -> Optional[str]:
    """What a container must bind for one path argument: the directory, or
    a file's parent directory."""
    path = literal_path(value)
    if path is None:
        return None
    path = Path(os.path.realpath(path))
    return str(path.parent if flag.endswith("file") else path)


def bind_source(entry: str) -> str:
    """The host side of a ``src[:dst[:opts]]`` bind entry."""
    return str(entry).split(":", 1)[0]


def check_paths(items: Iterable[tuple[str, str, bool]]) -> Optional[dict]:
    """The ``submission_path_not_shared`` refusal for the first path a compute
    node cannot see. ``items``: ``(role, path, must_exist)``."""
    from mdclaw._common import create_validation_error

    table = None if local_paths_allowed() else mount_table()
    for role, raw, must_exist in items:
        if raw is None:
            continue
        path = Path(os.path.abspath(os.path.expanduser(str(raw))))
        reason = None
        if must_exist and not path.exists():
            reason = f"{path} does not exist on this host"
        else:
            reason = unshared_reason(path, table=table)
        if reason is None:
            continue
        fix = _path_fix(role, path)
        error = create_validation_error(
            role, f"the job's {role} {path} is not visible from a compute node: {reason}",
            expected="a path on the cluster's shared filesystem",
            actual=str(path),
            hints=[f"{ALLOW_LOCAL_ENV}=1 lifts this check on a single-machine Slurm, where jobs run on "
                   "this host."],
            code="submission_path_not_shared",
            context_extra={"path": str(path), "role": role, "reason": reason},
        )
        error["next_action"] = fix
        return error
    return None


def _path_fix(role: str, path: Path) -> str:
    if role == "output_dir":
        return "Resubmit with --output-dir set to a directory on shared storage (the job directory works)"
    if role == "working directory":
        return ("cd to the study directory (on shared storage) and run submit_job from there; "
                ".mdclaw_cluster.json is read from the working directory")
    if role == "job_dir":
        return ("The job directory itself must live on shared storage: check the path for a typo; if the "
                "DAG really lives on this host's local or memory filesystem, rebuild it under the shared "
                "study directory")
    return (f"Change {role} in --script to a path on shared storage (or drop it: in node mode the "
            "tool writes into the node's own directory)")
