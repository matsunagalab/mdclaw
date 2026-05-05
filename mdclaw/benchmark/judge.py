"""LLM-judge plumbing for MDAgentBench v1.0.

The framework defines the *interface* for qualitative scoring, but does not
itself call an LLM. An external evaluator is expected to:

1. Read ``<task_dir>/scorer/llm_judge_prompt.json`` for the rubric prompt.
2. Combine it with the agent's submission.
3. Call an LLM and capture the structured JSON response.
4. Pass the response file via ``--llm-judge-file`` to
   ``score_benchmark_submission``.

A future v1.x release will ship ``mdclaw run_llm_judge`` to automate steps 2-3.
For now this module just validates and normalizes the supplied judge file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def load_judge_payload(path: Optional[str | Path]) -> Optional[dict[str, Any]]:
    """Load and normalize an LLM-judge response file.

    Returns ``None`` when ``path`` is falsy. Raises ``ValueError`` on a
    malformed file so callers fail fast rather than silently scoring zero.
    """
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"LLM judge file not found: {p}")
    try:
        payload = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM judge file {p} is not valid JSON: {exc}")
    if not isinstance(payload, dict):
        raise ValueError(f"LLM judge file {p} must contain a JSON object")
    payload.setdefault("scores", {})
    payload.setdefault("violations", [])
    payload.setdefault("rubric_version", "1.0")
    return payload


def make_judge_prompt(task: dict[str, Any]) -> dict[str, Any]:
    """Build the deterministic ``scorer/llm_judge_prompt.json`` for a task.

    Output schema is fixed across tasks; only ``rubrics`` and a few task
    identifiers vary. External evaluators should call this once per task to
    materialize the prompt file.
    """
    return {
        "schema_version": "1.0",
        "task_id": task.get("task_id"),
        "judge_role": "MDAgentBench v1.0 qualitative judge",
        "instructions": (
            "Read the agent's submission and produce a JSON object whose "
            "`scores` keys are the rubric names listed in `rubrics` and whose "
            "values are floats in [0,1]. Add `violations` entries for any "
            "explicit overclaim, fabricated data, or missing-limitation pattern. "
            "Set `enabled` to true and include the model name and temperature "
            "you used."
        ),
        "rubrics": list(task.get("scoring", {}).get("llm_judge_rubrics") or []),
        "output_schema": {
            "enabled": "bool",
            "judge_model": "string",
            "temperature": "float",
            "rubric_version": "1.0",
            "scores": "dict[str, float in [0,1]]",
            "violations": "list[dict]",
        },
    }
