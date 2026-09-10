"""The agent-facing CLI contract: envelope, DAG context, fix-carrying errors.

These tests pin what a skill-less agent sees on stdout: the first keys of
every result, the ``dag``/``next`` blocks, the ``result.json`` beside a node,
and errors that name the node to use or the command that creates it. They were
written from the 2026-09-10 MDDataBench campaign transcripts (see
``docs/research/cli-agentic-usability-20260910.md``).
"""

from __future__ import annotations

import inspect
import json
import re

import pytest

from mdclaw._envelope import (
    BRIEF_LIMIT,
    ENVELOPE_ORDER,
    brief_result,
    dag_context,
    helper_stage_hint,
    next_step,
    order_envelope,
    stage_tools_for,
    write_result_file,
)
from mdclaw._node import create_node, update_job_params
from mdclaw.node.constants import normalize_node_type, suggest_node_type
from mdclaw.node.snapshot import dag_snapshot, node_missing_error
from tests.pipeline_helpers import complete_node_with_placeholders as complete_node


@pytest.fixture
def job_dir(tmp_path):
    jd = tmp_path / "job_envelope"
    jd.mkdir()
    update_job_params(str(jd), {"solvent_regime": "explicit"})
    return jd


def _complete(job_dir, node_type, artifacts, **kwargs):
    res = create_node(str(job_dir), node_type, **kwargs)
    assert res["success"], res
    complete_node(str(job_dir), res["node_id"], artifacts=artifacts)
    return res["node_id"]


def _tools():
    from mdclaw._cli import _discover_tools

    return _discover_tools()


def _run_cli(argv):
    """Run ``mdclaw`` in-process; return (exit_code, parsed stdout JSON)."""
    from mdclaw._cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    return exc_info.value.code


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


class TestEnvelope:
    def test_envelope_keys_come_first_in_the_declared_order(self):
        result = {"zeta": 1, "warnings": ["w"], "success": True, "node_id": "min_001",
                  "message": "done", "alpha": 2}
        ordered = order_envelope(result, node_status="completed", result_file="/r.json")
        keys = list(ordered)
        head = [k for k in ENVELOPE_ORDER if k in ordered]
        assert keys[: len(head)] == head
        assert ordered["warnings_count"] == 1
        assert ordered["result_file"] == "/r.json"

    def test_success_without_message_gets_the_node_summary(self):
        ordered = order_envelope({"success": True}, node_id="eq_002", node_status="completed",
                                 include_node_keys=True)
        assert ordered["message"] == "eq_002 completed"
        assert order_envelope({"success": True})["message"] == "ok"

    def test_brief_stubs_only_large_unprotected_values(self):
        big = {"k%d" % i: "x" * 100 for i in range(100)}
        result = {"success": True, "errors": ["e" * (BRIEF_LIMIT + 10)],
                  "structure_analysis": big, "small": {"a": 1}, "count": 3}
        brief = brief_result(result, result_file="/job/nodes/prep_001/result.json")
        assert brief["errors"] == result["errors"]  # protected
        assert brief["small"] == {"a": 1}
        assert brief["count"] == 3
        stub = brief["structure_analysis"]
        assert stub["_omitted"] is True
        assert stub["see"] == "/job/nodes/prep_001/result.json#structure_analysis"
        assert stub["chars"] > BRIEF_LIMIT
        assert stub["keys"][:2] == ["k0", "k1"]

    def test_result_file_is_written_beside_the_node(self, job_dir):
        res = create_node(str(job_dir), "source")
        path = write_result_file({"success": True, "x": 1}, str(job_dir), res["node_id"])
        assert path.endswith(f"nodes/{res['node_id']}/result.json")
        assert json.loads(open(path).read()) == {"success": True, "x": 1}
        assert write_result_file({"success": True}, str(job_dir), "nope_001") is None


# ---------------------------------------------------------------------------
# DAG context and the next step
# ---------------------------------------------------------------------------


