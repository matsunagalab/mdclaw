#!/usr/bin/env python
"""Run MDAgentBench harness x OpenRouter model matrix evaluations.

The runner is intentionally lightweight and dependency-free. In ``--mock``
mode it never calls OpenRouter, which makes it suitable for CI and for checking
matrix plumbing. Without ``--mock`` the built-in ``generic-openrouter`` adapter
uses OpenRouter's OpenAI-compatible Chat Completions endpoint.

The generic adapter is intended for plan-only / answer-style tasks. Execution
tasks that need real trajectories require a harness-specific adapter.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mdclaw.benchmark import cli


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET_DIR = REPO_ROOT / "benchmarks" / "mdagentbench"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return slug.replace("/", "-").replace(":", "-")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def load_matrix_config(config_file: str | Path) -> dict[str, Any]:
    """Load and minimally validate a matrix config."""
    config_path = Path(config_file)
    payload = _read_json(config_path)
    for key in ("run_prefix", "tasks", "harnesses", "models"):
        if key not in payload:
            raise ValueError(f"matrix config missing required key: {key}")
    if not payload["tasks"]:
        raise ValueError("matrix config must include at least one task")
    if not payload["harnesses"]:
        raise ValueError("matrix config must include at least one harness")
    if not payload["models"]:
        raise ValueError("matrix config must include at least one model")
    return payload


def iter_matrix(config: dict[str, Any]):
    for harness in config["harnesses"]:
        for model in config["models"]:
            yield harness, model


def make_run_id(run_prefix: str, harness_name: str, model_name: str) -> str:
    return f"{_safe_slug(run_prefix)}__{_safe_slug(harness_name)}__{_safe_slug(model_name)}"


def _task_dir(dataset_dir: Path, task_id: str) -> Path:
    return dataset_dir / "tasks" / task_id


def _public_input_summary(task_dir: Path, max_chars: int = 4000) -> list[dict[str, str]]:
    """Read small public text inputs, never truth/scorer files."""
    summaries: list[dict[str, str]] = []
    input_dir = task_dir / "input"
    if not input_dir.is_dir():
        return summaries
    for path in sorted(input_dir.iterdir()):
        if not path.is_file():
            continue
        entry = {"path": f"input/{path.name}", "kind": path.suffix.lower(), "content": ""}
        if path.suffix.lower() in {".json", ".md", ".txt"}:
            text = path.read_text(errors="replace")
            entry["content"] = text[:max_chars]
        else:
            entry["content"] = f"<binary-or-structure-file; size={path.stat().st_size} bytes>"
        summaries.append(entry)
    return summaries


def build_generic_prompt(task_dir: Path) -> list[dict[str, str]]:
    task = _read_json(task_dir / "task.json")
    public_inputs = _public_input_summary(task_dir)
    instructions = {
        "task_id": task.get("task_id"),
        "task_intent": task.get("task_intent"),
        "primary_score": task.get("primary_score"),
        "secondary_scores": task.get("secondary_scores", []),
        "public_inputs": public_inputs,
        "required_response": {
            "schema_version": "1.0",
            "task_id": task.get("task_id"),
            "summary": "short evidence summary",
            "effect": {
                "direction": "one of the task-appropriate allowed direction strings",
                "confidence": "low|medium|high"
            },
            "limitations": ["explicit limitations"],
            "figure_captions": []
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "You are being evaluated by MDAgentBench. Read only the public "
                "task information provided here. Do not assume access to hidden "
                "truth files. Return only a JSON object for evidence_report.json."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(instructions, indent=2, sort_keys=True),
        },
    ]


def _mock_evidence(task_id: str, run_id: str) -> dict[str, Any]:
    directions = {
        "T06_answer_stability_t4l_l99a": "destabilizing",
        "T07_answer_ppi_hotspot_barnase_d39a": "weakened_binding",
    }
    direction = directions.get(task_id)
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "summary": (
            "Mock OpenRouter matrix response for plumbing tests. "
            "Do not use mock mode for leaderboard evidence."
        ),
        "effect": {"direction": direction, "confidence": "high" if direction else "low"},
        "limitations": ["Mock mode; no model call and no new MD were run."],
        "figure_captions": [],
    }


def _extract_json_object(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("model response did not contain a JSON object")
    return json.loads(match.group(0))


def call_openrouter(
    model_name: str,
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    max_tokens: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required unless --mock is used")
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if provider:
        payload["provider"] = provider
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/matsunagalab/mdclaw",
            "X-OpenRouter-Title": "MDAgentBench",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {body}") from exc
    content = response_payload["choices"][0]["message"]["content"]
    return _extract_json_object(content), response_payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def _update_manifest_status(submission_dir: Path, status: str, error: dict[str, str] | None = None) -> None:
    manifest_path = submission_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    manifest["status"] = status
    if error:
        manifest.setdefault("errors", []).append(error)
    _write_json(manifest_path, manifest)


def _update_provenance(
    submission_dir: Path,
    harness: dict[str, Any],
    model: dict[str, Any],
    response_payload: dict[str, Any] | None = None,
) -> None:
    provenance_path = submission_dir / "provenance.json"
    provenance = _read_json(provenance_path)
    model_name = model["name"]
    provider = model.get("provider") or {}
    provenance["harness"] = {
        "name": harness["name"],
        "adapter": harness.get("adapter", ""),
    }
    provenance["backend"] = {"name": harness.get("backend_name", "unknown")}
    provenance["model"] = {
        "provider": "openrouter",
        "name": model_name,
    }
    provenance["router"] = {
        "name": "openrouter",
        "model": model_name,
        "provider": provider,
    }
    if response_payload:
        provenance["router"]["response_id"] = response_payload.get("id")
        provenance["router"]["usage"] = response_payload.get("usage")
    _write_json(provenance_path, provenance)


def _score_task(task_file: Path, submission_dir: Path, run_id: str) -> dict[str, Any]:
    validation = cli.validate_benchmark_submission(str(task_file), str(submission_dir))
    score = cli.score_benchmark_submission(
        task_file=str(task_file),
        submission_dir=str(submission_dir),
        run_id=run_id,
        output_file=str(submission_dir.parent / "score.json"),
    )
    return {"validation": validation, "score": score}


def _run_task(
    *,
    dataset_dir: Path,
    run_id: str,
    run_dir: Path,
    task_id: str,
    harness: dict[str, Any],
    model: dict[str, Any],
    budget: dict[str, Any],
    mock: bool,
) -> dict[str, Any]:
    task_dir = _task_dir(dataset_dir, task_id)
    task_file = task_dir / "task.json"
    submission_dir = run_dir / "tasks" / task_id / "submission"
    template = cli.create_benchmark_submission_template(
        task_id=task_id,
        run_id=run_id,
        output_dir=str(submission_dir),
        dataset_dir=str(dataset_dir),
        agent_name=harness["name"],
        backend_name=harness.get("backend_name", "unknown"),
        harness_name=harness["name"],
        model_name=model["name"],
        overwrite=True,
    )
    if not template["success"]:
        return {"task_id": task_id, "success": False, "template": template}

    response_payload = None
    adapter = harness.get("adapter", "")
    try:
        if adapter != "generic-openrouter":
            raise NotImplementedError(
                f"adapter {adapter!r} is a template placeholder; use generic-openrouter "
                "or implement a harness-specific adapter"
            )
        if mock:
            evidence = _mock_evidence(task_id, run_id)
        else:
            max_tokens = int(budget.get("max_tokens_per_task", 4000) or 4000)
            messages = build_generic_prompt(task_dir)
            evidence, response_payload = call_openrouter(
                model["name"], model.get("provider") or {}, messages, max_tokens,
            )
            evidence.setdefault("schema_version", "1.0")
            evidence.setdefault("run_id", run_id)
            evidence.setdefault("task_id", task_id)
        _write_json(submission_dir / "evidence_report.json", evidence)
        status = "completed" if evidence.get("effect", {}).get("direction") else "partial"
        _update_manifest_status(submission_dir, status)
        _update_provenance(submission_dir, harness, model, response_payload)
    except Exception as exc:
        evidence = {
            "schema_version": "1.0",
            "run_id": run_id,
            "task_id": task_id,
            "summary": f"Harness/model execution failed: {type(exc).__name__}: {exc}",
            "limitations": ["No valid agent evidence was produced."],
            "effect": {"direction": None, "confidence": None},
            "figure_captions": [],
        }
        _write_json(submission_dir / "evidence_report.json", evidence)
        _update_manifest_status(
            submission_dir,
            "blocked",
            {"stage": "harness", "code": type(exc).__name__, "message": str(exc)},
        )
        _update_provenance(submission_dir, harness, model, response_payload)

    scored = _score_task(task_file, submission_dir, run_id)
    return {
        "task_id": task_id,
        "success": scored["score"].get("success", False),
        **scored,
    }


def run_matrix(
    config_file: str | Path,
    output_dir: str | Path,
    dataset_dir: str | Path = DEFAULT_DATASET_DIR,
    mock: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    config = load_matrix_config(config_file)
    dataset = Path(dataset_dir)
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    budget = config.get("budget") or {}
    runs: list[dict[str, Any]] = []

    for harness, model in iter_matrix(config):
        run_id = make_run_id(config["run_prefix"], harness["name"], model["name"])
        run_dir = out_root / run_id
        if run_dir.exists():
            if overwrite:
                shutil.rmtree(run_dir)
            else:
                raise FileExistsError(f"run_dir already exists: {run_dir} (pass --overwrite)")
        init = cli.init_benchmark_run(
            output_dir=str(out_root),
            run_id=run_id,
            execution_mode="plan_only",
            judge_mode="deterministic",
            backend_name=harness.get("backend_name", "unknown"),
            backend_version=harness.get("backend_version", ""),
            harness_name=harness["name"],
            harness_adapter=harness.get("adapter", ""),
            model_name=model["name"],
            model_provider="openrouter",
            max_walltime_minutes_per_task=int(budget.get("max_walltime_minutes_per_task", 30) or 30),
            max_gpu_hours=float(budget.get("max_gpu_hours", 0.0) or 0.0),
            max_tokens_per_task=int(budget.get("max_tokens_per_task", 0) or 0),
            max_simulation_ns=float(budget.get("max_simulation_ns", 0.0) or 0.0),
            task_ids=list(config["tasks"]),
        )
        task_results = [
            _run_task(
                dataset_dir=dataset,
                run_id=run_id,
                run_dir=run_dir,
                task_id=task_id,
                harness=harness,
                model=model,
                budget=budget,
                mock=mock,
            )
            for task_id in config["tasks"]
        ]
        summary = cli.summarize_benchmark_run(str(run_dir))
        runs.append({
            "run_id": run_id,
            "run_dir": str(run_dir),
            "init": init,
            "tasks": task_results,
            "summary": summary,
        })

    result = {
        "success": all(run["summary"].get("success") for run in runs),
        "mock": mock,
        "runs": runs,
    }
    _write_json(out_root / f"{_safe_slug(config['run_prefix'])}_matrix_result.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to matrix JSON config")
    parser.add_argument("--output-dir", default="benchmark_runs", help="Benchmark runs root")
    parser.add_argument("--dataset-dir", default=str(DEFAULT_DATASET_DIR), help="MDAgentBench dataset dir")
    parser.add_argument("--mock", action="store_true", help="Do not call OpenRouter; use canned responses")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing generated run directories")
    args = parser.parse_args(argv)

    try:
        result = run_matrix(
            config_file=args.config,
            output_dir=args.output_dir,
            dataset_dir=args.dataset_dir,
            mock=args.mock,
            overwrite=args.overwrite,
        )
    except Exception as exc:
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "success": result["success"],
        "mock": result["mock"],
        "runs": [run["run_id"] for run in result["runs"]],
    }, indent=2, sort_keys=True))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
