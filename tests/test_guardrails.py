import json

import pytest


class _DummyTool:
    def __init__(self, name: str):
        self.name = name


class _DummyToolContext:
    def __init__(self, workflow_state: dict):
        # ADK ToolContext provides a .state dict; we emulate that here.
        self.state = {"workflow_state": json.dumps(workflow_state, ensure_ascii=False)}


class _DummyToolContextWithSessionDir:
    def __init__(self, session_dir: str):
        # Simulate the failure mode: workflow_state is missing, but session_dir exists.
        self.state = {"session_dir": session_dir}


def test_guardrail_blocks_step_jump_update_workflow_state():
    from mdzen.guardrails import mdzen_before_tool_guardrail

    ctx = _DummyToolContext({"current_step": "acquire_structure"})
    tool = _DummyTool("update_workflow_state")
    args = {"step": "solvate_or_membrane", "mark_step_complete": True}

    res = mdzen_before_tool_guardrail(tool, args, ctx)
    assert isinstance(res, dict)
    assert res.get("success") is False
    assert "step jump blocked" in (res.get("error") or "")
    assert ctx.state.get("mdzen_guardrail_blocked") is True


def test_guardrail_allows_update_workflow_state_same_step():
    from mdzen.guardrails import mdzen_before_tool_guardrail

    ctx = _DummyToolContext({"current_step": "select_prepare"})
    tool = _DummyTool("update_workflow_state")
    args = {"step": "select_prepare", "mark_step_complete": True}

    res = mdzen_before_tool_guardrail(tool, args, ctx)
    # Missing merged_pdb => should be blocked when marking complete
    assert isinstance(res, dict)
    assert res.get("success") is False
    assert "cannot complete select_prepare" in (res.get("error") or "")
    assert ctx.state.get("mdzen_guardrail_blocked") is True


def test_guardrail_allows_complete_select_prepare_when_merged_present():
    from mdzen.guardrails import mdzen_before_tool_guardrail

    ctx = _DummyToolContext({"current_step": "select_prepare", "merged_pdb": "/tmp/merged.pdb"})
    tool = _DummyTool("update_workflow_state")
    args = {"step": "select_prepare", "mark_step_complete": True}

    res = mdzen_before_tool_guardrail(tool, args, ctx)
    assert res is None


def test_guardrail_allows_complete_select_prepare_when_merged_in_updates():
    from mdzen.guardrails import mdzen_before_tool_guardrail

    ctx = _DummyToolContext({"current_step": "select_prepare"})
    tool = _DummyTool("update_workflow_state")
    args = {"step": "select_prepare", "mark_step_complete": True, "updates": {"merged_pdb": "/tmp/merged.pdb"}}

    res = mdzen_before_tool_guardrail(tool, args, ctx)
    assert res is None


def test_guardrail_allows_complete_acquire_structure_when_structure_in_updates():
    from mdzen.guardrails import mdzen_before_tool_guardrail

    ctx = _DummyToolContext({"current_step": "acquire_structure"})
    tool = _DummyTool("update_workflow_state")
    args = {
        "step": "acquire_structure",
        "mark_step_complete": True,
        "updates": {"structure_file": "/tmp/1ake.pdb"},
    }

    res = mdzen_before_tool_guardrail(tool, args, ctx)
    assert res is None


def test_guardrail_enforces_no_ligands_prepare_complex():
    from mdzen.guardrails import mdzen_before_tool_guardrail

    ctx = _DummyToolContext(
        {
            "current_step": "select_prepare",
            "selection_chains": ["A"],
            "include_types": ["protein", "ion"],
        }
    )
    tool = _DummyTool("prepare_complex")
    args = {
        "structure_file": "foo.pdb",
        "select_chains": ["B"],
        "include_types": ["protein", "ligand", "ion"],
        "process_ligands": True,
        "run_parameterization": True,
        "ligand_smiles": {"AP5": "CC"},
        "include_ligand_ids": ["A:AP5:215"],
    }

    res = mdzen_before_tool_guardrail(tool, args, ctx)
    assert res is None
    assert args["select_chains"] == ["A"]
    assert args["include_types"] == ["protein", "ion"]
    assert args["process_ligands"] is False
    assert args["run_parameterization"] is False
    assert "ligand_smiles" not in args
    assert "include_ligand_ids" not in args
    assert ctx.state.get("mdzen_guardrails_prepare_complex_applied") is True


def test_guardrail_enforces_no_ligands_prepare_complex_with_prefixed_tool_name():
    from mdzen.guardrails import mdzen_before_tool_guardrail

    ctx = _DummyToolContext(
        {
            "current_step": "select_prepare",
            "selection_chains": ["A"],
            "include_types": ["protein", "ion"],
        }
    )
    tool = _DummyTool("structure.prepare_complex")
    args = {
        "structure_file": "foo.pdb",
        "select_chains": ["B"],
        "include_types": ["protein", "ligand", "ion"],
        "process_ligands": True,
        "run_parameterization": True,
    }

    res = mdzen_before_tool_guardrail(tool, args, ctx)
    assert res is None
    assert args["select_chains"] == ["A"]
    assert args["include_types"] == ["protein", "ion"]
    assert args["process_ligands"] is False
    assert args["run_parameterization"] is False


def test_guardrail_disables_identify_ligands_in_analyze_structure_details_when_no_ligands():
    from mdzen.guardrails import mdzen_before_tool_guardrail

    ctx = _DummyToolContext(
        {
            "current_step": "structure_decisions",
            "include_types": ["protein", "ion"],
        }
    )
    tool = _DummyTool("research.analyze_structure_details")
    args = {"structure_file": "merged.pdb", "ph": 7.4}

    res = mdzen_before_tool_guardrail(tool, args, ctx)
    assert res is None
    assert args["identify_ligands"] is False
    assert ctx.state.get("mdzen_guardrails_analyze_structure_details_applied") is True


def test_guardrail_leaves_identify_ligands_enabled_when_ligands_requested():
    from mdzen.guardrails import mdzen_before_tool_guardrail

    ctx = _DummyToolContext(
        {
            "current_step": "structure_decisions",
            "include_types": ["protein", "ligand", "ion"],
        }
    )
    tool = _DummyTool("research.analyze_structure_details")
    args = {"structure_file": "merged.pdb", "ph": 7.4}

    res = mdzen_before_tool_guardrail(tool, args, ctx)
    assert res is None
    assert args.get("identify_ligands") is None


def test_guardrail_loads_workflow_state_from_disk_when_missing_in_tool_context(tmp_path):
    from mdzen.guardrails import mdzen_before_tool_guardrail

    wf = {
        "current_step": "select_prepare",
        "selection_chains": ["A"],
        "include_types": ["protein", "ion"],  # no ligands
    }
    (tmp_path / "workflow_state.json").write_text(
        json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ctx = _DummyToolContextWithSessionDir(str(tmp_path))
    tool = _DummyTool("structure.prepare_complex")
    args = {
        "structure_file": "foo.pdb",
        "select_chains": ["B"],
        "include_types": ["protein", "ligand", "ion"],
        "process_ligands": True,
        "run_parameterization": True,
    }

    res = mdzen_before_tool_guardrail(tool, args, ctx)
    assert res is None
    assert args["select_chains"] == ["A"]
    assert args["include_types"] == ["protein", "ion"]
    assert args["process_ligands"] is False
    assert args["run_parameterization"] is False
    # Ensure the fallback also mirrored state into tool_context for downstream calls.
    assert "workflow_state" in ctx.state

