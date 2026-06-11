"""Dataset discovery helpers for the MD benchmark suites."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

DEFAULT_BENCHMARK_VERSION = "MDPrepBench-v0.1"
DEFAULT_DATASET_DIR = "benchmarks/mdprepbench"
BUILTIN_DATASET_DIRS = (
    "benchmarks/mdprepbench",
    "benchmarks/mdstudybench",
)


def repository_root() -> Path:
    """Return the repository root for built-in benchmark dataset lookup."""
    return Path(__file__).resolve().parents[2]


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def dataset_dir_candidates(dataset_dir: str | Path) -> list[Path]:
    """Return likely filesystem locations for a dataset path."""
    requested = Path(dataset_dir)
    candidates = [requested]
    if not requested.is_absolute():
        candidates.append(repository_root() / requested)
    return _unique_paths(candidates)


def resolve_dataset_dir(dataset_dir: str | Path = DEFAULT_DATASET_DIR) -> Path:
    """Resolve ``dataset_dir`` when it points to a known dataset checkout."""
    for candidate in dataset_dir_candidates(dataset_dir):
        if (candidate / "dataset.json").is_file():
            return candidate
    return Path(dataset_dir)


def load_dataset_metadata(
    dataset_dir: str | Path = DEFAULT_DATASET_DIR,
) -> dict[str, Any]:
    """Load ``dataset.json`` if it is present and valid."""
    dataset = resolve_dataset_dir(dataset_dir)
    dataset_file = dataset / "dataset.json"
    if not dataset_file.is_file():
        return {}
    try:
        payload = json.loads(dataset_file.read_text())
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def benchmark_version_for_dataset(
    dataset_dir: str | Path = DEFAULT_DATASET_DIR,
) -> str:
    """Return the dataset's benchmark version, falling back to the default."""
    payload = load_dataset_metadata(dataset_dir)
    version = payload.get("benchmark_version")
    return str(version) if version else DEFAULT_BENCHMARK_VERSION


def list_task_ids(dataset_dir: str | Path = DEFAULT_DATASET_DIR) -> list[str]:
    """Discover task IDs from dataset metadata."""
    for dataset in dataset_dir_candidates(dataset_dir):
        dataset_file = dataset / "dataset.json"
        if not dataset_file.is_file():
            continue
        try:
            payload = json.loads(dataset_file.read_text())
        except json.JSONDecodeError:
            continue
        ids = payload.get("task_ids")
        if isinstance(ids, list):
            return [str(task_id) for task_id in ids]
    return []


def builtin_task_contract_candidates(
    task_id: str,
    configured_dataset_dir: Optional[str | Path] = None,
) -> list[Path]:
    """Return task.json candidates for run summary fallback lookup."""
    candidates: list[Path] = []
    if configured_dataset_dir:
        dataset = resolve_dataset_dir(configured_dataset_dir)
        candidates.append(dataset / "tasks" / task_id / "task.json")

    root = repository_root()
    for dataset_dir in BUILTIN_DATASET_DIRS:
        dataset_path = Path(dataset_dir)
        candidates.append(dataset_path / "tasks" / task_id / "task.json")
        candidates.append(root / dataset_path / "tasks" / task_id / "task.json")
    return _unique_paths(candidates)
