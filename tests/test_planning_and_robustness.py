"""Tests for the weak-agent robustness layer (schema v3).

Covers:
- ``plan_next`` recommendations across DAG states.
- ``create_node`` auto-parent resolution (backward compatible).
- Stable ``code`` fields on ``create_node`` failures.
- Structured JSON for CLI preflight failures (node context / missing args).
- The ``workflow_hint`` envelope appended after successful workflow tools.
"""

import json

import pytest

from mdclaw._node import create_node, plan_next, read_node, update_job_params
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


# ── plan_next ───────────────────────────────────────────────────────────────


class TestPlanNext:

    def test_empty_job_recommends_source(self, job_dir):
        plan = plan_next(str(job_dir))
        assert plan["success"] is True
        assert plan["code"] == "empty_job"
        assert plan["next_action"]["action"] == "create_source"
        assert plan["next_action"]["node_type"] == "source"
        assert plan["next_skill"] == "skills/md-prepare/SKILL.md"

    def test_after_source_recommends_prep(self, job_dir):
        _explicit_chain_through(job_dir, "source")
        plan = plan_next(str(job_dir))
        action = plan["next_action"]
        assert action["action"] == "create_and_run"
        assert action["node_type"] == "prep"
        assert action["suggested_tool"] == "prepare_complex"
        assert action["suggested_parent_node_ids"] == ["source_001"]
        assert str(job_dir) in action["create_command"]

    def test_after_topo_recommends_min(self, job_dir):
        _explicit_chain_through(job_dir, "topo")
        action = plan_next(str(job_dir))["next_action"]
        assert action["node_type"] == "min"
        assert action["suggested_tool"] == "run_minimization"
        assert action["suggested_parent_node_ids"] == ["topo_001"]

    def test_after_prod_recommends_analyze(self, job_dir):
        _explicit_chain_through(job_dir, "prod")
        action = plan_next(str(job_dir))["next_action"]
        assert action["node_type"] == "analyze"
        assert action["suggested_tool"] == "concat_trajectory"
        assert action["requires_conditions"] is True

    def test_workflow_complete_after_analyze(self, job_dir):
        ids = _explicit_chain_through(job_dir, "prod")
        _complete(job_dir, "analyze", {"combined_trajectory": "artifacts/c.dcd"},
                  parent_node_ids=[ids["prod"]],
                  conditions={"analysis_data_scope": "production_chain"})
        plan = plan_next(str(job_dir))
        assert plan["code"] == "workflow_complete"
        assert plan["next_action"]["action"] == "workflow_complete"

    def test_run_existing_when_next_node_pending(self, job_dir):
        ids = _explicit_chain_through(job_dir, "topo")
        # A pending min node already exists.
        created = create_node(str(job_dir), "min", parent_node_ids=[ids["topo"]])
        action = plan_next(str(job_dir))["next_action"]
        assert action["action"] == "run_existing"
        assert action["existing_node_id"] == created["node_id"]
        assert action["ready_to_run"] is True

    def test_wait_running_when_next_node_running(self, job_dir):
        ids = _explicit_chain_through(job_dir, "topo")
        created = create_node(str(job_dir), "min", parent_node_ids=[ids["topo"]])
        from mdclaw._node import begin_node
        begin_node(str(job_dir), created["node_id"])
        action = plan_next(str(job_dir))["next_action"]
        assert action["action"] == "wait_running"
        assert created["node_id"] in action["running_node_ids"]

    def test_inspect_failure_when_next_node_failed(self, job_dir):
        ids = _explicit_chain_through(job_dir, "topo")
        created = create_node(str(job_dir), "min", parent_node_ids=[ids["topo"]])
        from mdclaw._node import fail_node
        fail_node(str(job_dir), created["node_id"], errors=["boom"])
        action = plan_next(str(job_dir))["next_action"]
        assert action["action"] == "inspect_failure"
        assert created["node_id"] in action["failed_node_ids"]

    def test_implicit_regime_skips_solv(self, job_dir):
        update_job_params(str(job_dir), {"solvent_regime": "implicit"})
        src = _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
        _complete(job_dir, "prep", {"merged_pdb": "artifacts/m.pdb"},
                  parent_node_ids=[src])
        action = plan_next(str(job_dir))["next_action"]
        assert action["node_type"] == "topo"
        assert action["suggested_parent_node_ids"] == ["prep_001"]

    def test_membrane_regime_suggests_embed_tool(self, job_dir):
        update_job_params(str(job_dir), {"solvent_regime": "membrane"})
        src = _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
        _complete(job_dir, "prep", {"merged_pdb": "artifacts/m.pdb"},
                  parent_node_ids=[src])
        action = plan_next(str(job_dir))["next_action"]
        assert action["node_type"] == "solv"
        assert action["suggested_tool"] == "embed_in_membrane"

    def test_unknown_regime_warns_and_defaults_explicit(self, job_dir):
        src = _complete(job_dir, "source", {"source_bundle": "artifacts/sb.json"})
        _complete(job_dir, "prep", {"merged_pdb": "artifacts/m.pdb"},
                  parent_node_ids=[src])
        plan = plan_next(str(job_dir))
        assert plan["next_action"]["node_type"] == "solv"
        assert plan["warnings"], "expected a solvent_regime warning"


