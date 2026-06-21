"""Tests for the weak-agent robustness layer (schema v3).

Covers:
- ``inspect_job`` and ``explain_node`` re-entry across DAG states.
- ``create_node`` auto-parent resolution (backward compatible).
- Stable ``code`` fields on ``create_node`` failures.
- Structured JSON for CLI preflight failures (node context / missing args).
- No hidden next-step planner envelope after successful CLI tools.
"""

import json

import pytest

from mdclaw._node import (
    add_node_need,
    begin_node,
    claim_node,
    create_node,
    explain_node,
    inspect_job,
    read_node,
    update_job_params,
)
from tests.pipeline_helpers import complete_node_with_placeholders as complete_node


@pytest.fixture
def job_dir(tmp_path):
    jd = tmp_path / "job_test001"
    jd.mkdir()
    return jd


def _complete(job_dir, node_type, artifacts, **kwargs):
    """Create + complete a node, returning its id."""
    res = create_node(str(job_dir), node_type, **kwargs)
    assert res["success"], res
    complete_node(str(job_dir), res["node_id"], artifacts=artifacts)
    return res["node_id"]


def _explicit_chain_through(job_dir, last_stage):
    """Build a completed explicit-water chain up to *last_stage* inclusive."""
    update_job_params(str(job_dir), {"solvent_regime": "explicit"})
    ids = {}
    ids["source"] = _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
    if last_stage == "source":
        return ids
    ids["prep"] = _complete(job_dir, "prep", {"merged_pdb": "artifacts/m.pdb"},
                            parent_node_ids=[ids["source"]])
    if last_stage == "prep":
        return ids
    ids["solv"] = _complete(job_dir, "solv", {"solvated_pdb": "artifacts/s.pdb"},
                            parent_node_ids=[ids["prep"]])
    if last_stage == "solv":
        return ids
    ids["topo"] = _complete(
        job_dir, "topo",
        {"system_xml": "artifacts/sys.xml", "topology_pdb": "artifacts/t.pdb",
         "state_xml": "artifacts/st.xml"},
        parent_node_ids=[ids["solv"]],
    )
    if last_stage == "topo":
        return ids
    ids["min"] = _complete(job_dir, "min", {"state": "artifacts/min.xml"},
                           parent_node_ids=[ids["topo"]])
    if last_stage == "min":
        return ids
    ids["eq"] = _complete(job_dir, "eq", {"state": "artifacts/eq.xml"},
                          parent_node_ids=[ids["min"]])
    if last_stage == "eq":
        return ids
    ids["prod"] = _complete(job_dir, "prod", {"trajectory": "artifacts/p.dcd"},
                            parent_node_ids=[ids["eq"]])
    return ids


# ── inspect_job / explain_node re-entry ─────────────────────────────────────


class TestDAGReentry:

    def test_empty_job_reports_missing_progress(self, job_dir):
        summary = inspect_job(str(job_dir))
        assert summary["success"] is False
        assert summary["code"] == "progress_missing_or_invalid"

    def test_inspect_job_reports_frontier_and_params(self, job_dir):
        ids = _explicit_chain_through(job_dir, "topo")
        created = create_node(str(job_dir), "min", parent_node_ids=[ids["topo"]])
        assert created["success"], created

        summary = inspect_job(str(job_dir))
        assert summary["success"] is True
        assert summary["params"]["solvent_regime"] == "explicit"
        assert summary["pending_nodes"] == [created["node_id"]]
        assert summary["leaf_nodes"] == [created["node_id"]]
        assert summary["nodes"][created["node_id"]]["parents"] == [ids["topo"]]

    def test_inspect_job_surfaces_claims_open_needs_and_running(self, job_dir):
        ids = _explicit_chain_through(job_dir, "topo")
        created = create_node(str(job_dir), "min", parent_node_ids=[ids["topo"]])
        node_id = created["node_id"]
        claimed = claim_node(str(job_dir), node_id, agent_id="agent-A")
        assert claimed["success"], claimed
        need = add_node_need(
            str(job_dir),
            node_id,
            {
                "need_type": "human_decision",
                "query": "Confirm whether to continue this branch",
                "rationale": "The branch has alternate conditions",
            },
        )
        assert need["success"], need
        begin_node(str(job_dir), node_id)

        summary = inspect_job(str(job_dir))
        assert summary["running_nodes"] == [node_id]
        assert summary["claims"][node_id]["claimed_by"] == "agent-A"
        assert summary["open_needs"][node_id]["open_needs_count"] == 1

    def test_explain_node_reports_ready_and_resolved_inputs(self, job_dir):
        ids = _explicit_chain_through(job_dir, "topo")
        created = create_node(str(job_dir), "min", parent_node_ids=[ids["topo"]])
        node_id = created["node_id"]

        explanation = explain_node(str(job_dir), node_id)
        assert explanation["success"] is True
        assert explanation["ready_to_run"] is True
        assert explanation["parent_statuses"] == {ids["topo"]: "completed"}
        assert not explanation["missing_inputs"]
        assert explanation["resolved_inputs"]["system_xml_file"]

    def test_explain_node_blocks_on_pending_parent(self, job_dir):
        src = create_node(str(job_dir), "source")
        assert src["success"], src
        prep = create_node(str(job_dir), "prep", parent_node_ids=[src["node_id"]])
        assert prep["success"], prep

        explanation = explain_node(str(job_dir), prep["node_id"])
        assert explanation["success"] is True
        assert explanation["ready_to_run"] is False
        assert explanation["parent_statuses"] == {src["node_id"]: "pending"}
        assert explanation["validation"]["success"] is False


