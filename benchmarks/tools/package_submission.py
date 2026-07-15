#!/usr/bin/env python3
"""Copy a completed raw OpenMM bundle into an MDPrepBench v0.3 submission."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path


_CORE_DESTINATIONS = {
    "topology/system.xml",
    "topology/topology.pdb",
    "topology/state.xml",
    "prepared_structure.pdb",
    "manifest.json",
    "metrics.json",
    "provenance.json",
    "minimized_structure.pdb",
    "minimization_report.json",
    "evidence_report.json",
    "command_log.json",
    "harness_execution.json",
}


def _parse_extra_outputs(values: list[str]) -> tuple[list[tuple[Path, Path]], list[str]]:
    outputs: list[tuple[Path, Path]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for value in values:
        if "=" not in value:
            errors.append(
                f"--extra-output must be relative_path=source_path, got {value!r}"
            )
            continue
        destination_text, source_text = value.split("=", 1)
        destination = Path(destination_text.strip())
        source = Path(source_text.strip())
        destination_posix = destination.as_posix()
        if (
            destination_posix in {"", "."}
            or destination.is_absolute()
            or ".." in destination.parts
        ):
            errors.append(f"--extra-output destination must be a safe relative path: {value!r}")
            continue
        if destination_posix in _CORE_DESTINATIONS or destination_posix in seen:
            errors.append(f"duplicate or reserved --extra-output destination: {destination_posix}")
            continue
        if not source.is_file():
            errors.append(f"--extra-output source not found: {source}")
            continue
        seen.add(destination_posix)
        outputs.append((destination, source))
    return outputs, errors


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission-dir", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--system-xml", required=True)
    parser.add_argument("--topology-pdb", required=True)
    parser.add_argument("--state-xml", required=True)
    parser.add_argument("--prepared-structure", required=True)
    parser.add_argument(
        "--extra-output",
        action="append",
        default=[],
        metavar="RELATIVE_PATH=SOURCE_PATH",
    )
    args = parser.parse_args()

    inputs = {
        "topology/system.xml": Path(args.system_xml),
        "topology/topology.pdb": Path(args.topology_pdb),
        "topology/state.xml": Path(args.state_xml),
    }
    errors = [f"input not found: {path}" for path in inputs.values() if not path.is_file()]
    prepared = Path(args.prepared_structure)
    if not prepared.is_file():
        errors.append(f"prepared structure not found: {prepared}")
    extra_outputs, extra_errors = _parse_extra_outputs(args.extra_output)
    errors.extend(extra_errors)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    submission = Path(args.submission_dir).resolve()
    if submission.exists() and not submission.is_dir():
        print(f"submission directory is an existing file: {submission}", file=sys.stderr)
        return 1
    submission.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{submission.name}.", dir=str(submission.parent))
    )
    try:
        for relative, source in inputs.items():
            _copy(source, staging / relative)
        _copy(prepared, staging / "prepared_structure.pdb")
        for relative, source in extra_outputs:
            _copy(source, staging / relative)
        if submission.exists():
            shutil.rmtree(submission)
        staging.rename(submission)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    print(f"wrote raw MDPrepBench v0.3 submission for {args.task_id} to {submission}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
