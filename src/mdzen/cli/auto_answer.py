"""Auto-answer helpers for PTY-less testing.

This module enables:
- Batch mode to keep going by accepting defaults ("agent recommended choices")
- Interactive mode to accept answers injected by an external driver (Cursor/Claude Code)
- Optional LLM-based answer generation for tests

The contract is intentionally simple: produce a single-line "user input" string
that can be passed through existing parsing logic (e.g. select_prepare's deterministic parser)
or sent as the next message to the step agent.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ExternalAnswer:
    text: str
    source: str | None = None
    step: str | None = None


def _one_line(text: str) -> str:
    """Normalize to a single CLI line."""
    return re.sub(r"\s+", " ", (text or "").strip())


def write_questions_json(session_dir: str, *, session_id: str | None = None, wf_state: dict[str, Any]) -> None:
    """Write machine-readable pending questions for external drivers.

    Intended for Cursor/Claude Code agents or CI to poll.
    """
    try:
        session_path = Path(session_dir)
        session_path.mkdir(parents=True, exist_ok=True)
        out_path = session_path / "questions.json"

        current_step = str(wf_state.get("current_step") or "")
        pending_questions = wf_state.get("pending_questions") or []
        if not isinstance(pending_questions, list):
            pending_questions = []
        pending_questions = [str(q) for q in pending_questions if str(q).strip()]

        # Provide a hint for the driver when possible
        suggested = ""
        if current_step == "select_prepare":
            chains = wf_state.get("detected_protein_chains") or []
            ligs = wf_state.get("detected_ligands") or []
            if isinstance(chains, list) and chains:
                chosen = "A" if "A" in {str(c).upper() for c in chains} else str(chains[0])
                if isinstance(ligs, list) and ligs:
                    include_ligands_default = os.environ.get("MDZEN_DEFAULT_INCLUDE_LIGANDS", "true").strip().lower()
                    include_ligands = include_ligands_default not in {"0", "false", "no", "off"}
                    suggested = f"{chosen} {'yes' if include_ligands else 'no'}"
                else:
                    suggested = chosen
        if not suggested and pending_questions:
            token = _extract_default_token(pending_questions[0])
            suggested = token or ""

        payload = {
            "schema_version": 1,
            "session_id": session_id or "",
            "current_step": current_step,
            "awaiting_user_input": bool(wf_state.get("awaiting_user_input")),
            "pending_questions": pending_questions,
            "suggested_reply_format": suggested,
            "detected": {
                "protein_chains": wf_state.get("detected_protein_chains") or [],
                "ligands": wf_state.get("detected_ligands") or [],
            },
            # Keep digest minimal to reduce churn
            "workflow_state_digest": {
                "current_step": current_step,
                "completed_steps": wf_state.get("completed_steps") or [],
                "structure_file": wf_state.get("structure_file") or "",
                "merged_pdb": wf_state.get("merged_pdb") or "",
            },
        }

        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(out_path)
    except Exception:
        # Best-effort only: never fail the workflow because of UI plumbing.
        return


def _extract_default_token(question: str) -> str | None:
    """Extract default token from prompt text.

    Supports patterns like:
    - "(default: X)"
    - "default: X"
    - "default = X"
    """
    q = question or ""
    m = re.search(r"default\s*[:=]\s*([^\)\n\r]+)", q, flags=re.IGNORECASE)
    if not m:
        return None
    token = _one_line(m.group(1))
    # Trim trailing punctuation that often follows defaults
    token = token.rstrip(".").strip()
    return token or None


def build_default_answer(wf_state: dict[str, Any], questions: list[str]) -> str | None:
    """Best-effort deterministic answer.

    Intended to keep tests moving when the agent asks questions.
    """
    questions = [q for q in (questions or []) if isinstance(q, str) and q.strip()]
    if not questions:
        return None

    detected_chains = wf_state.get("detected_protein_chains") or []
    detected_ligands = wf_state.get("detected_ligands") or []
    if not isinstance(detected_chains, list):
        detected_chains = []
    if not isinstance(detected_ligands, list):
        detected_ligands = []

    # Heuristic for select_prepare: a single line can answer both chain + ligand questions (e.g., "A no")
    lower_questions = " ".join(q.lower() for q in questions)
    looks_like_chain_q = "protein chains" in lower_questions or "which protein chains" in lower_questions
    looks_like_ligand_q = "ligand" in lower_questions and "include" in lower_questions

    if looks_like_chain_q or looks_like_ligand_q:
        chosen_chain = None
        chains_upper = [str(c).strip() for c in detected_chains if str(c).strip()]
        if chains_upper:
            # Prefer chain A when present (common expectation)
            chosen_chain = "A" if "A" in {c.upper() for c in chains_upper} else chains_upper[0]

        include_ligands_default = os.environ.get("MDZEN_DEFAULT_INCLUDE_LIGANDS", "true").strip().lower()
        include_ligands = include_ligands_default not in {"0", "false", "no", "off"}

        if chosen_chain and detected_ligands:
            return _one_line(f"{chosen_chain} {'yes' if include_ligands else 'no'}")
        if chosen_chain:
            return _one_line(chosen_chain)
        if detected_ligands:
            return "yes" if include_ligands else "no"

    # Generic: pick defaults embedded in question text
    defaults: list[str] = []
    for q in questions:
        token = _extract_default_token(q)
        if token:
            defaults.append(token)

    if defaults:
        # One-line join: some steps accept "explicit opc" etc.
        return _one_line(" ".join(defaults))

    return None


def _answers_paths(session_dir: str) -> tuple[Path, Path]:
    base = Path(session_dir)
    return base / "answers.json", base / "answers.txt"


def _consume_file_atomically(path: Path) -> Path | None:
    """Rename the file to a consumed name to avoid double reads."""
    if not path.exists():
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    consumed = path.with_name(f"{path.stem}.consumed.{ts}.{os.getpid()}{path.suffix}")
    try:
        path.rename(consumed)
        return consumed
    except Exception:
        return None


def read_external_answer_once(session_dir: str, expected_step: str | None = None) -> ExternalAnswer | None:
    """Read an externally injected answer if present.

    Files supported:
    - answers.json: {"schema_version": 1, "text": "...", "source": "...", "step": "..."}
    - answers.txt: first line is the answer
    """
    answers_json, answers_txt = _answers_paths(session_dir)

    if answers_json.exists():
        try:
            data = json.loads(answers_json.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                step = str(data.get("step") or "").strip() or None
                if expected_step and step and step != expected_step:
                    # Ignore mismatched answers (likely stale)
                    return None
                text = _one_line(str(data.get("text") or ""))
                if not text:
                    return None
                consumed = _consume_file_atomically(answers_json)
                if consumed is None:
                    # Best effort: if we couldn't rename, still proceed.
                    pass
                return ExternalAnswer(text=text, source=str(data.get("source") or "") or None, step=step)
        except Exception:
            # If parsing fails, do not consume (keeps it inspectable)
            return None

    if answers_txt.exists():
        try:
            raw = answers_txt.read_text(encoding="utf-8")
            first = _one_line(raw.splitlines()[0] if raw else "")
            if not first:
                return None
            _ = _consume_file_atomically(answers_txt)
            return ExternalAnswer(text=first, source="answers.txt", step=expected_step)
        except Exception:
            return None

    return None


async def wait_for_external_answer(
    session_dir: str,
    *,
    expected_step: str | None = None,
    timeout_s: float | None = None,
    poll_interval_s: float | None = None,
) -> ExternalAnswer | None:
    """Poll for an external answer file until timeout."""
    if timeout_s is None:
        timeout_s = float(os.environ.get("MDZEN_ANSWER_TIMEOUT_S", "300") or 300)
    if poll_interval_s is None:
        poll_interval_s = float(os.environ.get("MDZEN_ANSWER_POLL_INTERVAL_S", "1.0") or 1.0)

    deadline = time.monotonic() + max(timeout_s, 0.0)
    while time.monotonic() <= deadline:
        ans = read_external_answer_once(session_dir, expected_step=expected_step)
        if ans:
            return ans
        await asyncio.sleep(max(poll_interval_s, 0.1))
    return None


async def llm_generate_answer(
    *,
    model: str,
    wf_state: dict[str, Any],
    questions: list[str],
) -> str | None:
    """Generate an answer with an LLM (tests only).

    Returns a single line suitable as a user reply.
    """
    try:
        from litellm import acompletion
        from mdzen.config import get_litellm_model
    except Exception:
        return None

    qs = [q for q in (questions or []) if isinstance(q, str) and q.strip()]
    if not qs:
        return None

    llm_model = get_litellm_model(model)
    prompt = "\n".join(f"- {q}" for q in qs)
    # Keep the context very small and force a single-line output.
    sys_msg = (
        "You are an automated test driver for MDZen.\n"
        "Answer the user's pending questions with a SINGLE LINE ONLY.\n"
        "Prefer defaults if present. If chains are asked, pick chain A if available.\n"
        "If ligands are asked, follow MDZEN_DEFAULT_INCLUDE_LIGANDS if set; otherwise include ligands.\n"
        "Return only the answer line, no explanations."
    )
    user_msg = (
        "Pending questions:\n"
        f"{prompt}\n\n"
        f"Detected context (may be empty):\n{json.dumps({'detected_protein_chains': wf_state.get('detected_protein_chains'), 'detected_ligands': wf_state.get('detected_ligands')}, ensure_ascii=False)}"
    )
    try:
        resp = await acompletion(
            model=llm_model,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,
        )
        content = ""
        # LiteLLM response shape: choices[0].message.content
        if isinstance(resp, dict):
            choices = resp.get("choices") or []
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message") or {}
                content = msg.get("content") or ""
        text = _one_line(str(content))
        return text or None
    except Exception:
        return None


async def resolve_answer(
    session_dir: str,
    wf_state: dict[str, Any],
    questions: list[str],
    *,
    mode: str,
    expected_step: str | None = None,
    timeout_s: float | None = None,
    poll_interval_s: float | None = None,
    llm_model: str | None = None,
) -> str | None:
    """Resolve a single-line answer for pending questions.

    Args:
        session_dir: job/session directory for reading injected answers
        wf_state: current workflow state dict
        questions: list of pending question strings
        mode: one of "external", "default", "llm"
        expected_step: if set, external answers with a mismatched step are ignored
    """
    mode = (mode or "").strip().lower()
    if mode == "external":
        ans = await wait_for_external_answer(
            session_dir,
            expected_step=expected_step,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )
        return ans.text if ans else None

    if mode == "default":
        return build_default_answer(wf_state, questions)

    if mode == "llm":
        model = llm_model or os.environ.get("MDZEN_TEST_ANSWER_MODEL") or ""
        model = str(model).strip()
        if not model:
            return None
        return await llm_generate_answer(model=model, wf_state=wf_state, questions=questions)

    return None