class TestNextStep:
    def test_empty_job_starts_with_the_source_node(self, job_dir):
        step = next_step(str(job_dir), None, _tools())
        assert step["action"] == "create"
        assert step["node_type"] == "source"
        assert "--node-type source" in step["create_command"]
        assert "fetch_structure" in step["stage_tools"]

    def test_pending_node_is_run_with_its_stage_tool(self, job_dir):
        source = _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
        prep = create_node(str(job_dir), "prep", parent_node_ids=[source])["node_id"]
        step = next_step(str(job_dir), prep, _tools())
        assert step["action"] == "run"
        assert step["stage_tools"][0] == "prepare_complex"
        assert f"--node-id {prep} prepare_complex" in step["run_command"]
        assert "batch_command" not in step

    def test_batch_stages_also_get_the_submit_command(self, job_dir):
        source = _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
        prep = _complete(job_dir, "prep", {"merged_pdb": "artifacts/m.pdb"},
                         parent_node_ids=[source])
        solv = _complete(job_dir, "solv", {"solvated_pdb": "artifacts/s.pdb"},
                         parent_node_ids=[prep])
        topo = _complete(job_dir, "topo", {"system_xml": "artifacts/sys.xml",
                                           "topology_pdb": "artifacts/t.pdb",
                                           "state_xml": "artifacts/st.xml"},
                         parent_node_ids=[solv])
        step = next_step(str(job_dir), topo, _tools())
        assert step["action"] == "create"
        assert step["node_type"] == "min"
        assert f"--parent-node-ids {topo}" in step["create_command"]
        assert "submit_job" in step["batch_command"]

    def test_blocked_child_points_at_the_pending_parent(self, job_dir):
        source = _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
        prep = create_node(str(job_dir), "prep", parent_node_ids=[source])["node_id"]
        solv = create_node(str(job_dir), "solv", parent_node_ids=[prep])["node_id"]
        step = next_step(str(job_dir), solv, _tools())
        assert step["action"] == "run"
        assert step["node_id"] == prep
        assert step["blocked_node_id"] == solv
        assert prep in step["reason"] and "pending" in step["reason"]

    def test_failed_node_gets_trace_and_branch(self, job_dir):
        from mdclaw._node import fail_node

        source = _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
        prep = create_node(str(job_dir), "prep", parent_node_ids=[source])["node_id"]
        fail_node(str(job_dir), prep, errors=["boom"])
        step = next_step(str(job_dir), prep, _tools())
        assert step["action"] == "branch"
        assert "trace_failure" in step["trace_command"]
        assert f"--node-type prep --parent-node-ids {source}" in step["create_command"]

    def test_membrane_regime_prefers_embed_in_membrane(self):
        tools = _tools()
        assert stage_tools_for("solv", tools, {"solvent_regime": "membrane"})[0] == "embed_in_membrane"
        assert stage_tools_for("solv", tools, {"solvent_regime": "explicit"})[0] == "solvate_structure"

    def test_dag_context_carries_snapshot_and_status(self, job_dir):
        source = create_node(str(job_dir), "source")["node_id"]
        context = dag_context(str(job_dir), source, _tools())
        assert context["dag"] == dag_snapshot({source: {"type": "source", "status": "pending",
                                                        "parents": []}}) or context["dag"]["pending"] == [source]
        assert context["node_status"] == "pending"
        assert context["next"]["action"] == "run"


# ---------------------------------------------------------------------------
# Fix-carrying errors
# ---------------------------------------------------------------------------


class TestFixCarryingErrors:
    def test_node_missing_lists_existing_ids_of_that_type(self, job_dir):
        source = _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
        solv = create_node(str(job_dir), "solv", parent_node_ids=[source])["node_id"]
        err = node_missing_error(str(job_dir), "membrane_001", expected_type="solv")
        assert err["code"] == "node_missing"
        assert err["existing_node_ids"] == [source, solv]
        assert any(solv in h for h in err["hints"])
        assert f"--node-id {solv}" in err["next_action"]

    def test_node_missing_on_an_empty_job_says_to_create_source(self, job_dir):
        err = node_missing_error(str(job_dir), "prep_001")
        assert "--node-type source" in err["next_action"]

    def test_invalid_node_type_suggests_the_stage(self, job_dir):
        res = create_node(str(job_dir), "membrane")
        assert res["success"] is False
        assert res["code"] == "invalid_node_type"
        assert res["suggested_node_type"] == "solv"
        assert res["valid_node_types"][0] == "source"
        assert "--node-type solv" in res["next_action"]

    def test_long_stage_names_are_accepted_as_aliases(self, job_dir):
        _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
        res = create_node(str(job_dir), "preparation")
        assert res["success"], res
        assert res["node_id"].startswith("prep_")
        assert normalize_node_type("Minimization") == "min"
        assert suggest_node_type("split") == "prep"
        assert suggest_node_type("fetch") == "source"
        assert suggest_node_type("{node_type}") is None

    def test_pending_parent_error_names_the_parent_to_run(self, job_dir):
        from mdclaw._node import validate_node_execution_context

        source = _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
        prep = create_node(str(job_dir), "prep", parent_node_ids=[source])["node_id"]
        solv = create_node(str(job_dir), "solv", parent_node_ids=[prep])["node_id"]
        ctx = validate_node_execution_context(str(job_dir), solv, "solv")
        assert ctx["success"] is False
        assert ctx["code"] == "node_execution_context_invalid"
        assert "parent_not_completed" in ctx["blocking_codes"]
        assert ctx["blocking_nodes"][0]["node_id"] == prep
        assert f"--node-id {prep}" in ctx["next_action"]
        assert ctx["dag"]["pending"] == [prep, solv]

    def test_terminal_node_error_carries_the_branch_command(self, job_dir):
        from mdclaw._node import update_node_status

        source = _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
        prep = _complete(job_dir, "prep", {"merged_pdb": "artifacts/m.pdb"},
                         parent_node_ids=[source])
        res = update_node_status(str(job_dir), prep, "running")
        assert res["code"] == "node_terminal"
        assert f"--node-type prep --parent-node-ids {source}" in res["next_action"]

    def test_manual_completion_is_redirected_to_the_stage_tool(self, job_dir):
        from mdclaw._node import update_node_status

        source = create_node(str(job_dir), "source")["node_id"]
        res = update_node_status(str(job_dir), source, "completed")
        assert res["code"] == "node_terminal_transition_reserved"
        assert "stage tool" in res["message"]
        assert f"--node-id {source}" in res["next_action"]

    def test_helper_hint_names_the_stage_tool(self):
        hint = helper_stage_hint("clean_protein", "/j", "prep_001")
        assert "prepare_complex" in hint and "--node-id prep_001" in hint
        assert helper_stage_hint("run_minimization") is None


