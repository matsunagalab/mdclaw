"""ADK guardrails for MDZen.

Implements tool-level guardrails using ADK's `before_tool_callback` pattern.

Primary goal:
- Ensure user decisions recorded in `workflow_state.json` are respected even if the LLM
  generates incorrect tool args (e.g., "no ligands" but calls `prepare_complex` with
  process_ligands=True).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def _read_workflow_state(tool_context) -> dict[str, Any]:
    """Best-effort parse of workflow_state from ADK ToolContext state."""
    raw = tool_context.state.get("workflow_state")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            return {}

    # Fallback: if the model forgot to call read_workflow_state(), workflow_state may be missing
    # from ToolContext state. In that case, try to load it from disk via session_dir.
    try:
        session_dir = str(tool_context.state.get("session_dir", "") or "")
    except Exception:
        session_dir = ""
    if session_dir:
        try:
            p = Path(session_dir) / "workflow_state.json"
            if p.exists():
                loaded = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    # Mirror back into tool_context.state so subsequent tools/guardrails can use it.
                    try:
                        tool_context.state["workflow_state"] = json.dumps(
                            loaded, ensure_ascii=False, default=str
                        )
                    except Exception:
                        pass
                    return loaded
        except Exception:
            return {}

    return {}


def mdzen_before_tool_guardrail(tool, args: dict[str, Any], tool_context) -> Optional[dict[str, Any]]:
    """ADK before_tool_callback guardrail.

    This function may:
    - mutate `args` in-place to enforce workflow decisions, and return None (allow execution)
    - or return a dict to short-circuit the tool execution (treated as the tool response)

    We prefer in-place correction for determinism.
    """
    try:
        tool_name = getattr(tool, "name", "") or ""
    except Exception:
        tool_name = ""
    tool_name_base = tool_name.split(".")[-1] if tool_name else ""

    wf = _read_workflow_state(tool_context)

    def _resolve_structure_file_placeholder() -> None:
        """Replace placeholder structure_file values with paths from workflow_state."""
        v = args.get("structure_file")
        if not isinstance(v, str) or not v.strip():
            return
        key = v.strip()
        if key in ("merged_pdb", "structure_file") and isinstance(wf.get(key), str) and wf.get(key):
            args["structure_file"] = wf[key]
        # Some prompts may pass the key name without underscores or with slight drift.
        if key == "merged" and isinstance(wf.get("merged_pdb"), str) and wf.get("merged_pdb"):
            args["structure_file"] = wf["merged_pdb"]

    # ---------------------------------------------------------------------
    # Guardrail: respect "no ligands" during read-only analysis
    # ---------------------------------------------------------------------
    if tool_name_base == "analyze_structure_details":
        _resolve_structure_file_placeholder()
        include_types = wf.get("include_types")
        if isinstance(include_types, list) and include_types:
            include_set = {str(x).lower() for x in include_types}
            wants_ligands = "ligand" in include_set
            if not wants_ligands:
                # Ensure the analysis does not trigger ligand-related decisions/questions.
                # (The tool already supports identify_ligands=False.)
                args["identify_ligands"] = False
                try:
                    tool_context.state["mdzen_guardrails_analyze_structure_details_applied"] = True
                except Exception:
                    pass
        return None

    # ---------------------------------------------------------------------
    # Guardrail: block step skipping via update_workflow_state(step=...)
    # ---------------------------------------------------------------------
    if tool_name_base == "update_workflow_state":
        current_step = str(wf.get("current_step") or "")
        requested_step = args.get("step")
        mark_step_complete = bool(args.get("mark_step_complete") or False)
        updates = args.get("updates") if isinstance(args, dict) else None

        # Evaluate completion against the *post-update* view of state.
        wf_after = dict(wf)
        if isinstance(updates, dict):
            wf_after.update(updates)

        def _is_missing(v: Any) -> bool:
            if v is None:
                return True
            if isinstance(v, str):
                return v.strip() == ""
            if isinstance(v, dict):
                return len(v) == 0
            if isinstance(v, list):
                return len(v) == 0
            return False

        # If the model tries to set a different step than the current one, block.
        # This prevents skipping mandatory steps (e.g., select_prepare).
        if requested_step and str(requested_step) != current_step:
            try:
                tool_context.state["mdzen_guardrail_blocked"] = True
                tool_context.state["mdzen_guardrail_block_reason"] = (
                    f"step jump blocked: expected={current_step}, got={requested_step}"
                )
            except Exception:
                pass
            return {
                "success": False,
                "error": f"guardrail: step jump blocked (expected={current_step}, got={requested_step})",
                "expected_step": current_step,
                "requested_step": str(requested_step),
                "mark_step_complete": mark_step_complete,
            }

        # When marking completion, also disallow passing an explicit mismatching step.
        if mark_step_complete and requested_step and str(requested_step) != current_step:
            try:
                tool_context.state["mdzen_guardrail_blocked"] = True
                tool_context.state["mdzen_guardrail_block_reason"] = (
                    f"step mismatch on completion: expected={current_step}, got={requested_step}"
                )
            except Exception:
                pass
            return {
                "success": False,
                "error": f"guardrail: step mismatch on completion (expected={current_step}, got={requested_step})",
                "expected_step": current_step,
                "requested_step": str(requested_step),
                "mark_step_complete": True,
            }

        # If marking a step complete, require the step's outputs to exist in state.
        if mark_step_complete:
            missing: list[str] = []

            if current_step == "acquire_structure":
                if _is_missing(wf_after.get("structure_file")):
                    missing.append("structure_file")
            elif current_step == "select_prepare":
                if _is_missing(wf_after.get("merged_pdb")):
                    missing.append("merged_pdb")
            elif current_step == "structure_decisions":
                if _is_missing(wf_after.get("merged_pdb")):
                    missing.append("merged_pdb")
            elif current_step == "solvate_or_membrane":
                if _is_missing(wf_after.get("solvated_pdb")) and _is_missing(wf_after.get("membrane_pdb")):
                    missing.extend(["solvated_pdb_or_membrane_pdb"])
            elif current_step == "quick_md":
                if _is_missing(wf_after.get("parm7")):
                    missing.append("parm7")
                if _is_missing(wf_after.get("rst7")):
                    missing.append("rst7")
            elif current_step == "validation":
                if _is_missing(wf_after.get("validation_result")):
                    missing.append("validation_result")

            if missing:
                try:
                    tool_context.state["mdzen_guardrail_blocked"] = True
                    tool_context.state["mdzen_guardrail_block_reason"] = (
                        f"cannot complete step without required outputs: step={current_step}, missing={missing}"
                    )
                    tool_context.state["mdzen_guardrail_missing_keys"] = missing
                except Exception:
                    pass
                return {
                    "success": False,
                    "error": f"guardrail: cannot complete {current_step} without required outputs",
                    "expected_step": current_step,
                    "missing": missing,
                    "mark_step_complete": True,
                }

        return None

    if tool_name_base != "prepare_complex":
        return None

    _resolve_structure_file_placeholder()

    # Enforce chain selection when available
    selection_chains = wf.get("selection_chains")
    if isinstance(selection_chains, list) and selection_chains:
        # Only override if missing or clearly inconsistent
        if not args.get("select_chains") or args.get("select_chains") != selection_chains:
            args["select_chains"] = selection_chains

    # Enforce ligand inclusion/exclusion
    include_types = wf.get("include_types")
    if isinstance(include_types, list) and include_types:
        include_set = {str(x).lower() for x in include_types}
        wants_ligands = "ligand" in include_set

        # Always align include_types
        args["include_types"] = include_types

        if not wants_ligands:
            # Strongly disable ligand processing regardless of what the LLM asked for.
            args["process_ligands"] = False
            args["run_parameterization"] = False
            # Remove ligand-specific inputs if present (avoid accidental processing).
            args.pop("ligand_smiles", None)
            args.pop("include_ligand_ids", None)
            # Keep exclude_ligand_ids if present (harmless), but don't require it.

    # Mark state so debugging can confirm the guardrail fired.
    try:
        tool_context.state["mdzen_guardrails_prepare_complex_applied"] = True
    except Exception:
        pass

    return None


__all__ = ["mdzen_before_tool_guardrail"]

