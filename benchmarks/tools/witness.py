#!/usr/bin/env python3
"""Manage MDPrepBench positive fixtures ("witnesses").

A witness is one artifact bundle the current scorer accepts for a task, kept so
that a later change can be caught: a bundle that scored 1.0 and no longer does
means the scoring side changed, not the agent.

What it does NOT establish, and must not be described as establishing:

- that the preparation is scientifically correct beyond what the scorer checks;
- that the scorer is strict enough — deleting a check leaves a witness at 1.0,
  so the negative fixtures remain the other half of the test;
- that the current container can still *produce* the bundle. Witnesses are
  static; they exercise the scoring side only.

Bundles are large (~1.8 GB for 40 tasks) and live outside git, under
`$MDPREPBENCH_WITNESS_DIR`, laid out as:

    <task_id>/submission/...          the raw artifacts, exactly as scored
    <task_id>/harness_execution.json  the run's execution record

Usage:

    MDPREPBENCH_WITNESS_DIR=/path python benchmarks/tools/witness.py record \\
        --produced-by "codex gpt-5.6-sol via run_benchmark_agent"
    MDPREPBENCH_WITNESS_DIR=/path python benchmarks/tools/witness.py verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

MANIFEST = REPO_ROOT / "benchmarks" / "mdprepbench" / "witnesses" / "manifest.json"
DATASET = REPO_ROOT / "benchmarks" / "mdprepbench" / "dataset.json"
TASKS_DIR = REPO_ROOT / "benchmarks" / "mdprepbench" / "tasks"

# Everything else in a bundle directory is derived by scoring and must never be
# hashed, or the next verification reports drift the artifacts did not cause.
IMMUTABLE = ("submission", "harness_execution.json")
DERIVED = ("normalized_submission", "score.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contract_sha256(task_id: str) -> str:
    """Hash everything the scorer reads for a task, not only task.json.

    Five tasks keep a private reference structure under `truth/`, and swapping
    one silently invalidates a witness recorded against the old contract.
    """
    digest = hashlib.sha256()
    task_dir = TASKS_DIR / task_id
    for path in sorted(task_dir.rglob("*")):
        if path.is_file() and path.name != "prompt.md":
            digest.update(str(path.relative_to(task_dir)).encode())
            digest.update(_sha256(path).encode())
    return digest.hexdigest()


def _input_files(bundle: Path) -> dict[str, dict]:
    """Hash the bundle's immutable inputs, keyed by path relative to the bundle."""
    out: dict[str, dict] = {}
    for name in IMMUTABLE:
        root = bundle / name
        paths = sorted(root.rglob("*")) if root.is_dir() else [root]
        for path in paths:
            if path.is_file():
                rel = str(path.relative_to(bundle))
                out[rel] = _sha256(path)
    return out


def _drop_derived(bundle: Path) -> None:
    for name in DERIVED:
        path = bundle / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            path.unlink()


def _scorer_revision() -> str:
    """Read HEAD from .git directly; the container has no git binary."""
    head = REPO_ROOT / ".git" / "HEAD"
    if not head.is_file():
        return "unknown"
    text = head.read_text().strip()
    if not text.startswith("ref:"):
        return text[:7] or "unknown"
    ref = text.split(" ", 1)[1].strip()
    direct = REPO_ROOT / ".git" / ref
    if direct.is_file():
        return direct.read_text().strip()[:7]
    packed = REPO_ROOT / ".git" / "packed-refs"
    if packed.is_file():
        for line in packed.read_text().splitlines():
            if line.endswith(f" {ref}"):
                return line.split(" ", 1)[0][:7]
    return "unknown"


def _load_manifest() -> dict:
    if MANIFEST.is_file():
        return json.loads(MANIFEST.read_text())
    return {
        "schema_version": "1.0",
        "description": (
            "Positive fixtures for MDPrepBench: bundles the scorer accepts, kept "
            "to detect the scoring side regressing. Not canonical solutions — "
            "never score a submission by similarity to one."
        ),
        "witnesses": {},
    }


