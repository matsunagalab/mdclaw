"""OpenRouter harness-matrix runner tests.

These tests use mock mode only. They verify benchmark plumbing without making
network calls or requiring an OpenRouter API key.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdagentbench"
RUNNER = REPO_ROOT / "examples" / "benchmark" / "run_openrouter_matrix.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_openrouter_matrix", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_matrix_config_requires_core_fields(tmp_path: Path):
    runner = _load_runner()
    config = tmp_path / "bad.json"
    config.write_text(json.dumps({"run_prefix": "x", "tasks": []}))

    with pytest.raises(ValueError, match="missing required key"):
        runner.load_matrix_config(config)


def test_openrouter_matrix_mock_run_writes_scores_and_router_provenance(tmp_path: Path):
    runner = _load_runner()
    config = {
        "run_prefix": "pytest_openrouter_matrix",
        "tasks": [
            "T06_answer_stability_t4l_l99a",
            "T07_answer_ppi_hotspot_barnase_d39a",
        ],
        "harnesses": [
            {
                "name": "generic-openrouter",
                "adapter": "generic-openrouter",
                "backend_name": "literature-answer-workflow",
            }
        ],
        "models": [
            {"name": "anthropic/claude-sonnet-4-5", "provider": {"allow_fallbacks": False}},
            {"name": "openai/gpt-5.5", "provider": {"allow_fallbacks": False}},
        ],
        "budget": {"max_tokens_per_task": 1000, "max_walltime_minutes_per_task": 5},
    }
    config_path = tmp_path / "matrix.json"
    config_path.write_text(json.dumps(config))

    result = runner.run_matrix(
        config_file=config_path,
        output_dir=tmp_path / "benchmark_runs",
        dataset_dir=DATASET_DIR,
        mock=True,
    )

    assert result["success"], result
    assert len(result["runs"]) == 2
    for run in result["runs"]:
        summary = run["summary"]["summary"]
        assert summary["overall_score"] == 1.0
        assert summary["model"]["provider"] == "openrouter"
        assert summary["harness"]["name"] == "generic-openrouter"

        run_dir = Path(run["run_dir"])
        provenance_path = (
            run_dir
            / "tasks"
            / "T06_answer_stability_t4l_l99a"
            / "submission"
            / "provenance.json"
        )
        provenance = json.loads(provenance_path.read_text())
        assert provenance["router"]["name"] == "openrouter"
        assert provenance["router"]["model"] in {
            "anthropic/claude-sonnet-4-5",
            "openai/gpt-5.5",
        }
        assert provenance["router"]["provider"] == {"allow_fallbacks": False}


def test_openrouter_matrix_mock_marks_unimplemented_adapters_blocked(tmp_path: Path):
    runner = _load_runner()
    config = {
        "run_prefix": "pytest_openrouter_blocked",
        "tasks": ["T06_answer_stability_t4l_l99a"],
        "harnesses": [
            {
                "name": "pydantic-ai",
                "adapter": "examples/benchmark/adapters/pydantic_ai_openrouter.py",
                "backend_name": "literature-answer-workflow",
            }
        ],
        "models": [
            {"name": "anthropic/claude-sonnet-4-5", "provider": {"allow_fallbacks": False}},
        ],
    }
    config_path = tmp_path / "matrix.json"
    config_path.write_text(json.dumps(config))

    result = runner.run_matrix(
        config_file=config_path,
        output_dir=tmp_path / "benchmark_runs",
        dataset_dir=DATASET_DIR,
        mock=True,
    )

    run = result["runs"][0]
    submission_dir = (
        Path(run["run_dir"])
        / "tasks"
        / "T06_answer_stability_t4l_l99a"
        / "submission"
    )
    manifest = json.loads((submission_dir / "manifest.json").read_text())
    assert manifest["status"] == "blocked"
    assert run["summary"]["summary"]["overall_score"] == 0.0