# ---------------------------------------------------------------------------
# CLI level
# ---------------------------------------------------------------------------


class TestCliContract:
    def test_json_input_unknown_keys_are_a_structured_error(self, capsys):
        code = _run_cli(["clean_protein", "--json-input",
                         json.dumps({"pdb_file": "x.pdb", "job_dir": "/j", "fille": 1})])
        assert code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "unknown_parameter"
        assert payload["context"]["unknown_parameters"] == ["fille", "job_dir"]
        assert any("prepare_complex" in h for h in payload["hints"])
        assert any("did you mean" in h for h in payload["hints"])

    def test_node_context_on_a_helper_is_refused(self, capsys, tmp_path):
        code = _run_cli(["--job-dir", str(tmp_path), "--node-id", "prep_001",
                         "rdkit_validate_smiles", "--smiles", "CCO"])
        assert code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "node_context_not_applicable"

    def test_preflight_type_mismatch_points_at_the_right_node(self, capsys, job_dir):
        source = _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
        prep = create_node(str(job_dir), "prep", parent_node_ids=[source])["node_id"]
        code = _run_cli(["--job-dir", str(job_dir), "--node-id", source, "prepare_complex"])
        assert code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "node_type_mismatch"
        assert f"--node-id {prep} prepare_complex" in payload["next_action"]
        assert payload["dag"]["pending"] == [prep]
        assert payload["next"]["node_id"] == prep

    def test_running_a_child_before_its_parent_does_not_spend_it(self, capsys, job_dir):
        from mdclaw._node import read_node

        source = create_node(str(job_dir), "source")["node_id"]
        prep = create_node(str(job_dir), "prep", parent_node_ids=[source])["node_id"]
        code = _run_cli(["--job-dir", str(job_dir), "--node-id", prep, "prepare_complex",
                         "--solvent-type", "explicit"])
        assert code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "parent_not_completed"
        assert read_node(str(job_dir), prep)["status"] == "pending"
        assert f"--node-id {source} fetch_structure" in payload["next_action"]
        assert payload["next"]["node_id"] == source
        assert payload["next"]["blocked_node_id"] == prep

    def test_output_id_prints_only_the_node_id(self, capsys, job_dir):
        code = _run_cli(["--output", "id", "create_node", "--job-dir", str(job_dir),
                         "--node-type", "source"])
        assert code == 0
        assert capsys.readouterr().out.strip() == "source_001"

    def test_workflow_lists_stage_tools_in_order(self, capsys):
        code = _run_cli(["--workflow"])
        assert code == 0
        out = capsys.readouterr().out
        assert "source > prep > solv > topo > min > eq > prod > analyze" in out
        assert re.search(r"^  solv\s+solvate_structure.*embed_in_membrane", out, re.M)
        assert "Nodes run once" in out
        assert "bootstrap_md_workflow" in out

    def test_help_fits_one_screen(self, capsys):
        code = _run_cli(["--help"])
        assert code == 0
        out = capsys.readouterr().out
        assert len(out.splitlines()) < 45
        assert "mdclaw --workflow" in out

    def test_choice_error_replaces_the_value_error(self):
        from mdclaw.structure.clean_protein import clean_protein

        res = clean_protein(pdb_file="missing.pdb", protonation_method="pdbfixer")
        assert res["success"] is False
        assert res["code"] == "invalid_parameter_value"
        assert res["context"]["accepted_values"] == ["propka", "standard"]


class TestStageToolAudit:
    def test_every_tool_validating_a_node_type_declares_it(self):
        """A tool that cross-checks a node type at run time must be a node tool.

        ``embed_in_membrane`` lost its decorator to an inserted helper on
        2026-09-10 and the CLI stopped guarding it.
        """
        tools = _tools()
        pattern = re.compile(r'validate_node_execution_context\(\s*job_dir,\s*node_id,\s*"(\w+)"')
        for name, info in tools.items():
            try:
                source = inspect.getsource(info["fn"])
            except (OSError, TypeError):
                continue
            match = pattern.search(source)
            if match:
                assert info["node_type"] == match.group(1), (
                    f"{name} validates node type {match.group(1)!r} but declares "
                    f"{info['node_type']!r}: missing @node_tool?"
                )
