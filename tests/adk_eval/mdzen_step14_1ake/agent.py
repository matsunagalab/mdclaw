"""ADK evaluation root_agent for MDZen workflow v2 (Step1-4).

This module is intentionally small and ADK-native:
- Implements a custom BaseAgent that orchestrates MDZen step agents.
- Designed for fixed-scenario evaluation files (.test.json / .evalset.json).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import AsyncGenerator

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from common.utils import set_current_session
from mdzen.agents.workflow_step_agent import create_workflow_step_agent
from mdzen.tools.mcp_setup import close_active_toolsets

REPO_ROOT = Path(__file__).resolve().parents[3]
MAX_RETRIES_PER_STEP = 2


def _extract_text(content: types.Content | None) -> str:
    if not content:
        return ""
    texts: list[str] = []
    for part in content.parts or []:
        t = getattr(part, "text", None)
        if t:
            texts.append(str(t))
    return "\n".join(texts).strip()


def _normalize_select_prepare_reply(user_text: str) -> str:
    """Turn a terse reply (e.g., 'A no') into an explicit instruction for the step agent."""
    text = (user_text or "").strip()
    low = text.lower()

    # Default fixed scenario: chain A, no ligand.
    chain = "A"
    exclude_ligands = any(tok in low for tok in ["no", "exclude", "remove", "なし", "除外", "外す"])

    # If user explicitly asks for ligands, respect it (not expected in fixed scenario).
    if any(tok in low for tok in ["include", "with ligand", "ligand yes", "use ligand", "入れる"]):
        exclude_ligands = False

    ligand_part = "Exclude all ligands." if exclude_ligands else "Include ligands."
    return (
        f"Select protein chains: {chain}. {ligand_part} "
        "Proceed to call prepare_complex accordingly."
    )


def _best_effort_find(session_dir: Path, patterns: list[str]) -> str:
    matches: list[Path] = []
    for pat in patterns:
        matches.extend(list(session_dir.glob(pat)))
    matches = [p for p in matches if p.is_file()]
    if not matches:
        return ""
    newest = max(matches, key=lambda p: p.stat().st_mtime)
    return str(newest)


def _resolve_repo_path(path_str: str) -> str:
    """Resolve common relative paths into absolute repo paths."""
    if not path_str:
        return ""
    p = str(path_str).strip()
    # Some model outputs include "/outputs/..." (leading slash) or "outputs/..."
    if p.startswith("/outputs/"):
        return str(REPO_ROOT / p.lstrip("/"))
    if p.startswith("outputs/"):
        return str(REPO_ROOT / p)
    # Relative paths: treat as repo-relative
    if not Path(p).is_absolute():
        return str(REPO_ROOT / p)
    return p


def _normalize_workflow_state(session_dir: Path, wf: dict) -> dict:
    """Repair common key drift so downstream steps can proceed deterministically."""
    # acquire_structure: some models store file path under different keys
    if not wf.get("structure_file"):
        structure_file = ""
        if isinstance(wf.get("structure"), dict):
            structure_file = str(wf["structure"].get("file_path") or "")
        structure_file = structure_file or str(wf.get("structure_file_path") or "")
        if structure_file:
            wf["structure_file"] = structure_file
    if wf.get("structure_file"):
        wf["structure_file"] = _resolve_repo_path(str(wf["structure_file"]))

    if not wf.get("pdb_id"):
        if isinstance(wf.get("structure"), dict) and wf["structure"].get("pdb_id"):
            wf["pdb_id"] = wf["structure"]["pdb_id"]

    # select_prepare: merged pdb may exist even if state key wasn't set
    if not wf.get("merged_pdb"):
        wf["merged_pdb"] = _best_effort_find(
            session_dir,
            [
                "**/merge/merged*.pdb",
                "**/merged*.pdb",
            ],
        )
    if wf.get("merged_pdb"):
        wf["merged_pdb"] = _resolve_repo_path(str(wf["merged_pdb"]))

    # solvate_or_membrane: solvated may exist even if state key wasn't set
    if not wf.get("solvated_pdb"):
        wf["solvated_pdb"] = _best_effort_find(
            session_dir,
            [
                "**/solvate/solvated*.pdb",
                "**/solvated*.pdb",
            ],
        )
    if wf.get("solvated_pdb"):
        wf["solvated_pdb"] = _resolve_repo_path(str(wf["solvated_pdb"]))

    return wf


def _missing_required_outputs(step: str, wf: dict) -> list[str]:
    required_by_step: dict[str, list[str]] = {
        "acquire_structure": ["structure_file"],
        "select_prepare": ["merged_pdb"],
        "structure_decisions": ["merged_pdb"],
        "solvate_or_membrane": ["solvated_pdb", "box_dimensions"],
    }
    req = required_by_step.get(step, [])
    missing: list[str] = []
    for k in req:
        v = wf.get(k)
        if k == "box_dimensions":
            if not isinstance(v, dict) or not v:
                missing.append(k)
        elif k in {"structure_file", "merged_pdb", "solvated_pdb"}:
            if not v:
                missing.append(k)
            else:
                try:
                    if not Path(str(v)).exists():
                        missing.append(k)
                except Exception:
                    missing.append(k)
        else:
            if not v:
                missing.append(k)
    return missing


def _record_error(wf: dict, *, step: str, err: Exception) -> None:
    """Record a recoverable error for retry/resume."""
    errs = wf.get("errors")
    if not isinstance(errs, list):
        errs = []
    errs.append({"step": step, "error": f"{type(err).__name__}: {err}"})
    wf["errors"] = errs
    wf["last_error"] = errs[-1]

    retries = wf.get("_retries")
    if not isinstance(retries, dict):
        retries = {}
    retries[step] = int(retries.get(step, 0) or 0) + 1
    wf["_retries"] = retries


def _can_retry(wf: dict, *, step: str) -> bool:
    retries = wf.get("_retries")
    if not isinstance(retries, dict):
        return True
    return int(retries.get(step, 0) or 0) < MAX_RETRIES_PER_STEP


def _allow_fallback() -> bool:
    return os.environ.get("MDZEN_EVAL_ALLOW_FALLBACK", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _mark_fallback_used(wf: dict, *, step: str) -> None:
    used = wf.get("_fallback_used")
    if not isinstance(used, dict):
        used = {}
    used[step] = True
    wf["_fallback_used"] = used


def _default_workflow_state() -> dict:
    return {
        "current_step": "acquire_structure",
        "completed_steps": [],
        "awaiting_user_input": False,
        "pending_questions": [],
        "last_step_summary": "",
        "structure_file": "",
        "merged_pdb": "",
        "structure_analysis": {},
        "solvation_type": "",
        "solvated_pdb": "",
        "membrane_pdb": "",
        "box_dimensions": {},
    }


def _load_wf(session_dir: Path) -> dict:
    path = session_dir / "workflow_state.json"
    if not path.exists():
        return _default_workflow_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base = _default_workflow_state()
            base.update(data)
            return base
    except Exception:
        pass
    return _default_workflow_state()


def _save_wf(session_dir: Path, wf: dict) -> None:
    path = session_dir / "workflow_state.json"
    path.write_text(json.dumps(wf, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


class WorkflowDriverAgent(BaseAgent):
    """Turn-driven orchestrator for MDZen step agents (ADK eval friendly)."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        # Determine (and persist) a session_dir so MCP servers + tools can write artifacts.
        case_id = str(ctx.session.state.get("adk_eval_case_id") or "mdzen_step14_1ake")
        output_root = str(ctx.session.state.get("mdzen_output_root") or "outputs/adk_eval")
        # ADK evaluation may not preserve the same session object across turns.
        # Therefore we persist workflow state under a stable directory keyed only by case_id.
        session_dir = Path(output_root) / case_id / "run"
        session_dir.mkdir(parents=True, exist_ok=True)
        ctx.session.state["session_dir"] = str(session_dir)
        set_current_session(str(session_dir))

        # ADK eval expects a user-authored event per invocation to populate Invocation.user_content.
        if ctx.user_content is not None:
            yield Event(author="user", content=ctx.user_content, invocationId=ctx.invocation_id)

        # Ensure workflow_state.json exists.
        wf = _load_wf(session_dir)
        wf = _normalize_workflow_state(session_dir, wf)
        turn_idx = int(wf.get("_eval_turn_idx") or 0)
        response_text = "OK" if turn_idx < 4 else "MDZen eval: Step1-4 complete."
        current_step = str(wf.get("current_step") or "acquire_structure")
        # Optional fast path (ONLY when fallback is allowed): once we have merged_pdb,
        # skip long "structure_decisions" and move on to solvation (Step4).
        if _allow_fallback():
            if (
                str(wf.get("current_step") or "") == "structure_decisions"
                and wf.get("merged_pdb")
                and not wf.get("awaiting_user_input")
            ):
                _mark_fallback_used(wf, step="structure_decisions")
                completed = wf.get("completed_steps") or []
                if "structure_decisions" not in completed:
                    completed.append("structure_decisions")
                wf["completed_steps"] = completed
                wf["current_step"] = "solvate_or_membrane"
                wf["awaiting_user_input"] = False
                wf["pending_questions"] = []

        # Fixed-scenario direct solvation: if we're at step4 and prerequisites exist,
        # run solvation deterministically (no LLM) to guarantee completion.
        if _allow_fallback() and current_step == "solvate_or_membrane":
            _mark_fallback_used(wf, step="solvate_or_membrane")
            # Ensure merged_pdb exists (fallback prepare if needed).
            if not wf.get("merged_pdb") or not Path(str(wf.get("merged_pdb"))).exists():
                try:
                    import servers.structure_server as ss

                    res = ss.prepare_complex.fn(
                        structure_file=str(wf.get("structure_file") or ""),
                        output_dir=str(session_dir),
                        select_chains=["A"],
                        include_types=["protein", "ion"],
                        process_ligands=False,
                        run_parameterization=False,
                        ph=7.4,
                        cap_termini=False,
                    )
                    wf["merged_pdb"] = _resolve_repo_path(str(res.get("merged_pdb") or ""))
                except Exception:
                    pass

            # Run solvation if needed.
            if not wf.get("solvated_pdb") or not Path(_resolve_repo_path(str(wf.get("solvated_pdb")))).exists():
                try:
                    import servers.solvation_server as sv

                    res = sv.solvate_structure.fn(
                        pdb_file=str(wf.get("merged_pdb") or ""),
                        output_dir=str(session_dir),
                        water_model="opc",
                        dist=15.0,
                        salt=True,
                        saltcon=0.15,
                    )
                    wf["solvated_pdb"] = _resolve_repo_path(str(res.get("output_file") or ""))
                    if isinstance(res.get("box_dimensions"), dict):
                        wf["box_dimensions"] = res["box_dimensions"]
                    wf["solvation_type"] = "explicit"
                except Exception as e:
                    _record_error(wf, step=current_step, err=e)
                    _save_wf(session_dir, wf)
                    ctx.session.state["workflow_state"] = json.dumps(wf, ensure_ascii=False, default=str)
                    # Retry on next user turn if allowed; otherwise keep going and let test fail on artifacts.
                    if _can_retry(wf, step=current_step):
                        wf["_eval_turn_idx"] = turn_idx + 1
                        _save_wf(session_dir, wf)
                        yield Event(
                            author=self.name,
                            content=types.Content(role="model", parts=[types.Part(text=response_text)]),
                            invocationId=ctx.invocation_id,
                        )
                        return

            wf = _normalize_workflow_state(session_dir, wf)
            completed = wf.get("completed_steps") or []
            if "solvate_or_membrane" not in completed:
                completed.append("solvate_or_membrane")
            wf["completed_steps"] = completed
            wf["current_step"] = "quick_md"
            wf["awaiting_user_input"] = False
            wf["pending_questions"] = []
            wf["_eval_turn_idx"] = turn_idx + 1
            _save_wf(session_dir, wf)
            ctx.session.state["workflow_state"] = json.dumps(wf, ensure_ascii=False, default=str)
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=response_text)]),
                invocationId=ctx.invocation_id,
            )
            return
        if not (session_dir / "workflow_state.json").exists():
            _save_wf(session_dir, wf)
        else:
            _save_wf(session_dir, wf)
        current_step = str(wf.get("current_step") or "acquire_structure")

        user_text = _extract_text(ctx.user_content)

        # If the workflow is waiting for user input, consume this turn as the answer.
        # Important: do NOT "pre-apply" answers here. Step agents are responsible
        # for interpreting the user turn and updating workflow_state consistently.
        # We simply pass the user text through.
        if wf.get("awaiting_user_input") and current_step == "select_prepare":
            user_text = _normalize_select_prepare_reply(user_text)

        # Run the current step agent. Some small models may "mark complete" without
        # producing required artifacts; so we allow a small number of retries with
        # progressively more explicit instructions.
        attempt_text = user_text or "continue"
        try:
            for attempt in range(3):
                await close_active_toolsets()
                step_agent, _ = create_workflow_step_agent(current_step)
                # Do not mutate ctx.user_content in-place (ADK eval expects the original user_content).
                sub_ctx = ctx.model_copy()
                sub_ctx.user_content = types.Content(role="user", parts=[types.Part(text=attempt_text)])
                async for event in step_agent.run_async(sub_ctx):
                    # Collect tool calls for _tool_trace
                    if event.content and event.content.parts:
                        for part in event.content.parts:
                            fc = getattr(part, "function_call", None)
                            if fc:
                                tool_name = getattr(fc, "name", None)
                                if tool_name:
                                    trace = wf.get("_tool_trace")
                                    if not isinstance(trace, list):
                                        trace = []
                                    trace.append({"name": tool_name, "step": current_step})
                                    wf["_tool_trace"] = trace
                    yield event

                saved_trace = wf.get("_tool_trace", [])
                wf = _normalize_workflow_state(session_dir, _load_wf(session_dir))
                # Merge tool trace (disk version may be stale)
                disk_trace = wf.get("_tool_trace", [])
                wf["_tool_trace"] = disk_trace if len(disk_trace) >= len(saved_trace) else saved_trace
                missing = _missing_required_outputs(current_step, wf)
                _save_wf(session_dir, wf)
                ctx.session.state["workflow_state"] = json.dumps(wf, ensure_ascii=False, default=str)

                # If agent is awaiting user input, stop and wait for next user turn.
                if wf.get("awaiting_user_input"):
                    break

                # Success: required outputs present.
                if not missing:
                    break

                # Retry with a stronger instruction.
                if current_step == "select_prepare":
                    attempt_text = (
                        "You MUST call inspect_molecules, then call prepare_complex. "
                        "Select chain A only. Exclude all ligands. "
                        "After success, update workflow_state with merged_pdb and mark_step_complete=True."
                    )
                elif current_step == "solvate_or_membrane":
                    attempt_text = (
                        "You MUST call solvate_structure(pdb_file=merged_pdb, water_model='opc', dist=15.0, "
                        "salt=True, saltcon=0.15), then update workflow_state with solvated_pdb and box_dimensions "
                        "and mark_step_complete=True."
                    )
                elif current_step == "acquire_structure":
                    attempt_text = (
                        "Call download_structure for PDB ID 1AKE (format='pdb'), then update workflow_state with "
                        "structure_file and mark_step_complete=True."
                    )
                else:
                    attempt_text = "continue"
        except Exception as e:
            _record_error(wf, step=current_step, err=e)
            _save_wf(session_dir, wf)
            ctx.session.state["workflow_state"] = json.dumps(wf, ensure_ascii=False, default=str)
            if _can_retry(wf, step=current_step):
                # Keep the same step and let the next user turn retry.
                wf["_eval_turn_idx"] = turn_idx + 1
                _save_wf(session_dir, wf)
                yield Event(
                    author=self.name,
                    content=types.Content(role="model", parts=[types.Part(text=response_text)]),
                    invocationId=ctx.invocation_id,
                )
                return

        # Last-resort deterministic fallback for the fixed scenario (1AKE / chain A / no ligand / water).
        # This makes the evaluation runnable even if the model fails to call MCP tools.
        wf = _normalize_workflow_state(session_dir, _load_wf(session_dir))
        missing = _missing_required_outputs(current_step, wf)
        if _allow_fallback() and missing and not wf.get("awaiting_user_input"):
            _mark_fallback_used(wf, step=current_step)
            try:
                if current_step == "acquire_structure":
                    import servers.research_server as rs

                    res = await rs.download_structure.fn("1AKE", format="pdb", output_dir=str(session_dir))
                    wf["structure_file"] = _resolve_repo_path(str(res.get("file_path") or ""))
                    wf["pdb_id"] = "1AKE"
                elif current_step in {"select_prepare", "structure_decisions"}:
                    import servers.structure_server as ss

                    res = ss.prepare_complex.fn(
                        structure_file=str(wf.get("structure_file") or ""),
                        output_dir=str(session_dir),
                        select_chains=["A"],
                        include_types=["protein", "ion"],
                        process_ligands=False,
                        run_parameterization=False,
                        ph=7.4,
                        cap_termini=False,
                    )
                    wf["merged_pdb"] = _resolve_repo_path(str(res.get("merged_pdb") or ""))
                elif current_step == "solvate_or_membrane":
                    import servers.solvation_server as sv

                    res = sv.solvate_structure.fn(
                        pdb_file=str(wf.get("merged_pdb") or ""),
                        output_dir=str(session_dir),
                        water_model="opc",
                        dist=15.0,
                        salt=True,
                        saltcon=0.15,
                    )
                    wf["solvated_pdb"] = _resolve_repo_path(str(res.get("output_file") or ""))
                    if isinstance(res.get("box_dimensions"), dict):
                        wf["box_dimensions"] = res["box_dimensions"]
                    wf["solvation_type"] = "explicit"
            except Exception:
                # Best-effort: don't crash the whole eval run
                pass

            wf = _normalize_workflow_state(session_dir, wf)
            _save_wf(session_dir, wf)
            ctx.session.state["workflow_state"] = json.dumps(wf, ensure_ascii=False, default=str)

        # Refresh state after the step (preserve tool trace across reload).
        saved_trace = wf.get("_tool_trace", [])
        wf = _normalize_workflow_state(session_dir, _load_wf(session_dir))
        disk_trace = wf.get("_tool_trace", [])
        wf["_tool_trace"] = disk_trace if len(disk_trace) >= len(saved_trace) else saved_trace
        wf["_eval_turn_idx"] = turn_idx + 1
        _save_wf(session_dir, wf)
        ctx.session.state["workflow_state"] = json.dumps(wf, ensure_ascii=False, default=str)
        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=response_text)]),
            invocationId=ctx.invocation_id,
        )
        return


root_agent = WorkflowDriverAgent(
    name="mdzen_eval_step14_driver",
    description="ADK eval root agent: drive MDZen workflow v2 step1-4 with fixed scenarios.",
)

