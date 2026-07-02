"""LLM-judge plumbing for MD benchmark submissions.

``run_llm_judge`` automates the qualitative scoring step: it builds the rubric
prompt, embeds the agent's submission, calls an LLM (Claude sonnet by default via
the ``claude`` CLI so the judge stays on the host like the agent runner), and
writes the structured judge file that ``score_benchmark_submission
--llm-judge-file`` consumes. The scorer itself stays offline/deterministic; the
LLM call lives here in a separate, host-run step. ``load_judge_payload`` /
``make_judge_prompt`` remain for consuming a pre-supplied file.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional


DEFAULT_JUDGE_MODEL = "sonnet"


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
        "judge_role": "MD benchmark qualitative judge",
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


# ---------------------------------------------------------------------------
# Automated judge (host-run step; separate from the offline scorer)


def _call_claude_judge(prompt: str, model: str, timeout: int = 180) -> str:
    """Call the ``claude`` CLI headlessly and return the model's text.

    Kept as a small, monkeypatchable seam so tests can stub the LLM. Uses plain
    ``-p`` (no tool use, no approval-bypass flags): the judge only reads the
    prompt and emits JSON.
    """
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError(
            "claude CLI not found on PATH; run_llm_judge needs it (or stub "
            "_call_claude_judge). The judge runs on the host, not inside the SIF."
        )
    proc = subprocess.run(
        [exe, "-p", prompt, "--model", model, "--output-format", "json"],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude judge exited {proc.returncode}: {proc.stderr[:300]}")
    # --output-format json wraps the answer: {"type":"result","result":"<text>",...}
    try:
        envelope = json.loads(proc.stdout)
        return str(envelope.get("result") or proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout


def _extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first balanced top-level JSON object from ``text``."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in judge response")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON object in judge response")


def _submission_material(submission_dir: Path) -> str:
    """Collect the reasoning artifacts the judge scores (no truth/scorer)."""
    parts: list[str] = []
    for name in ("evidence_report.json", "methods.md", "metrics.json"):
        p = submission_dir / name
        if p.is_file():
            body = p.read_text()[:6000]
            parts.append(f"----- {name} -----\n{body}")
    return "\n\n".join(parts) or "(no evidence_report.json / methods.md / metrics.json found)"


def run_llm_judge(
    task_file: str,
    submission_dir: str,
    output_file: str,
    judge_model: str = DEFAULT_JUDGE_MODEL,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Run the LLM judge for one submission and write the judge file.

    The written file is exactly what ``score_benchmark_submission
    --llm-judge-file`` expects: ``scores`` keyed by rubric name in [0,1] plus
    ``violations``. Rubrics come from the task's ``scoring.llm_judge_rubrics``.
    """
    task = json.loads(Path(task_file).read_text())
    prompt_spec = make_judge_prompt(task)
    rubrics = prompt_spec["rubrics"]
    if not rubrics:
        return {"success": False, "errors": ["task declares no llm_judge_rubrics"]}
    material = _submission_material(Path(submission_dir))

    prompt = (
        "You are a strict, neutral MD-benchmark qualitative judge. Score the "
        "agent's submission on each rubric as a float in [0,1] (1 = fully meets "
        "the rubric).\n\n"
        "IMPORTANT: these study answers have well-known literature values, and a "
        "capable agent already knows the textbook direction. The scorer already "
        "verifies deterministically, from the submitted trajectories, that the "
        "reported observable numbers are real and that their sign is consistent "
        "with the claimed direction. Your job is NOT to re-verify the numbers or "
        "reward stating the known direction. Judge only the QUALITATIVE reasoning "
        "and calibration around those numbers.\n\n"
        "Rubric meanings: reasoning_logic = the written reasoning coherently and "
        "correctly connects the submission's own observables (their values, "
        "signs, and uncertainties) to the stated effect.direction; the argument "
        "is logically sound rather than a bare assertion or a hand-wave. Score "
        "LOW if the reasoning is absent, generic, internally inconsistent, or "
        "ignores its own numbers. confidence_calibration = the stated confidence "
        "matches the strength of the evidence, including the observable's "
        "separation relative to its uncertainty (do not reward high confidence "
        "on a near-zero or noisy separation). overclaim_detection = the "
        "submission does NOT overclaim (e.g. does not claim a converged free "
        "energy from finite MD) — 1 means no overclaiming. limitations = "
        "limitations are explicit, relevant, and honest; methods_traceability = "
        "the conclusion is traceable to stated methods and evidence.\n\n"
        f"Rubrics to score: {rubrics}\n\n"
        "Submission material:\n"
        f"{material}\n\n"
        "Output ONLY a JSON object, no prose, of the form: "
        '{"scores": {"<rubric>": <float 0..1>, ...}, '
        '"violations": [{"rubric": "<name>", "note": "<why>"}], '
        '"rationale": {"<rubric>": "<one line>"}}. '
        "Include every rubric listed above in scores."
    )

    try:
        raw = _call_claude_judge(prompt, judge_model)
        parsed = _extract_json_object(raw)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "errors": [f"judge call/parse failed: {exc}"]}

    scores = {
        str(k): max(0.0, min(1.0, float(v)))
        for k, v in (parsed.get("scores") or {}).items()
        if isinstance(v, (int, float))
    }
    missing = [r for r in rubrics if r not in scores]
    payload = {
        "enabled": True,
        "judge_model": judge_model,
        "temperature": float(temperature),
        "rubric_version": "1.0",
        "scores": scores,
        "violations": list(parsed.get("violations") or []),
        "rationale": parsed.get("rationale") or {},
    }
    out = Path(output_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    result = {"success": not missing, "output_file": str(out), "scores": scores}
    if missing:
        result["errors"] = [f"judge omitted rubrics: {missing}"]
    return result
