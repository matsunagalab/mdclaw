#!/usr/bin/env python3
"""Negative MDPrepBench baseline that produces no raw artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission-dir", required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()

    submission = Path(args.submission_dir)
    submission.mkdir(parents=True, exist_ok=True)
    print(f"created empty submission for {args.task_id}: {submission}")
    print("expected evaluator outcome: rejected because required raw files are missing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