# ── create_node auto-parent ───────────────────────────────────────────────


class TestAutoParent:

    def test_single_candidate_auto_attaches(self, job_dir):
        src = _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
        res = create_node(str(job_dir), "prep")  # no explicit parent
        assert res["success"]
        assert res["auto_resolved_parent"] == src
        assert read_node(str(job_dir), res["node_id"])["parent_node_ids"] == [src]

    def test_no_candidate_stays_parentless(self, job_dir):
        # Legacy behavior: prep without a source is still creatable.
        res = create_node(str(job_dir), "prep")
        assert res["success"]
        assert "auto_resolved_parent" not in res
        assert read_node(str(job_dir), res["node_id"])["parent_node_ids"] == []

    def test_ambiguous_candidates_stay_parentless(self, job_dir):
        # Two completed prep leaves -> ambiguous frontier for solv; do not guess.
        _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
        _complete(job_dir, "prep", {"merged_pdb": "artifacts/a.pdb"},
                  parent_node_ids=["source_001"])
        _complete(job_dir, "prep", {"merged_pdb": "artifacts/b.pdb"},
                  parent_node_ids=["source_001"])
        res = create_node(str(job_dir), "solv")
        assert res["success"]
        assert "auto_resolved_parent" not in res
        assert read_node(str(job_dir), res["node_id"])["parent_node_ids"] == []

    def test_explicit_parent_disables_auto(self, job_dir):
        src = _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
        _complete(job_dir, "prep", {"merged_pdb": "artifacts/m.pdb"},
                  parent_node_ids=[src])
        # Explicitly parent solv from prep; auto path must not engage.
        res = create_node(str(job_dir), "solv", parent_node_ids=["prep_001"])
        assert res["success"]
        assert "auto_resolved_parent" not in res


# ── create_node stable codes ──────────────────────────────────────────────


class TestCreateNodeCodes:

    def test_invalid_node_type(self, job_dir):
        res = create_node(str(job_dir), "bogus")
        assert res["success"] is False
        assert res["code"] == "invalid_node_type"

    def test_source_already_exists(self, job_dir):
        create_node(str(job_dir), "source")
        res = create_node(str(job_dir), "source")
        assert res["success"] is False
        assert res["code"] == "source_already_exists"

    def test_continue_from_invalid_node_type(self, job_dir):
        res = create_node(str(job_dir), "eq", continue_from="prod_001")
        assert res["success"] is False
        assert res["code"] == "continue_from_invalid_node_type"

    def test_analyze_parents_mixed(self, job_dir):
        _complete(job_dir, "prod", {"trajectory": "artifacts/p.dcd"})
        _complete(job_dir, "analyze", {"combined_trajectory": "artifacts/c.dcd"},
                  parent_node_ids=["prod_001"],
                  conditions={"analysis_data_scope": "production_chain"})
        res = create_node(
            str(job_dir), "analyze",
            parent_node_ids=["prod_001", "analyze_001"],
            conditions={"analysis_data_scope": "production_chain"},
        )
        assert res["success"] is False
        assert res["code"] == "analyze_parents_mixed"

    def test_referenced_node_missing(self, job_dir):
        res = create_node(str(job_dir), "prep", parent_node_ids=["nope_999"])
        assert res["success"] is False
        assert res["code"] == "referenced_node_missing"


# ── CLI preflight JSON ─────────────────────────────────────────────────────


class TestCliPreflightJson:

    def test_node_context_required_is_json(self, capsys):
        from mdclaw._cli import main

        with pytest.raises(SystemExit) as exc:
            main(["run_minimization"])
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is False
        assert payload["code"] == "node_context_required"

    def test_missing_required_args_is_json(self, capsys):
        from mdclaw._cli import main

        # inspect_job requires --job-dir; omitting it must yield structured
        # JSON on stdout rather than an argparse stderr message.
        with pytest.raises(SystemExit) as exc:
            main(["inspect_job"])
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is False
        assert payload["code"] == "missing_required_arguments"
        assert payload["error_type"] == "ValidationError"

    def test_create_node_does_not_emit_planner_envelope(self, tmp_path, capsys):
        from mdclaw._cli import main

        jd = tmp_path / "job_hint"
        jd.mkdir()
        # source first
        with pytest.raises(SystemExit):
            main(["create_node", "--job-dir", str(jd), "--node-type", "source"])
        capsys.readouterr()
        with pytest.raises(SystemExit):
            main(["create_node", "--job-dir", str(jd), "--node-type", "prep",
                  "--parent-node-ids", "source_001"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is True
        assert "workflow" + "_hint" not in payload

    def test_inspect_job_does_not_emit_planner_envelope(self, tmp_path, capsys):
        from mdclaw._cli import main

        # Mirror the real stall: a completed solv chain plus a pending topo node
        # whose build_amber_system has not run yet. Polling inspect_job must
        # report DAG facts without inventing a next-step command.
        jd = tmp_path / "job_inspect_hint"
        jd.mkdir()
        ids = _explicit_chain_through(jd, "solv")
        topo = create_node(str(jd), "topo", parent_node_ids=[ids["solv"]])
        assert topo["success"], topo
        with pytest.raises(SystemExit):
            main(["inspect_job", "--job-dir", str(jd)])
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is True
        assert "workflow" + "_hint" not in payload
        assert payload["pending_nodes"] == [topo["node_id"]]