def _save_manifest(manifest: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _bundle_root() -> Path:
    root = os.environ.get("MDPREPBENCH_WITNESS_DIR")
    if not root:
        raise SystemExit("set MDPREPBENCH_WITNESS_DIR to the bundle directory")
    path = Path(root)
    if not path.is_dir():
        raise SystemExit(f"MDPREPBENCH_WITNESS_DIR is not a directory: {path}")
    return path


def _score(task_id: str, bundle: Path, run_id: str) -> dict:
    """Score a bundle, then remove the files scoring left inside it."""
    from mdclaw.benchmark.cli import score_benchmark_submission

    try:
        result = score_benchmark_submission(
            task_file=str(TASKS_DIR / task_id / "task.json"),
            submission_dir=str(bundle / "submission"),
            run_id=run_id,
            harness_record_file=str(bundle / "harness_execution.json"),
        )
    finally:
        _drop_derived(bundle)
    return result.get("score") or result


def _accepted(score: dict) -> tuple[bool, str]:
    """A witness must pass on every axis the scorer reports, not just one."""
    prep = (score.get("scores") or {}).get("preparation")
    status = score.get("status")
    total = score.get("weighted_total")
    warnings = score.get("integrity_warnings") or []
    problems = []
    if status != "passed":
        problems.append(f"status={status}")
    if total != 1.0:
        problems.append(f"weighted_total={total}")
    if prep != 1.0:
        problems.append(f"preparation={prep}")
    if warnings:
        problems.append(f"integrity_warnings={warnings[:2]}")
    return not problems, ", ".join(problems)


def _select(bundle_root: Path, requested: list[str] | None) -> list[Path]:
    available = {
        p.name: p for p in sorted(bundle_root.iterdir())
        if (p / "submission").is_dir()
    }
    if requested:
        unknown = [t for t in requested if t not in available]
        if unknown:
            raise SystemExit(f"no bundle for: {', '.join(unknown)}")
        return [available[t] for t in requested]
    if not available:
        raise SystemExit(f"no bundles under {bundle_root}")
    return list(available.values())


def cmd_record(args: argparse.Namespace) -> int:
    manifest = _load_manifest()
    bundle_root = _bundle_root()
    revision = _scorer_revision()
    rejected = []
    for bundle in _select(bundle_root, args.task):
        task_id = bundle.name
        record_file = bundle / "harness_execution.json"
        if not record_file.is_file():
            print(f"  SKIP {task_id}: no harness_execution.json")
            rejected.append(task_id)
            continue
        run_id = json.loads(record_file.read_text()).get("run_id", "")
        ok, why = _accepted(_score(task_id, bundle, run_id))
        if not ok:
            print(f"  SKIP {task_id}: {why}")
            rejected.append(task_id)
            continue
        entry = {
            "run_id": run_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "produced_by": args.produced_by,
            "repository_head": revision,
            "contract_sha256": _contract_sha256(task_id),
            "files": _input_files(bundle),
        }
        manifest["witnesses"][task_id] = entry
        print(f"  OK   {task_id}")
    _save_manifest(manifest)
    # A witness set that silently covers a subset is worse than none: the
    # always-on test would pass while most tasks had no regression guard.
    suite = set(json.loads(DATASET.read_text())["task_ids"])
    uncovered = sorted(suite - set(manifest["witnesses"]))
    print(f"\n{len(manifest['witnesses'])} witnesses recorded, "
          f"{len(rejected)} rejected")
    if uncovered:
        print(f"{len(uncovered)} suite task(s) still without a witness: "
              f"{uncovered[:5]}")
    return 1 if rejected or uncovered else 0


def cmd_verify(args: argparse.Namespace) -> int:
    manifest = _load_manifest()
    recorded = manifest.get("witnesses") or {}
    if not recorded:
        print("manifest holds no witnesses")
        return 1
    bundle_root = _bundle_root()
    requested = args.task or sorted(recorded)
    unknown = [t for t in requested if t not in recorded]
    if unknown:
        print(f"not in manifest: {', '.join(unknown)}")
        return 1

    failures = []
    for task_id in requested:
        entry = recorded[task_id]
        bundle = bundle_root / task_id
        if not (bundle / "submission").is_dir():
            print(f"  MISS  {task_id}: no bundle at {bundle}")
            failures.append(task_id)
            continue
        current = _input_files(bundle)
        expected = entry["files"]
        changed = sorted(
            set(expected) ^ set(current)
            | {k for k in set(expected) & set(current) if expected[k] != current[k]}
        )
        if changed:
            print(f"  DRIFT {task_id}: {len(changed)} file(s): {changed[:3]}")
            failures.append(task_id)
            continue
        if _contract_sha256(task_id) != entry.get("contract_sha256"):
            print(f"  TASK  {task_id}: task contract changed since recording")
            failures.append(task_id)
            continue
        ok, why = _accepted(_score(task_id, bundle, entry["run_id"]))
        if ok:
            print(f"  OK    {task_id}")
        else:
            print(f"  FAIL  {task_id}: {why}")
            failures.append(task_id)
    print(f"\n{len(requested) - len(failures)}/{len(requested)} accepted, "
          f"{len(failures)} failure(s)")
    return 1 if failures else 0


def main() -> int:
    # Scoring one bundle takes tens of seconds; unbuffered output lets a caller
    # watch progress instead of waiting for the whole run to flush at exit.
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("record", help="score bundles and register the accepted ones")
    p.add_argument("--produced-by", required=True,
                   help="e.g. 'codex gpt-5.6-sol via run_benchmark_agent'")
    p.add_argument("--task", nargs="+", default=None)
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("verify", help="re-score recorded bundles; nonzero on any failure")
    p.add_argument("--task", nargs="+", default=None)
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