# ── plan_next coordination (multi-agent) ──────────────────────────────────


class TestPlanNextCoordination:

    def test_coordination_block_always_present(self, job_dir):
        # Even an empty job carries the (empty) coordination snapshot.
        plan = plan_next(str(job_dir))
        assert plan["coordination"] == {"claims": {}, "open_needs": {}}

    def test_run_existing_surfaces_active_claim(self, job_dir):
        from mdclaw._node import claim_node

        ids = _explicit_chain_through(job_dir, "topo")
        created = create_node(str(job_dir), "min", parent_node_ids=[ids["topo"]])
        claimed = claim_node(str(job_dir), created["node_id"], agent_id="agent-A")
        assert claimed["success"], claimed

        plan = plan_next(str(job_dir))
        action = plan["next_action"]
        assert action["action"] == "run_existing"
        assert action["claim"]["claimed_by"] == "agent-A"
        assert action["claim"]["active"] is True
        assert any("agent-A" in w for w in plan["warnings"])
        assert created["node_id"] in plan["coordination"]["claims"]

    def test_wait_running_surfaces_claim(self, job_dir):
        from mdclaw._node import begin_node, claim_node

        ids = _explicit_chain_through(job_dir, "topo")
        created = create_node(str(job_dir), "min", parent_node_ids=[ids["topo"]])
        claim_node(str(job_dir), created["node_id"], agent_id="agent-B")
        begin_node(str(job_dir), created["node_id"])

        action = plan_next(str(job_dir))["next_action"]
        assert action["action"] == "wait_running"
        assert action["claims"][created["node_id"]]["claimed_by"] == "agent-B"


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

    def test_create_node_emits_workflow_hint(self, tmp_path, capsys):
        from mdclaw._cli import main

        jd = tmp_path / "job_hint"
        jd.mkdir()
        # source first
        with pytest.raises(SystemExit):
            main(["create_node", "--job-dir", str(jd), "--node-type", "source"])
        capsys.readouterr()
        # plan_next-derived hint should ride along on create_node output
        with pytest.raises(SystemExit):
            main(["create_node", "--job-dir", str(jd), "--node-type", "prep",
                  "--parent-node-ids", "source_001"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is True
        assert "workflow_hint" in payload
        hint = payload["workflow_hint"]
        assert hint["action"] in {
            "run_existing", "create_and_run", "wait_running",
        }
        assert "next_command" in hint

    def test_inspect_job_emits_workflow_hint(self, tmp_path, capsys):
        from mdclaw._cli import main

        # Mirror the real stall: a completed solv chain plus a pending topo node
        # whose build_amber_system has not run yet (the P05 case where a weak
        # agent looped on inspect_job). Polling inspect_job must now hand back the
        # ready-to-run build_amber_system command so it cannot loop blind.
        jd = tmp_path / "job_inspect_hint"
        jd.mkdir()
        ids = _explicit_chain_through(jd, "solv")
        topo = create_node(str(jd), "topo", parent_node_ids=[ids["solv"]])
        assert topo["success"], topo
        with pytest.raises(SystemExit):
            main(["inspect_job", "--job-dir", str(jd)])
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is True
        assert "workflow_hint" in payload
        hint = payload["workflow_hint"]
        assert hint["suggested_tool"] == "build_amber_system"
        assert hint["next_command"]
        assert "build_amber_system" in hint["next_command"]
