#!/usr/bin/env python3
"""Generate canonical MDPrepBench task.json files from compact specs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mdclaw.benchmark.task_specs import build_task_payload


SUITE_DIR = Path(__file__).resolve().parents[1]


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _task_payloads() -> list[tuple[str, dict]]:
    dataset = _read_json(SUITE_DIR / "dataset.json")
    defaults = _read_json(SUITE_DIR / "task_specs" / "defaults.json")

    payloads: list[tuple[str, dict]] = []
    for task_id in dataset["task_ids"]:
        spec_path = SUITE_DIR / "task_specs" / "tasks" / f"{task_id}.json"
        spec = _read_json(spec_path)
        payloads.append((task_id, build_task_payload(defaults, spec)))
    return payloads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="check that generated payloads match committed task.json files",
    )
    args = parser.parse_args()

    mismatches: list[str] = []
    for task_id, payload in _task_payloads():
        task_path = SUITE_DIR / "tasks" / task_id / "task.json"
        if args.check:
            current = _read_json(task_path)
            if current != payload:
                mismatches.append(task_id)
            continue
        task_path.write_text(json.dumps(payload, indent=2) + "\n")

    if mismatches:
        for task_id in mismatches:
            print(f"generated task differs from committed task.json: {task_id}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
