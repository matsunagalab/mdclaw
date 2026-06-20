#!/usr/bin/env python3
"""Compatibility wrapper for the canonical MDPrepBench task generator."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

runpy.run_path(
    str(ROOT / "benchmarks" / "mdprepbench" / "scripts" / "generate_tasks.py"),
    run_name="__main__",
)
