"""Tests for node-based job graph management (schema v3).

Covers: _lock.py, _node.py lifecycle, node_server.py registration.
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from mdclaw._node import (
    SCHEMA_VERSION,
    add_node_need,
    begin_node,
    claim_node,
    clear_node_need,
    create_node,
    fail_node,
    find_ancestor_artifact,
    find_nodes,
    get_ancestors,
    get_children,
    init_progress_v3,
    explain_node,
    inspect_job,
    read_node,
    rebuild_progress_index,
    record_node_failure,
    record_node_need_attempt,
    release_node_claim,
    resolve_artifact,
    resolve_node_inputs,
    trace_failure,
    update_job_params,
    update_job_summaries,
    update_node,
    update_node_status,
    update_workflow_state,
    validate_node_execution_context,
    wait_node,
)
from mdclaw._node import complete_node as _real_complete_node
from mdclaw._node import _sync_progress_node_entry
from tests.pipeline_helpers import complete_node_with_placeholders as complete_node

ANALYSIS_PRODUCTION_CHAIN_CONDITIONS = {"analysis_data_scope": "production_chain"}


def _create_two_analyze_parents(job_dir: Path) -> list[str]:
    jd = str(job_dir)
    create_node(jd, "prod")
    create_node(jd, "prod")
    first = create_node(
        jd,
        "analyze",
        parent_node_ids=["prod_001"],
        conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
    )
    second = create_node(
        jd,
        "analyze",
        parent_node_ids=["prod_002"],
        conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
    )
    assert first["success"] is True
    assert second["success"] is True
    return [first["node_id"], second["node_id"]]


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def job_dir(tmp_path):
    """Create an empty job directory."""
    jd = tmp_path / "job_test001"
    jd.mkdir()
    return jd


@pytest.fixture
def job_with_prep(job_dir):
    """Job with a completed prep node."""
    result = create_node(str(job_dir), "prep")
    assert result["success"]
    complete_node(str(job_dir), result["node_id"],
                  artifacts={"merged_pdb": "artifacts/merge/merged.pdb"})
    return job_dir, result["node_id"]


# ── init_progress_v3 ──────────────────────────────────────────────────────


class TestInitProgress:

    def test_creates_progress_json(self, job_dir):
        init_progress_v3(str(job_dir), "job_test001")
        pj = job_dir / "progress.json"
        assert pj.exists()
        data = json.loads(pj.read_text())
        assert data["schema_version"] == SCHEMA_VERSION
        assert data["job_id"] == "job_test001"
        assert data["nodes"] == {}

    def test_default_job_id_from_dirname(self, job_dir):
        init_progress_v3(str(job_dir))
        data = json.loads((job_dir / "progress.json").read_text())
        assert data["job_id"] == job_dir.name

    def test_creates_parent_dirs(self, tmp_path):
        deep = tmp_path / "a" / "b" / "job_deep"
        init_progress_v3(str(deep))
        assert (deep / "progress.json").exists()


# ── create_node ────────────────────────────────────────────────────────────


class TestCreateNode:

    def test_basic_creation(self, job_dir):
        result = create_node(str(job_dir), "prep")
        assert result["success"] is True
        assert "resolves artifacts between nodes" in result["dag_guidance"]
        assert result["node_id"] == "prep_001"
        assert Path(result["node_dir"]).exists()
        assert Path(result["artifacts_dir"]).exists()

    def test_node_json_created(self, job_dir):
        result = create_node(str(job_dir), "prep")
        node_json = Path(result["node_dir"]) / "node.json"
        assert node_json.exists()
        data = json.loads(node_json.read_text())
        assert data["node_id"] == "prep_001"
        assert data["node_type"] == "prep"
        assert data["status"] == "pending"
        assert data["parent_node_ids"] == []
        assert data["dependency_node_ids"] == []
        assert data["schema_version"] == SCHEMA_VERSION

    def test_registered_in_progress(self, job_dir):
        create_node(str(job_dir), "prep")
        pj = json.loads((job_dir / "progress.json").read_text())
        assert "prep_001" in pj["nodes"]
        assert pj["nodes"]["prep_001"]["type"] == "prep"
        assert pj["nodes"]["prep_001"]["status"] == "pending"
        assert pj["nodes"]["prep_001"]["parents"] == []

    def test_sequential_ids(self, job_dir):
        r1 = create_node(str(job_dir), "eq")
        r2 = create_node(str(job_dir), "eq")
        r3 = create_node(str(job_dir), "eq")
        assert r1["node_id"] == "eq_001"
        assert r2["node_id"] == "eq_002"
        assert r3["node_id"] == "eq_003"

    def test_ids_per_type(self, job_dir):
        r_prep = create_node(str(job_dir), "prep")
        r_solv = create_node(str(job_dir), "solv")
        r_eq = create_node(str(job_dir), "eq")
        assert r_prep["node_id"] == "prep_001"
        assert r_solv["node_id"] == "solv_001"
        assert r_eq["node_id"] == "eq_001"

    def test_with_parents(self, job_with_prep):
        job_dir, prep_id = job_with_prep
        result = create_node(str(job_dir), "solv", parent_node_ids=[prep_id])
        assert result["success"]
        node = read_node(str(job_dir), result["node_id"])
        assert node["parent_node_ids"] == [prep_id]
        pj = json.loads((job_dir / "progress.json").read_text())
        assert pj["nodes"][result["node_id"]]["parents"] == [prep_id]

    def test_multi_parent(self, job_dir):
        r1 = create_node(str(job_dir), "prep")
        r2 = create_node(str(job_dir), "prep")
        r3 = create_node(str(job_dir), "topo",
                         parent_node_ids=[r1["node_id"], r2["node_id"]])
        assert r3["success"]
        node = read_node(str(job_dir), r3["node_id"])
        assert set(node["parent_node_ids"]) == {r1["node_id"], r2["node_id"]}

    def test_with_dependencies(self, job_with_prep):
        job_dir, prep_id = job_with_prep
        # Create a second prep node
        r2 = create_node(str(job_dir), "prep")
        # Solv depends on prep_001 as parent, prep_002 as dependency
        result = create_node(str(job_dir), "solv",
                             parent_node_ids=[prep_id],
                             dependency_node_ids=[r2["node_id"]])
        assert result["success"]
        node = read_node(str(job_dir), result["node_id"])
        assert node["dependency_node_ids"] == [r2["node_id"]]

    def test_with_label_and_conditions(self, job_with_prep):
        job_dir, prep_id = job_with_prep
        result = create_node(str(job_dir), "eq",
                             parent_node_ids=[prep_id],
                             label="300K",
                             conditions={"temperature_kelvin": 300.0})
        node = read_node(str(job_dir), result["node_id"])
        assert node["label"] == "300K"
        assert node["conditions"]["temperature_kelvin"] == 300.0

    def test_analyze_requires_analysis_data_scope(self, job_dir):
        create_node(str(job_dir), "prod")

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=["prod_001"],
        )

        assert result["success"] is False
        assert "analysis_data_scope" in result["error"]

    def test_analyze_rejects_legacy_chain_scope(self, job_dir):
        create_node(str(job_dir), "prod")

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=["prod_001"],
            conditions={"analysis_data_scope": "chain"},
        )

        assert result["success"] is False
        assert "analysis_data_scope" in result["error"]

    def test_analyze_accepts_production_chain_scope(self, job_dir):
        create_node(str(job_dir), "prod")

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=["prod_001"],
            conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
        )

        assert result["success"] is True
        node = read_node(str(job_dir), result["node_id"])
        assert node["conditions"]["analysis_data_scope"] == "production_chain"

    def test_analyze_accepts_segment_scope_without_subjects(self, job_dir):
        create_node(str(job_dir), "prod")

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=["prod_001"],
            conditions={"analysis_data_scope": "segment"},
        )

        assert result["success"] is True
        node = read_node(str(job_dir), result["node_id"])
        assert node["conditions"]["analysis_data_scope"] == "segment"
        assert "analysis_subjects" not in node["conditions"]

    def test_analyze_comparison_requires_subjects_and_mapping(self, job_dir):
        analyze_parents = _create_two_analyze_parents(job_dir)

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=analyze_parents,
            conditions={"analysis_data_scope": "comparison"},
        )

        assert result["success"] is False
        assert "analysis_subjects" in result["error"]

    def test_analyze_accepts_residue_number_comparison_mapping(self, job_dir):
        analyze_parents = _create_two_analyze_parents(job_dir)

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=analyze_parents,
            conditions={
                "analysis_data_scope": "comparison",
                "analysis_subjects": [
                    {"label": "chain_A", "chain_id": "A"},
                    {"label": "chain_B", "chain_id": "B"},
                ],
                "comparison_mapping": {
                    "type": "residue_number",
                    "pairs": [["chain_A:10", "chain_B:10"]],
                },
            },
        )

        assert result["success"] is True

    def test_analyze_accepts_label_only_comparison_subjects(self, job_dir):
        analyze_parents = _create_two_analyze_parents(job_dir)

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=analyze_parents,
            conditions={
                "analysis_data_scope": "comparison",
                "analysis_subjects": [
                    {"label": "apo"},
                    {"label": "holo"},
                ],
                "comparison_mapping": {
                    "type": "residue_number",
                    "pairs": [["apo:10", "holo:10"]],
                },
            },
        )

        assert result["success"] is True

    def test_analyze_treats_residue_id_as_string(self, job_dir):
        analyze_parents = _create_two_analyze_parents(job_dir)

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=analyze_parents,
            conditions={
                "analysis_data_scope": "comparison",
                "analysis_subjects": [
                    {"label": "apo"},
                    {"label": "holo"},
                ],
                "comparison_mapping": {
                    "type": "residue_number",
                    "pairs": [["apo:10A", "holo:10A"]],
                },
            },
        )

        assert result["success"] is True

    def test_analyze_rejects_duplicate_subject_labels(self, job_dir):
        analyze_parents = _create_two_analyze_parents(job_dir)

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=analyze_parents,
            conditions={
                "analysis_data_scope": "comparison",
                "analysis_subjects": [
                    {"label": "chain_A", "chain_id": "A"},
                    {"label": "chain_A", "chain_id": "B"},
                ],
                "comparison_mapping": {
                    "type": "residue_number",
                    "pairs": [["chain_A:10", "chain_A:10"]],
                },
            },
        )

        assert result["success"] is False
        assert "duplicate" in result["error"]

    def test_analyze_rejects_unknown_mapping_subject_label(self, job_dir):
        analyze_parents = _create_two_analyze_parents(job_dir)

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=analyze_parents,
            conditions={
                "analysis_data_scope": "comparison",
                "analysis_subjects": [
                    {"label": "chain_A", "chain_id": "A"},
                    {"label": "chain_B", "chain_id": "B"},
                ],
                "comparison_mapping": {
                    "type": "residue_number",
                    "pairs": [["chain_A:10", "chain_C:10"]],
                },
            },
        )

        assert result["success"] is False
        assert "unknown" in result["error"]

    def test_analyze_rejects_non_binary_comparison_subjects(self, job_dir):
        analyze_parents = _create_two_analyze_parents(job_dir)

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=analyze_parents,
            conditions={
                "analysis_data_scope": "comparison",
                "analysis_subjects": [
                    {"label": "chain_A"},
                    {"label": "chain_B"},
                    {"label": "chain_C"},
                ],
                "comparison_mapping": {
                    "type": "residue_number",
                    "pairs": [["chain_A:10", "chain_B:10"]],
                },
            },
        )

        assert result["success"] is False
        assert "binary" in result["error"]

    def test_analyze_rejects_residue_pair_missing_binary_subject(self, job_dir):
        analyze_parents = _create_two_analyze_parents(job_dir)

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=analyze_parents,
            conditions={
                "analysis_data_scope": "comparison",
                "analysis_subjects": [
                    {"label": "chain_A"},
                    {"label": "chain_B"},
                ],
                "comparison_mapping": {
                    "type": "residue_number",
                    "pairs": [["chain_A:10", "chain_A:11"]],
                },
            },
        )

        assert result["success"] is False
        assert "both binary" in result["error"]

    def test_analyze_accepts_atom_selection_comparison_mapping(self, job_dir):
        analyze_parents = _create_two_analyze_parents(job_dir)

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=analyze_parents,
            conditions={
                "analysis_data_scope": "comparison",
                "analysis_subjects": [
                    {"label": "chain_A", "chain_id": "A"},
                    {"label": "chain_B", "chain_id": "B"},
                ],
                "comparison_mapping": {
                    "type": "atom_selection",
                    "selections": {
                        "chain_A": "chainid A and name CA",
                        "chain_B": "chainid B and name CA",
                    },
                },
            },
        )

        assert result["success"] is True

    def test_analyze_rejects_atom_selection_missing_binary_subject(self, job_dir):
        analyze_parents = _create_two_analyze_parents(job_dir)

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=analyze_parents,
            conditions={
                "analysis_data_scope": "comparison",
                "analysis_subjects": [
                    {"label": "chain_A"},
                    {"label": "chain_B"},
                ],
                "comparison_mapping": {
                    "type": "atom_selection",
                    "selections": {
                        "chain_A": "chainid A and name CA",
                    },
                },
            },
        )

        assert result["success"] is False
        assert "exactly the two" in result["error"]

    def test_analyze_rejects_empty_atom_selection(self, job_dir):
        analyze_parents = _create_two_analyze_parents(job_dir)

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=analyze_parents,
            conditions={
                "analysis_data_scope": "comparison",
                "analysis_subjects": [
                    {"label": "chain_A"},
                    {"label": "chain_B"},
                ],
                "comparison_mapping": {
                    "type": "atom_selection",
                    "selections": {
                        "chain_A": "chainid A and name CA",
                        "chain_B": " ",
                    },
                },
            },
        )

        assert result["success"] is False
        assert "non-empty" in result["error"]

    def test_analyze_rejects_comparison_directly_on_prod_parent(self, job_dir):
        create_node(str(job_dir), "prod")

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=["prod_001"],
            conditions={
                "analysis_data_scope": "comparison",
                "analysis_subjects": [
                    {"label": "apo"},
                    {"label": "holo"},
                ],
                "comparison_mapping": {
                    "type": "residue_number",
                    "pairs": [["apo:10", "holo:10"]],
                },
            },
        )

        assert result["success"] is False
        assert "exactly two analyze parents" in result["error"]

    def test_analyze_rejects_mapping_without_comparison_scope(self, job_dir):
        create_node(str(job_dir), "prod")

        result = create_node(
            str(job_dir),
            "analyze",
            parent_node_ids=["prod_001"],
            conditions={
                "analysis_data_scope": "production_chain",
                "comparison_mapping": {"type": "residue_number", "pairs": []},
            },
        )

        assert result["success"] is False
        assert "comparison_mapping" in result["error"]

    def test_invalid_type(self, job_dir):
        result = create_node(str(job_dir), "invalid_type")
        assert result["success"] is False
        assert "Invalid node_type" in result["error"]

    def test_invalid_parent_ref(self, job_dir):
        result = create_node(str(job_dir), "solv",
                             parent_node_ids=["nonexistent_001"])
        assert result["success"] is False
        assert "does not exist" in result["error"]

    def test_bootstraps_progress(self, job_dir):
        """create_node creates progress.json if it doesn't exist."""
        assert not (job_dir / "progress.json").exists()
        result = create_node(str(job_dir), "prep")
        assert result["success"]
        assert (job_dir / "progress.json").exists()

    def test_rejects_legacy_progress_schema(self, job_dir):
        (job_dir / "progress.json").write_text(json.dumps({"schema_version": "2.0"}))
        with pytest.raises(ValueError, match="schema v3 only"):
            create_node(str(job_dir), "prep")


# ── Runtime execution-context validation ───────────────────────────────────


class TestValidateNodeExecutionContext:

    def test_rejects_unfinished_parent(self, job_dir):
        create_node(str(job_dir), "prep")
        result = create_node(str(job_dir), "solv", parent_node_ids=["prep_001"])
        assert result["success"]

        ctx = validate_node_execution_context(str(job_dir), "solv_001", "solv")

        assert ctx["success"] is False
        assert "parent_not_completed" in ctx["blocking_codes"]
        assert any("must be completed" in e for e in ctx["errors"])

    def test_rejects_wrong_parent_type(self, job_dir):
        create_node(str(job_dir), "prep")
        complete_node(str(job_dir), "prep_001",
                      artifacts={"merged_pdb": "artifacts/merged.pdb"})
        create_node(str(job_dir), "eq", parent_node_ids=["prep_001"])

        ctx = validate_node_execution_context(str(job_dir), "eq_001", "eq")

        assert ctx["success"] is False
        assert "parent_type_invalid" in ctx["blocking_codes"]
        # eq accepts {"topo", "min", "eq"} parents — rejection message lists
        # all as the allowed set.
        assert any("expected one of ['eq', 'min', 'topo']" in e
                   for e in ctx["errors"])

    def test_accepts_min_parent_for_eq(self, job_dir):
        create_node(str(job_dir), "topo")
        complete_node(str(job_dir), "topo_001",
                      artifacts={"system_xml": "artifacts/system.xml",
                                 "topology_pdb": "artifacts/topology.pdb",
                                 "state_xml": "artifacts/state.xml"})
        create_node(str(job_dir), "min", parent_node_ids=["topo_001"])
        complete_node(str(job_dir), "min_001",
                      artifacts={"state": "artifacts/minimized.xml",
                                 "minimized_structure": "artifacts/minimized.pdb"})
        create_node(str(job_dir), "eq", parent_node_ids=["min_001"])

        ctx = validate_node_execution_context(str(job_dir), "eq_001", "eq")

        assert ctx["success"] is True

    def test_min_rejects_wrong_parent_type(self, job_dir):
        create_node(str(job_dir), "prep")
        complete_node(str(job_dir), "prep_001",
                      artifacts={"merged_pdb": "artifacts/merged.pdb"})
        create_node(str(job_dir), "min", parent_node_ids=["prep_001"])

        ctx = validate_node_execution_context(str(job_dir), "min_001", "min")

        assert ctx["success"] is False
        assert "parent_type_invalid" in ctx["blocking_codes"]
        assert any("expected one of ['min', 'topo']" in e for e in ctx["errors"])

    def test_rejects_condition_mismatch(self, job_dir):
        create_node(str(job_dir), "topo")
        complete_node(str(job_dir), "topo_001",
                      artifacts={"system_xml": "artifacts/system.xml",
                                 "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"})
        create_node(
            str(job_dir),
            "eq",
            parent_node_ids=["topo_001"],
            conditions={"temperature_kelvin": 310.0},
        )

        ctx = validate_node_execution_context(
            str(job_dir),
            "eq_001",
            "eq",
            actual_conditions={"temperature_kelvin": 300.0},
        )

        assert ctx["success"] is False
        assert "condition_mismatch" in ctx["blocking_codes"]
        assert any("condition mismatch" in e for e in ctx["errors"])

    def test_accepts_completed_parent_and_matching_conditions(self, job_dir):
        create_node(str(job_dir), "topo")
        complete_node(str(job_dir), "topo_001",
                      artifacts={"system_xml": "artifacts/system.xml",
                                 "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"})
        create_node(
            str(job_dir),
            "eq",
            parent_node_ids=["topo_001"],
            conditions={"temperature_kelvin": 300.0},
        )

        ctx = validate_node_execution_context(
            str(job_dir),
            "eq_001",
            "eq",
            actual_conditions={"temperature_kelvin": 300.0},
        )

        assert ctx["success"] is True

    def test_rejects_declared_condition_missing_from_actual(self, job_dir):
        """Strict cross-check: a key declared on node.conditions must be
        present in the tool's actual_conditions. Silently skipping the
        check defeats the point of declaring it."""
        create_node(str(job_dir), "topo")
        complete_node(str(job_dir), "topo_001",
                      artifacts={"system_xml": "artifacts/system.xml",
                                 "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"})
        create_node(
            str(job_dir),
            "eq",
            parent_node_ids=["topo_001"],
            conditions={"temperature_kelvin": 300.0, "pressure_bar": 1.0},
        )

        ctx = validate_node_execution_context(
            str(job_dir),
            "eq_001",
            "eq",
            actual_conditions={"temperature_kelvin": 300.0},
        )

        assert ctx["success"] is False
        assert "condition_missing" in ctx["blocking_codes"]
        assert any("did not include declared condition 'pressure_bar'" in e
                   for e in ctx["errors"])

    def test_rejects_declared_condition_actual_none(self, job_dir):
        """A declared condition must be checked against a concrete runtime
        value. ``None`` is treated as unverifiable, not as a match."""
        create_node(str(job_dir), "topo")
        complete_node(str(job_dir), "topo_001",
                      artifacts={"system_xml": "artifacts/system.xml",
                                 "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"})
        create_node(
            str(job_dir),
            "eq",
            parent_node_ids=["topo_001"],
            conditions={"temperature_kelvin": 300.0, "device_index": 0},
        )

        ctx = validate_node_execution_context(
            str(job_dir),
            "eq_001",
            "eq",
            actual_conditions={"temperature_kelvin": 300.0, "device_index": None},
        )

        assert ctx["success"] is False
        assert "condition_unverifiable" in ctx["blocking_codes"]
        assert any("actual_conditions['device_index'] is None" in e
                   for e in ctx["errors"])


# ── Read-only inspection helpers ───────────────────────────────────────────


class TestReadOnlyInspection:

    def test_inspect_job_summarizes_statuses_and_leaves(self, job_dir):
        create_node(str(job_dir), "prep")
        complete_node(str(job_dir), "prep_001",
                      artifacts={"merged_pdb": "artifacts/merge/merged.pdb"})
        create_node(str(job_dir), "solv", parent_node_ids=["prep_001"])
        begin_node(str(job_dir), "solv_001")

        summary = inspect_job(str(job_dir))

        assert summary["success"] is True
        assert summary["code"] == "ok"
        assert "resolves artifacts between nodes" in summary["dag_guidance"]
        assert summary["node_count"] == 2
        assert summary["status_counts"]["completed"] == 1
        assert summary["status_counts"]["running"] == 1
        assert summary["leaf_nodes"] == ["solv_001"]
        assert summary["running_nodes"] == ["solv_001"]
        assert "prep_001" in summary["nodes"]

    def test_explain_node_returns_validation_and_inputs(self, job_dir):
        create_node(str(job_dir), "prep")
        complete_node(str(job_dir), "prep_001",
                      artifacts={"merged_pdb": "artifacts/merge/merged.pdb"})
        create_node(str(job_dir), "solv", parent_node_ids=["prep_001"])

        explanation = explain_node(str(job_dir), "solv_001")

        assert explanation["success"] is True
        assert "resolves artifacts between nodes" in explanation["dag_guidance"]
        assert explanation["node_type"] == "solv"
        assert explanation["parents"] == ["prep_001"]
        assert explanation["parent_statuses"] == {"prep_001": "completed"}
        assert explanation["validation"]["success"] is True
        assert explanation["ready_to_run"] is True
        assert explanation["resolved_inputs"]["pdb_file"].endswith(
            "prep_001/artifacts/merge/merged.pdb"
        )

    def test_explain_node_surfaces_blockers(self, job_dir):
        create_node(str(job_dir), "prep")
        create_node(str(job_dir), "solv", parent_node_ids=["prep_001"])

        explanation = explain_node(str(job_dir), "solv_001")

        assert explanation["success"] is True
        assert explanation["ready_to_run"] is False
        assert "parent_not_completed" in explanation["validation"]["blocking_codes"]
        assert explanation["missing_inputs"]

    def test_explain_node_rejects_terminal_node(self, job_dir):
        create_node(str(job_dir), "prep")
        fail_node(str(job_dir), "prep_001", errors=["bad input"])

        explanation = explain_node(str(job_dir), "prep_001")

        assert explanation["ready_to_run"] is False
        assert "node_terminal" in explanation["validation"]["blocking_codes"]


# ── complete_node strict artifact validation ──────────────────────────────


class TestCompleteNodeStrictArtifacts:
    """Covers the P1 strict guard: complete_node refuses to record str
    artifact paths whose files are missing on disk, so registration
    mistakes (e.g. wrong subdirectory) surface immediately rather than
    silently dropping the sha256 entry."""

    def test_records_artifact_sha256_for_real_file(self, job_dir):
        create_node(str(job_dir), "solv")
        node_id = "solv_001"
        artifact_file = job_dir / "nodes" / node_id / "artifacts" / "solvated.pdb"
        artifact_file.parent.mkdir(parents=True, exist_ok=True)
        artifact_file.write_text("ATOM      1  N   ALA A   1\n")
        expected_sha = hashlib.sha256(artifact_file.read_bytes()).hexdigest()

        _real_complete_node(
            str(job_dir),
            node_id,
            artifacts={"solvated_pdb": "artifacts/solvated.pdb"},
        )

        node = read_node(str(job_dir), node_id)
        assert node["metadata"]["artifact_sha256"]["solvated_pdb"] == expected_sha

    def test_raises_on_missing_artifact_file(self, job_dir):
        create_node(str(job_dir), "solv")
        with pytest.raises(ValueError, match="artifact 'solvated_pdb' file missing"):
            _real_complete_node(
                str(job_dir),
                "solv_001",
                artifacts={"solvated_pdb": "artifacts/solvated.pdb"},
            )
        node = read_node(str(job_dir), "solv_001")
        assert node["status"] == "pending"
        assert "artifact_sha256" not in node.get("metadata", {})

    def test_raises_when_artifact_path_is_directory(self, job_dir):
        create_node(str(job_dir), "solv")
        artifact_dir = job_dir / "nodes" / "solv_001" / "artifacts" / "solvated.pdb"
        artifact_dir.mkdir(parents=True)

        with pytest.raises(ValueError, match="artifact 'solvated_pdb' file missing"):
            _real_complete_node(
                str(job_dir),
                "solv_001",
                artifacts={"solvated_pdb": "artifacts/solvated.pdb"},
            )

        node = read_node(str(job_dir), "solv_001")
        assert node["status"] == "pending"

    def test_hash_failure_does_not_complete_node(self, job_dir):
        create_node(str(job_dir), "solv")
        node_id = "solv_001"
        artifact_file = job_dir / "nodes" / node_id / "artifacts" / "solvated.pdb"
        artifact_file.parent.mkdir(parents=True, exist_ok=True)
        artifact_file.write_text("ATOM      1  N   ALA A   1\n")

        with patch("mdclaw.node.lifecycle._sha256_path", return_value=None):
            with pytest.raises(ValueError, match="could not be hashed"):
                _real_complete_node(
                    str(job_dir),
                    node_id,
                    artifacts={"solvated_pdb": "artifacts/solvated.pdb"},
                )

        node = read_node(str(job_dir), node_id)
        assert node["status"] == "pending"

    def test_sync_progress_rereads_node_json_instead_of_stale_caller_data(self, job_dir):
        create_node(str(job_dir), "solv")
        node_id = "solv_001"
        stale = read_node(str(job_dir), node_id)
        latest = dict(stale)
        latest["status"] = "failed"
        latest["metadata"] = {"errors": ["newer failure"]}
        node_json = job_dir / "nodes" / node_id / "node.json"
        node_json.write_text(json.dumps(latest))

        _sync_progress_node_entry(str(job_dir), node_id, stale)

        progress = json.loads((job_dir / "progress.json").read_text())
        assert progress["nodes"][node_id]["status"] == "failed"

    def test_sync_progress_rejects_corrupt_node_json(self, job_dir):
        create_node(str(job_dir), "solv")
        node_id = "solv_001"
        stale = read_node(str(job_dir), node_id)
        node_json = job_dir / "nodes" / node_id / "node.json"
        node_json.write_text("{bad json")

        with pytest.raises(ValueError, match="Corrupt node.json"):
            _sync_progress_node_entry(str(job_dir), node_id, stale)

    def test_concurrent_complete_node_updates_progress_consistently(self, job_dir):
        node_ids = []
        for _ in range(8):
            created = create_node(str(job_dir), "prod")
            node_id = created["node_id"]
            node_ids.append(node_id)
            artifact = job_dir / "nodes" / node_id / "artifacts" / "state.xml"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(f"<State node='{node_id}'/>\n")

        def complete_one(node_id: str) -> None:
            _real_complete_node(
                str(job_dir),
                node_id,
                artifacts={"state": "artifacts/state.xml"},
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(complete_one, node_ids))

        progress = json.loads((job_dir / "progress.json").read_text())
        for node_id in node_ids:
            node = read_node(str(job_dir), node_id)
            assert node["status"] == "completed"
            assert progress["nodes"][node_id]["status"] == "completed"
            assert "state" in node["metadata"]["artifact_sha256"]

    def test_atomic_write_json_cleans_tmp_on_replace_failure(self, tmp_path, monkeypatch):
        from mdclaw import _node

        out = tmp_path / "node.json"

        def fail_replace(src, dst):
            raise OSError("replace failed")

        monkeypatch.setattr(_node.os, "replace", fail_replace)
        with pytest.raises(OSError, match="replace failed"):
            _node._atomic_write_json(out, {"status": "pending"})

        assert list(tmp_path.glob(".*.tmp.*")) == []


# ── continue_from sugar (prod extension) ───────────────────────────────────


class TestContinueFromSugar:
    """Covers create_node(..., continue_from=<prod_id>)."""

    @pytest.fixture
    def job_with_prod(self, job_dir):
        """Build source→prep→solv→topo→eq→prod_001 and return (job_dir,
        prod_001_id). prod_001 is marked completed with a checkpoint
        artifact."""
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001",
                      artifacts={"merged_pdb": "artifacts/merge/merged.pdb"})
        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(jd, "solv_001",
                      artifacts={"solvated_pdb": "artifacts/solvated.pdb"})
        create_node(jd, "topo", parent_node_ids=["solv_001"])
        complete_node(jd, "topo_001",
                      artifacts={"system_xml": "artifacts/system.xml",
                                 "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"})
        create_node(jd, "eq", parent_node_ids=["topo_001"])
        complete_node(jd, "eq_001",
                      artifacts={"checkpoint": "artifacts/equilibrated.chk"})
        create_node(jd, "prod", parent_node_ids=["eq_001"])
        complete_node(jd, "prod_001",
                      artifacts={"checkpoint": "artifacts/checkpoint.chk",
                                 "trajectory": "artifacts/trajectory.dcd"})
        return job_dir, "prod_001"

    def test_continue_from_folds_into_parent_ids(self, job_with_prod):
        jd, prod_id = job_with_prod
        result = create_node(str(jd), "prod", continue_from=prod_id)
        assert result["success"]
        node = read_node(str(jd), result["node_id"])
        assert node["parent_node_ids"] == [prod_id]

    def test_continue_from_records_audit_metadata(self, job_with_prod):
        jd, prod_id = job_with_prod
        result = create_node(str(jd), "prod", continue_from=prod_id)
        node = read_node(str(jd), result["node_id"])
        assert node["metadata"].get("continued_from") == prod_id

    def test_continue_from_resolves_restart(self, job_with_prod):
        jd, prod_id = job_with_prod
        result = create_node(str(jd), "prod", continue_from=prod_id)
        inputs = resolve_node_inputs(str(jd), result["node_id"], "prod")
        assert inputs["restart_from"].endswith(f"{prod_id}/artifacts/checkpoint.chk")

    def test_continue_from_only_allowed_for_prod(self, job_with_prod):
        jd, prod_id = job_with_prod
        result = create_node(str(jd), "eq", continue_from=prod_id)
        assert result["success"] is False
        assert "only valid for node_type='prod'" in result["error"]

    def test_continue_from_rejects_mixed_parents(self, job_with_prod):
        jd, prod_id = job_with_prod
        result = create_node(str(jd), "prod",
                             continue_from=prod_id,
                             parent_node_ids=[prod_id])
        assert result["success"] is False
        assert "mutually exclusive" in result["error"]

    def test_continue_from_rejects_non_prod_reference(self, job_with_prod):
        jd, _ = job_with_prod
        # Pointing at eq_001 (not a prod) must fail
        result = create_node(str(jd), "prod", continue_from="eq_001")
        assert result["success"] is False
        assert "must reference a prod node" in result["error"]

    def test_continue_from_rejects_unknown_reference(self, job_with_prod):
        jd, _ = job_with_prod
        result = create_node(str(jd), "prod", continue_from="prod_999")
        assert result["success"] is False
        # Unknown reference is caught by the standard parent-ref check
        assert "does not exist" in result["error"]


# ── Strict continue_from enforcement at resolve_node_inputs ────────────────


class TestContinueFromStrictEnforcement:
    """Covers runtime enforcement of node.json.metadata.continued_from.

    When a prod node was created via ``--continue-from``, the resolver
    must use *only* that ancestor's checkpoint. Silently falling through
    to another prod or to the eq ancestor would defeat the point of the
    explicit marker, so the contract is: exact hit, or ``restart_from_error``.
    """

    @pytest.fixture
    def full_dag_with_prod(self, job_dir):
        """source-less DAG: prep→solv→topo→eq→prod_001 (no checkpoint yet)."""
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001",
                      artifacts={"merged_pdb": "artifacts/merge/merged.pdb"})
        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(jd, "solv_001",
                      artifacts={"solvated_pdb": "artifacts/solvated.pdb"})
        create_node(jd, "topo", parent_node_ids=["solv_001"])
        complete_node(jd, "topo_001",
                      artifacts={"system_xml": "artifacts/system.xml",
                                 "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"})
        create_node(jd, "eq", parent_node_ids=["topo_001"])
        complete_node(jd, "eq_001",
                      artifacts={"checkpoint": "artifacts/equilibrated.chk"})
        create_node(jd, "prod", parent_node_ids=["eq_001"])
        return jd

    def test_exact_ancestor_checkpoint_is_used(self, full_dag_with_prod):
        jd = full_dag_with_prod
        complete_node(jd, "prod_001",
                      artifacts={"checkpoint": "artifacts/checkpoint.chk"})
        create_node(jd, "prod", continue_from="prod_001")

        inputs = resolve_node_inputs(jd, "prod_002", "prod")
        assert "restart_from_error" not in inputs
        assert inputs["restart_from"].endswith(
            "prod_001/artifacts/checkpoint.chk"
        )

    def test_missing_checkpoint_surfaces_error(self, full_dag_with_prod):
        """prod_001 never completed (no checkpoint artifact) → strict
        continue_from must NOT silently fall back to eq_001."""
        jd = full_dag_with_prod
        # prod_001 stays pending, no checkpoint artifact recorded
        create_node(jd, "prod", continue_from="prod_001")

        inputs = resolve_node_inputs(jd, "prod_002", "prod")
        assert "restart_from" not in inputs
        assert "restart_from_error" not in inputs
        assert "input_resolution_error" in inputs
        assert "prod_001" in inputs["input_resolution_error"]
        assert "pending" in inputs["input_resolution_error"]

    def test_does_not_pull_sibling_prod_checkpoint(self, full_dag_with_prod):
        """prod_003 is a sibling branched off eq_001 (completed, has a
        checkpoint). prod_002 is continue_from=prod_001, which has none.
        Strict enforcement must NOT scoop prod_003's checkpoint."""
        jd = full_dag_with_prod
        # Create a sibling prod on eq_001 and complete it — this should be
        # invisible to strict continue_from resolution.
        create_node(jd, "prod", parent_node_ids=["eq_001"])
        complete_node(jd, "prod_002",
                      artifacts={"checkpoint": "artifacts/checkpoint.chk"})
        # The extension the user explicitly asked for: prod_001's continuation
        create_node(jd, "prod", continue_from="prod_001")

        inputs = resolve_node_inputs(jd, "prod_003", "prod")
        assert "restart_from" not in inputs
        assert "restart_from_error" not in inputs
        assert "input_resolution_error" in inputs
        assert "prod_001" in inputs["input_resolution_error"]

    def test_plain_parent_ids_reject_unfinished_parent(self, full_dag_with_prod):
        """The default (non-continue_from) resolver also refuses to auto-resolve
        through an unfinished direct parent, even if eq has a usable checkpoint."""
        jd = full_dag_with_prod
        create_node(jd, "prod", parent_node_ids=["prod_001"])

        inputs = resolve_node_inputs(jd, "prod_002", "prod")
        assert "restart_from_error" not in inputs
        assert "restart_from" not in inputs
        assert "input_resolution_error" in inputs
        assert "prod_001" in inputs["input_resolution_error"]


# ── update_node_status tool ────────────────────────────────────────────────


class TestUpdateNodeStatusTool:
    """Covers the CLI-exposed ``update_node_status`` that keeps
    ``node.json`` and ``progress.json`` in sync."""

    def test_updates_both_files(self, job_dir):
        create_node(str(job_dir), "prep")
        result = update_node_status(str(job_dir), "prep_001", "submitted")
        assert result == {"success": True, "node_id": "prep_001",
                          "status": "queued"}

        node = read_node(str(job_dir), "prep_001")
        assert node["status"] == "queued"

        pj = json.loads((job_dir / "progress.json").read_text())
        assert pj["nodes"]["prep_001"]["status"] == "queued"

    def test_bumps_updated_at(self, job_dir):
        create_node(str(job_dir), "prep")
        before = read_node(str(job_dir), "prep_001")["updated_at"]
        # Same-second writes would match; patch updated_at to a clearly
        # older timestamp so we can observe the refresh.
        update_node(str(job_dir), "prep_001",
                    {"updated_at": "2000-01-01T00:00:00+00:00"})
        update_node_status(str(job_dir), "prep_001", "running")
        after = read_node(str(job_dir), "prep_001")["updated_at"]
        assert after != "2000-01-01T00:00:00+00:00"
        assert after >= before

    def test_node_json_and_progress_stay_consistent(self, job_dir):
        """Multiple status changes keep both stores in sync."""
        create_node(str(job_dir), "prod")
        for status in ("submitted", "running"):
            update_node_status(str(job_dir), "prod_001", status)
            node = read_node(str(job_dir), "prod_001")
            pj = json.loads((job_dir / "progress.json").read_text())
            expected = "queued" if status == "submitted" else status
            assert node["status"] == expected
            assert pj["nodes"]["prod_001"]["status"] == expected

        complete_node(
            str(job_dir),
            "prod_001",
            artifacts={"trajectory": "artifacts/trajectory.dcd"},
        )
        node = read_node(str(job_dir), "prod_001")
        pj = json.loads((job_dir / "progress.json").read_text())
        assert node["status"] == "completed"
        assert pj["nodes"]["prod_001"]["status"] == "completed"

    def test_workflow_state_rejects_direct_completion_without_mutation(self, job_dir):
        create_node(str(job_dir), "prod")

        result = update_workflow_state(
            str(job_dir),
            node_id="prod_001",
            status="completed",
        )

        assert result["success"] is False
        assert result["code"] == "node_terminal_transition_reserved"
        node = read_node(str(job_dir), "prod_001")
        pj = json.loads((job_dir / "progress.json").read_text())
        assert node["status"] == "pending"
        assert node["artifacts"] == {}
        assert pj["nodes"]["prod_001"]["status"] == "pending"

    def test_rejects_invalid_status_without_mutating_node(self, job_dir):
        create_node(str(job_dir), "prep")

        result = update_node_status(str(job_dir), "prep_001", "done")

        assert result["success"] is False
        assert result["code"] == "invalid_node_status"
        node = read_node(str(job_dir), "prep_001")
        pj = json.loads((job_dir / "progress.json").read_text())
        assert node["status"] == "pending"
        assert pj["nodes"]["prep_001"]["status"] == "pending"

    def test_unknown_node_raises(self, job_dir):
        create_node(str(job_dir), "prep")
        # update_node opens nodes/<id>/node.json unconditionally, so an
        # unknown id surfaces as FileNotFoundError — that's acceptable and
        # is the same behaviour as update_node's existing contract.
        with pytest.raises((FileNotFoundError, OSError)):
            update_node_status(str(job_dir), "prod_999", "running")

    def test_lifecycle_calls_keep_both_files_in_sync(self, job_dir):
        """begin_node / complete_node / fail_node all route through the
        same single status writer, so at every step node.json and the
        progress.json index must agree on status.

        This regression-guards against any future reintroduction of a
        two-step status write (node.json + progress.json as independent
        operations), which was the SSOT bug this refactor closed.
        """
        jd = str(job_dir)
        create_node(jd, "prep")

        def _pair() -> tuple[str, str]:
            node = read_node(jd, "prep_001")
            pj = json.loads((job_dir / "progress.json").read_text())
            return node["status"], pj["nodes"]["prep_001"]["status"]

        assert _pair() == ("pending", "pending")

        begin_node(jd, "prep_001")
        assert _pair() == ("running", "running")

        complete_node(jd, "prep_001", artifacts={"dummy": "artifacts/x.dat"})
        assert _pair() == ("completed", "completed")

        # Separately: fail_node on a fresh node
        create_node(jd, "eq")
        fail_node(jd, "eq_001", errors=["something"])
        node = read_node(jd, "eq_001")
        pj = json.loads((job_dir / "progress.json").read_text())
        assert node["status"] == "failed"
        assert pj["nodes"]["eq_001"]["status"] == "failed"


# ── update_job_params tool ────────────────────────────────────────────────


class TestUpdateJobParamsTool:

    def test_bootstraps_progress_json(self, job_dir):
        result = update_job_params(
            str(job_dir),
            {"execution_mode": "autonomous"},
        )
        assert result["success"] is True
        progress = json.loads((job_dir / "progress.json").read_text())
        assert progress["params"]["execution_mode"] == "autonomous"

    def test_merges_without_overwriting_other_params(self, job_dir):
        update_job_params(str(job_dir), {"execution_mode": "autonomous"})
        result = update_job_params(str(job_dir), {"custom_note": "keep me"})
        assert result["success"] is True
        progress = json.loads((job_dir / "progress.json").read_text())
        assert progress["params"]["execution_mode"] == "autonomous"
        assert progress["params"]["custom_note"] == "keep me"

    def test_rejects_unknown_execution_mode(self, job_dir):
        result = update_job_params(str(job_dir), {"execution_mode": "hybrid"})
        assert result["success"] is False
        assert "execution_mode must be one of" in result["error"]

    def test_rejects_legacy_progress_schema(self, job_dir):
        (job_dir / "progress.json").write_text(json.dumps({"schema_version": "2.0"}))
        with pytest.raises(ValueError, match="schema v3 only"):
            update_job_params(str(job_dir), {"execution_mode": "autonomous"})


# ── State transitions ──────────────────────────────────────────────────────


class TestStateTransitions:

    def test_begin_node(self, job_dir):
        create_node(str(job_dir), "eq")
        begin_node(str(job_dir), "eq_001")

        node = read_node(str(job_dir), "eq_001")
        assert node["status"] == "running"

        pj = json.loads((job_dir / "progress.json").read_text())
        assert pj["nodes"]["eq_001"]["status"] == "running"

    def test_complete_node(self, job_dir):
        create_node(str(job_dir), "eq")
        begin_node(str(job_dir), "eq_001")
        complete_node(str(job_dir), "eq_001",
                      artifacts={"checkpoint": "artifacts/equilibrated.chk"},
                      metadata={"platform": "CUDA"})

        node = read_node(str(job_dir), "eq_001")
        assert node["status"] == "completed"
        assert node["artifacts"]["checkpoint"] == "artifacts/equilibrated.chk"
        assert node["metadata"]["platform"] == "CUDA"

        pj = json.loads((job_dir / "progress.json").read_text())
        assert pj["nodes"]["eq_001"]["status"] == "completed"

    def test_complete_node_clears_operational_metadata_before_sealing(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "eq")
        claim_node(jd, "eq_001", "agent-a", lease_seconds=60)
        add_node_need(
            jd,
            "eq_001",
            {
                "need_type": "prod_extension",
                "query": "extend production",
                "rationale": "Additional sampling would improve confidence.",
            },
        )

        complete_node(jd, "eq_001", artifacts={"state": "artifacts/state.xml"})

        node = read_node(jd, "eq_001")
        assert node["status"] == "completed"
        assert "claimed_by" not in node["metadata"]
        assert "claim_expires_at" not in node["metadata"]
        assert "open_needs" not in node["metadata"]
        entry = json.loads((job_dir / "progress.json").read_text())["nodes"]["eq_001"]
        assert "claim" not in entry
        assert "open_needs_count" not in entry

    def test_complete_node_rejects_operational_metadata_payload(self, job_dir):
        create_node(str(job_dir), "eq")

        with pytest.raises(ValueError, match="operational fields"):
            complete_node(
                str(job_dir),
                "eq_001",
                artifacts={"state": "artifacts/state.xml"},
                metadata={"open_needs": []},
            )

    def test_complete_node_with_warnings(self, job_dir):
        create_node(str(job_dir), "solv")
        complete_node(str(job_dir), "solv_001",
                      artifacts={"solvated_pdb": "artifacts/solvated.pdb"},
                      warnings=["Low salt concentration"])

        node = read_node(str(job_dir), "solv_001")
        assert "Low salt concentration" in node["warnings"]

    def test_fail_node(self, job_dir):
        create_node(str(job_dir), "eq")
        begin_node(str(job_dir), "eq_001")
        fail_node(str(job_dir), "eq_001",
                  errors=["Simulation diverged"],
                  warnings=["High initial energy"])

        node = read_node(str(job_dir), "eq_001")
        assert node["status"] == "failed"
        assert node["metadata"]["errors"] == ["Simulation diverged"]
        assert "High initial energy" in node["warnings"]

        pj = json.loads((job_dir / "progress.json").read_text())
        assert pj["nodes"]["eq_001"]["status"] == "failed"

    def test_fail_node_records_failure_code_and_artifact_index(self, job_dir):
        create_node(str(job_dir), "eq")
        fail_node(
            str(job_dir),
            "eq_001",
            errors=["Simulation diverged"],
            code="simulation_diverged",
            failure_artifact="artifacts/failure/latest/failure_manifest.json",
        )

        node = read_node(str(job_dir), "eq_001")
        assert node["metadata"]["failure_code"] == "simulation_diverged"
        assert node["artifacts"]["failure"] == "artifacts/failure/latest/failure_manifest.json"

        pj = json.loads((job_dir / "progress.json").read_text())
        assert pj["nodes"]["eq_001"]["failure_code"] == "simulation_diverged"
        assert "failure" in pj["nodes"]["eq_001"]["artifact_keys"]

    def test_record_node_failure_writes_latest_failure_bundle(self, job_dir):
        create_node(str(job_dir), "eq")
        result = {
            "success": False,
            "code": "shake_constraint_failure",
            "message": "SHAKE failed",
            "errors": ["SHAKE failed"],
            "warnings": ["reduce timestep"],
        }

        record = record_node_failure(
            str(job_dir),
            "eq_001",
            result,
            tool="run_equilibration",
            argv=["mdclaw", "run_equilibration"],
            stderr_tail="constraint error tail",
            exit_code=1,
        )

        node = read_node(str(job_dir), "eq_001")
        assert node["status"] == "failed"
        assert node["metadata"]["failure_code"] == "shake_constraint_failure"
        manifest = job_dir / "nodes" / "eq_001" / record["failure_artifact"]
        assert manifest.is_file()
        manifest_data = json.loads(manifest.read_text())
        assert manifest_data["tool"] == "run_equilibration"
        assert manifest_data["code"] == "shake_constraint_failure"
        assert (manifest.parent / "tool_result.json").is_file()
        assert (manifest.parent / "stderr_tail.txt").read_text() == "constraint error tail"

    def test_repeated_failure_observation_does_not_mutate_failed_node(self, job_dir):
        create_node(str(job_dir), "eq")
        record_node_failure(
            str(job_dir),
            "eq_001",
            {"success": False, "code": "first", "errors": ["first failure"]},
        )
        sealed = read_node(str(job_dir), "eq_001")

        observation = record_node_failure(
            str(job_dir),
            "eq_001",
            {"success": False, "code": "later", "errors": ["later observation"]},
            stderr_tail="late stderr",
        )

        assert read_node(str(job_dir), "eq_001") == sealed
        assert "/observations/" in observation["failure_artifact"]
        assert (job_dir / "nodes" / "eq_001" / observation["failure_artifact"]).is_file()

    def test_trace_failure_returns_evidence_and_branch_option(self, job_dir):
        create_node(str(job_dir), "topo")
        complete_node(
            str(job_dir),
            "topo_001",
            artifacts={
                "system_xml": "artifacts/system.xml",
                "topology_pdb": "artifacts/topology.pdb",
                "state_xml": "artifacts/state.xml",
            },
        )
        create_node(str(job_dir), "min", parent_node_ids=["topo_001"])
        record_node_failure(
            str(job_dir),
            "min_001",
            {
                "success": False,
                "code": "nan_detected",
                "errors": ["NaN energy"],
                "warnings": [],
            },
            tool="run_minimization",
            exit_code=1,
        )

        trace = trace_failure(str(job_dir), "min_001")
        assert trace["success"] is True
        assert trace["failure_code"] == "nan_detected"
        assert trace["evidence_files"]["tool_result"].endswith("tool_result.json")
        retry_options = [
            option for option in trace["recovery_options"]
            if option["reason"] == "retry_as_new_branch"
        ]
        assert retry_options
        assert retry_options[0]["node_type"] == "min"
        assert retry_options[0]["parent_node_ids"] == ["topo_001"]

    def test_full_lifecycle(self, job_dir):
        """pending -> running -> completed."""
        create_node(str(job_dir), "prod")

        node = read_node(str(job_dir), "prod_001")
        assert node["status"] == "pending"

        begin_node(str(job_dir), "prod_001")
        node = read_node(str(job_dir), "prod_001")
        assert node["status"] == "running"

        complete_node(str(job_dir), "prod_001",
                      artifacts={"trajectory": "artifacts/trajectory.dcd"})
        node = read_node(str(job_dir), "prod_001")
        assert node["status"] == "completed"

    def test_failed_node_is_terminal(self, job_dir):
        create_node(str(job_dir), "prod")
        begin_node(str(job_dir), "prod_001")
        fail_node(str(job_dir), "prod_001",
                  errors=["transient OpenMM crash"])
        sealed = read_node(str(job_dir), "prod_001")

        with pytest.raises(ValueError, match="terminal"):
            begin_node(str(job_dir), "prod_001")
        with pytest.raises(ValueError, match="terminal"):
            complete_node(
                str(job_dir),
                "prod_001",
                artifacts={"trajectory": "artifacts/trajectory.dcd"},
            )

        assert read_node(str(job_dir), "prod_001") == sealed

    def test_failure_clears_operational_metadata_before_sealing(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "eq")
        claim_node(jd, "eq_001", "agent-a", lease_seconds=60)
        add_node_need(
            jd,
            "eq_001",
            {
                "need_type": "repair",
                "query": "repair the failed setup",
                "rationale": "The current attempt cannot continue.",
            },
        )

        fail_node(jd, "eq_001", errors=["runtime failed"])

        metadata = read_node(jd, "eq_001").get("metadata", {})
        assert "claimed_by" not in metadata
        assert "claim_expires_at" not in metadata
        assert "open_needs" not in metadata


# ── update_node / update_node_status ───────────────────────────────────────


class TestUpdateNode:

    def test_update_node_status_reserves_failed_transition_for_failure_recorder(
        self, job_dir
    ):
        create_node(str(job_dir), "prep")

        result = update_node_status(str(job_dir), "prep_001", "failed")

        assert result["success"] is False
        assert result["code"] == "node_terminal_transition_reserved"
        assert read_node(str(job_dir), "prep_001")["status"] == "pending"

    def test_merge_dict(self, job_dir):
        create_node(str(job_dir), "prep")
        update_node(str(job_dir), "prep_001", {
            "artifacts": {"merged_pdb": "artifacts/merged.pdb"}
        })
        update_node(str(job_dir), "prep_001", {
            "artifacts": {"ligand_chemistry": "artifacts/ligand_chemistry.json"}
        })
        node = read_node(str(job_dir), "prep_001")
        assert node["artifacts"]["merged_pdb"] == "artifacts/merged.pdb"
        assert node["artifacts"]["ligand_chemistry"] == "artifacts/ligand_chemistry.json"

    def test_append_warnings(self, job_dir):
        create_node(str(job_dir), "prep")
        update_node(str(job_dir), "prep_001", {"warnings": ["w1"]})
        update_node(str(job_dir), "prep_001", {"warnings": ["w2"]})
        node = read_node(str(job_dir), "prep_001")
        assert node["warnings"] == ["w1", "w2"]

    def test_overwrite_scalar(self, job_dir):
        """Generic scalar merges still work for non-status fields."""
        create_node(str(job_dir), "prep")
        update_node(str(job_dir), "prep_001", {"label": "reheat"})
        node = read_node(str(job_dir), "prep_001")
        assert node["label"] == "reheat"

    def test_update_node_refuses_identity_edits(self, job_dir):
        create_node(str(job_dir), "prep")
        with pytest.raises(ValueError, match="immutable node identity"):
            update_node(str(job_dir), "prep_001", {
                "conditions": {"temperature_kelvin": 300.0}
            })

    def test_update_node_refuses_status_edits(self, job_dir):
        """update_node must not write the ``status`` field — that is
        routed through update_node_status so the progress.json index
        can never drift from node.json."""
        create_node(str(job_dir), "prep")
        with pytest.raises(ValueError, match="status"):
            update_node(str(job_dir), "prep_001", {"status": "running"})

    def test_update_node_refuses_completed_node_edits(self, job_dir):
        create_node(str(job_dir), "prep")
        complete_node(
            str(job_dir),
            "prep_001",
            artifacts={"merged_pdb": "artifacts/merged.pdb"},
        )

        with pytest.raises(ValueError, match="sealed"):
            update_node(str(job_dir), "prep_001", {
                "metadata": {"post_completion_note": "late note"}
            })

    def test_update_node_status_refuses_completed_node_edits(self, job_dir):
        create_node(str(job_dir), "prep")
        complete_node(
            str(job_dir),
            "prep_001",
            artifacts={"merged_pdb": "artifacts/merged.pdb"},
        )

        result = update_node_status(str(job_dir), "prep_001", "failed")

        assert result["success"] is False
        assert result["code"] == "node_terminal"
        assert read_node(str(job_dir), "prep_001")["status"] == "completed"

    def test_update_node_refuses_failed_node_edits(self, job_dir):
        create_node(str(job_dir), "prep")
        fail_node(str(job_dir), "prep_001", errors=["bad input"])

        with pytest.raises(ValueError, match="terminal"):
            update_node(str(job_dir), "prep_001", {"metadata": {"late": True}})

        result = update_node_status(str(job_dir), "prep_001", "running")
        assert result["success"] is False
        assert result["code"] == "node_terminal"


class TestWaitNode:

    def test_wait_node_returns_completed_terminal_status(self, job_dir):
        create_node(str(job_dir), "solv")
        complete_node(
            str(job_dir),
            "solv_001",
            artifacts={"solvated_pdb": "artifacts/membrane.pdb"},
        )

        result = wait_node(str(job_dir), "solv_001", timeout_seconds=0)

        assert result["success"] is True
        assert result["code"] == "node_terminal"
        assert result["status"] == "completed"
        assert result["node_completed"] is True
        assert result["node_failed"] is False

    def test_wait_node_times_out_on_running_node(self, job_dir):
        create_node(str(job_dir), "solv")
        begin_node(str(job_dir), "solv_001")

        result = wait_node(
            str(job_dir),
            "solv_001",
            timeout_seconds=0,
            poll_interval_seconds=0.1,
        )

        assert result["success"] is False
        assert result["code"] == "node_wait_timeout"
        assert result["status"] == "running"
        assert "do not package final outputs" in result["errors"][0]


# ── update_job_summaries ───────────────────────────────────────────────────


class TestUpdateJobSummaries:

    def test_update_system(self, job_dir):
        init_progress_v3(str(job_dir))
        update_job_summaries(str(job_dir), system={"pdb_id": "1AKE", "chains": ["A"]})
        pj = json.loads((job_dir / "progress.json").read_text())
        assert pj["system"]["pdb_id"] == "1AKE"

    def test_merge_params(self, job_dir):
        init_progress_v3(str(job_dir))
        update_job_summaries(str(job_dir), params={"water_model": "opc"})
        update_job_summaries(str(job_dir), params={"forcefield": "ff19SB"})
        pj = json.loads((job_dir / "progress.json").read_text())
        assert pj["params"]["water_model"] == "opc"
        assert pj["params"]["forcefield"] == "ff19SB"


# ── Read helpers ───────────────────────────────────────────────────────────


class TestReadHelpers:

    def test_find_nodes_all(self, job_dir):
        create_node(str(job_dir), "prep")
        create_node(str(job_dir), "solv")
        create_node(str(job_dir), "eq")
        nodes = find_nodes(str(job_dir))
        assert len(nodes) == 3

    def test_find_nodes_by_type(self, job_dir):
        create_node(str(job_dir), "prep")
        create_node(str(job_dir), "eq")
        create_node(str(job_dir), "eq")
        nodes = find_nodes(str(job_dir), node_type="eq")
        assert len(nodes) == 2
        assert all(n["type"] == "eq" for n in nodes.values())

    def test_find_nodes_by_status(self, job_dir):
        create_node(str(job_dir), "prep")
        create_node(str(job_dir), "eq")
        complete_node(str(job_dir), "prep_001",
                      artifacts={"merged_pdb": "artifacts/merged.pdb"})
        completed = find_nodes(str(job_dir), status="completed")
        assert len(completed) == 1
        assert "prep_001" in completed

    def test_find_nodes_empty(self, tmp_path):
        assert find_nodes(str(tmp_path)) == {}

    def test_get_ancestors_linear(self, job_dir):
        create_node(str(job_dir), "prep")
        create_node(str(job_dir), "solv", parent_node_ids=["prep_001"])
        create_node(str(job_dir), "topo", parent_node_ids=["solv_001"])
        ancestors = get_ancestors(str(job_dir), "topo_001")
        assert ancestors == ["topo_001", "solv_001", "prep_001"]

    def test_get_ancestors_multi_parent(self, job_dir):
        create_node(str(job_dir), "prep")
        create_node(str(job_dir), "prep")
        create_node(str(job_dir), "topo",
                     parent_node_ids=["prep_001", "prep_002"])
        ancestors = get_ancestors(str(job_dir), "topo_001")
        assert "topo_001" in ancestors
        assert "prep_001" in ancestors
        assert "prep_002" in ancestors

    def test_get_children(self, job_dir):
        create_node(str(job_dir), "eq")
        create_node(str(job_dir), "prod", parent_node_ids=["eq_001"])
        create_node(str(job_dir), "prod", parent_node_ids=["eq_001"])
        children = get_children(str(job_dir), "eq_001")
        assert set(children) == {"prod_001", "prod_002"}

    def test_get_children_none(self, job_dir):
        create_node(str(job_dir), "prep")
        assert get_children(str(job_dir), "prep_001") == []

    def test_resolve_artifact(self, job_dir):
        create_node(str(job_dir), "eq")
        path = resolve_artifact(str(job_dir), "eq_001", "artifacts/equilibrated.chk")
        expected = job_dir / "nodes" / "eq_001" / "artifacts" / "equilibrated.chk"
        assert path == expected.resolve()


# ── Multi-agent global index helpers ───────────────────────────────────────


class TestProgressIndexRebuild:

    def test_rebuild_progress_index_from_node_json(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prep", label="protein_only")
        complete_node(
            jd,
            "prep_001",
            artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
            metadata={"producer_agent": "agent-a"},
        )
        create_node(jd, "eq", label="needs_more_sampling")
        add_node_need(
            jd,
            "eq_001",
            {
                "need_type": "prod_extension",
                "query": "extend production by 100 ns",
                "rationale": "RMSD has not converged yet.",
                "preferred_node_type": "prod",
            },
        )

        progress_path = job_dir / "progress.json"
        progress = json.loads(progress_path.read_text())
        progress["nodes"] = {"stale_999": {"type": "prep", "status": "failed"}}
        progress_path.write_text(json.dumps(progress))

        result = rebuild_progress_index(jd)

        assert result["success"] is True
        rebuilt = json.loads(progress_path.read_text())["nodes"]
        assert set(rebuilt) == {"prep_001", "eq_001"}
        assert rebuilt["prep_001"]["type"] == "prep"
        assert rebuilt["prep_001"]["status"] == "completed"
        assert rebuilt["prep_001"]["label"] == "protein_only"
        assert rebuilt["prep_001"]["producer_agent"] == "agent-a"
        assert rebuilt["prep_001"]["artifact_keys"] == ["merged_pdb"]
        assert "open_needs_count" not in rebuilt["prep_001"]
        assert rebuilt["eq_001"]["status"] == "pending"
        assert rebuilt["eq_001"]["open_needs_count"] == 1
        assert rebuilt["eq_001"]["open_need_types"] == ["prod_extension"]

    def test_rebuild_progress_index_warns_on_unreadable_node(self, job_dir):
        create_node(str(job_dir), "prep")
        bad_dir = job_dir / "nodes" / "bad_001"
        bad_dir.mkdir(parents=True)
        (bad_dir / "node.json").write_text("{not json")

        result = rebuild_progress_index(str(job_dir))

        assert result["success"] is True
        assert any("unreadable node.json" in w for w in result["warnings"])
        progress = json.loads((job_dir / "progress.json").read_text())
        assert "prep_001" in progress["nodes"]
        assert "bad_001" not in progress["nodes"]


class TestNodeClaim:

    def test_claim_node_sets_metadata_and_progress_summary(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prod")

        result = claim_node(jd, "prod_001", "agent-a", lease_seconds=60)

        assert result["success"] is True
        node = read_node(jd, "prod_001")
        assert node["metadata"]["claimed_by"] == "agent-a"
        assert node["metadata"]["claim_expires_at"] == result["claim_expires_at"]
        progress = json.loads((job_dir / "progress.json").read_text())
        assert progress["nodes"]["prod_001"]["claim"]["claimed_by"] == "agent-a"

    def test_claim_node_rejects_active_other_agent_claim(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prod")
        claim_node(jd, "prod_001", "agent-a", lease_seconds=60)

        result = claim_node(jd, "prod_001", "agent-b", lease_seconds=60)

        assert result["success"] is False
        assert result["code"] == "node_already_claimed"
        assert result["claimed_by"] == "agent-a"

    def test_claim_node_allows_expired_claim_override(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prod")
        expired = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        update_node(
            jd,
            "prod_001",
            {
                "metadata": {
                    "claimed_by": "agent-a",
                    "claim_expires_at": expired,
                }
            },
        )

        result = claim_node(jd, "prod_001", "agent-b", lease_seconds=60)

        assert result["success"] is True
        node = read_node(jd, "prod_001")
        assert node["metadata"]["claimed_by"] == "agent-b"

    def test_claim_node_rejects_invalid_existing_expiry(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prod")
        update_node(
            jd,
            "prod_001",
            {
                "metadata": {
                    "claimed_by": "agent-a",
                    "claim_expires_at": "not-a-date",
                }
            },
        )

        result = claim_node(jd, "prod_001", "agent-b", lease_seconds=60)

        assert result["success"] is False
        assert result["code"] == "invalid_claim_expiry"
        node = read_node(jd, "prod_001")
        assert node["metadata"]["claimed_by"] == "agent-a"

    def test_release_node_claim_removes_metadata_and_progress_summary(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prod")
        claim_node(jd, "prod_001", "agent-a", lease_seconds=60)

        result = release_node_claim(jd, "prod_001", agent_id="agent-a")

        assert result["success"] is True
        node = read_node(jd, "prod_001")
        assert "claimed_by" not in node["metadata"]
        assert "claim_expires_at" not in node["metadata"]
        progress = json.loads((job_dir / "progress.json").read_text())
        assert "claim" not in progress["nodes"]["prod_001"]

    def test_release_node_claim_rejects_wrong_owner(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prod")
        claim_node(jd, "prod_001", "agent-a", lease_seconds=60)

        result = release_node_claim(jd, "prod_001", agent_id="agent-b")

        assert result["success"] is False
        assert result["code"] == "claim_owner_mismatch"

    def test_claim_node_rejects_completed_node(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prod")
        complete_node(jd, "prod_001", artifacts={"trajectory": "artifacts/t.dcd"})

        result = claim_node(jd, "prod_001", "agent-a", lease_seconds=60)

        assert result["success"] is False
        assert result["code"] == "node_terminal"

    def test_claim_node_rejects_failed_node(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prod")
        fail_node(jd, "prod_001", errors=["runtime failed"])

        result = claim_node(jd, "prod_001", "agent-a", lease_seconds=60)

        assert result["success"] is False
        assert result["code"] == "node_terminal"


class TestNodeNeeds:

    def test_add_node_need_updates_metadata_and_progress_summary(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "eq")

        result = add_node_need(
            jd,
            "eq_001",
            {
                "need_type": "prod_extension",
                "query": "extend production by 100 ns",
                "rationale": "RMSD has not converged yet.",
                "preferred_node_type": "prod",
                "max_variants": 2,
            },
        )

        assert result["success"] is True
        node = read_node(jd, "eq_001")
        assert node["metadata"]["open_needs"][0]["need_type"] == "prod_extension"
        assert node["metadata"]["open_needs"][0]["attempts"] == []
        progress = json.loads((job_dir / "progress.json").read_text())
        entry = progress["nodes"]["eq_001"]
        assert entry["open_needs_count"] == 1
        assert entry["open_need_types"] == ["prod_extension"]

    def test_record_node_need_attempt_updates_metadata_and_progress(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "eq")
        add_node_need(
            jd,
            "eq_001",
            {
                "need_type": "prod_extension",
                "query": "extend production by 100 ns",
                "rationale": "RMSD has not converged yet.",
                "preferred_node_type": "prod",
            },
        )

        result = record_node_need_attempt(
            jd,
            "eq_001",
            0,
            {
                "node_id": "prod_002",
                "agent_id": "agent-b",
                "status": "completed",
            },
        )

        assert result["success"] is True
        assert result["attempt_index"] == 0
        node = read_node(jd, "eq_001")
        attempt = node["metadata"]["open_needs"][0]["attempts"][0]
        assert attempt["node_id"] == "prod_002"
        assert attempt["agent_id"] == "agent-b"
        assert attempt["status"] == "completed"
        progress = json.loads((job_dir / "progress.json").read_text())
        entry = progress["nodes"]["eq_001"]
        assert entry["open_need_attempts_count"] == 1
        assert entry["attempted_node_ids"] == ["prod_002"]

    def test_record_node_need_attempt_rejects_invalid_attempt(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "eq")
        add_node_need(
            jd,
            "eq_001",
            {
                "need_type": "prod_extension",
                "query": "extend production",
                "rationale": "Additional sampling would improve confidence.",
            },
        )

        result = record_node_need_attempt(jd, "eq_001", 0, {"agent_id": "agent-b"})

        assert result["success"] is False
        assert result["code"] == "invalid_need_attempt"

    def test_clear_node_need_removes_one_or_all_needs(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "eq")
        for need_type in ("prod_extension", "replicate"):
            add_node_need(
                jd,
                "eq_001",
                {
                    "need_type": need_type,
                    "query": f"{need_type} request",
                    "rationale": "Additional sampling would improve confidence.",
                },
            )

        one = clear_node_need(jd, "eq_001", need_index=0)
        assert one["success"] is True
        assert one["remaining_open_needs"] == 1
        node = read_node(jd, "eq_001")
        assert node["metadata"]["open_needs"][0]["need_type"] == "replicate"

        all_needs = clear_node_need(jd, "eq_001")
        assert all_needs["success"] is True
        assert all_needs["cleared"] == 1
        progress = json.loads((job_dir / "progress.json").read_text())
        assert "open_needs_count" not in progress["nodes"]["eq_001"]

    def test_add_node_need_rejects_invalid_need(self, job_dir):
        create_node(str(job_dir), "eq")

        result = add_node_need(str(job_dir), "eq_001", {"query": "missing type"})

        assert result["success"] is False
        assert result["code"] == "invalid_need"

    def test_add_node_need_rejects_completed_node(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "eq")
        complete_node(jd, "eq_001", artifacts={"state": "artifacts/state.xml"})

        result = add_node_need(
            jd,
            "eq_001",
            {
                "need_type": "prod_extension",
                "query": "extend production",
                "rationale": "Additional sampling would improve confidence.",
            },
        )

        assert result["success"] is False
        assert result["code"] == "node_terminal"

    def test_add_node_need_rejects_failed_node(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "eq")
        fail_node(jd, "eq_001", errors=["runtime failed"])

        result = add_node_need(
            jd,
            "eq_001",
            {
                "need_type": "prod_extension",
                "query": "extend production",
                "rationale": "Additional sampling would improve confidence.",
            },
        )

        assert result["success"] is False
        assert result["code"] == "node_terminal"


# ── DAG auto-resolve ──────────────────────────────────────────────────────


class TestDAGAutoResolve:
    """Test find_ancestor_artifact and resolve_node_inputs."""

    @pytest.fixture
    def full_dag(self, job_dir):
        """Build a complete prep->solv->topo->eq->prod DAG with artifacts."""
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001",
                      artifacts={"merged_pdb": "artifacts/merge/merged.pdb"})

        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(jd, "solv_001",
                      artifacts={"solvated_pdb": "artifacts/solvated.pdb",
                                 "box_dimensions": "artifacts/box_dimensions.json"})

        create_node(jd, "topo", parent_node_ids=["solv_001"])
        complete_node(jd, "topo_001",
                      artifacts={"system_xml": "artifacts/system.xml",
                                 "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"})

        create_node(jd, "eq", parent_node_ids=["topo_001"])
        complete_node(jd, "eq_001",
                      artifacts={"checkpoint": "artifacts/equilibrated.chk",
                                 "final_structure": "artifacts/equilibrated.pdb"})

        create_node(jd, "prod", parent_node_ids=["eq_001"])
        return job_dir

    def test_find_ancestor_system_xml_from_eq(self, full_dag):
        jd = str(full_dag)
        result = find_ancestor_artifact(jd, "eq_001", "topo", "system_xml")
        assert result is not None
        assert result.endswith("topo_001/artifacts/system.xml")

    def test_find_ancestor_checkpoint_from_prod(self, full_dag):
        jd = str(full_dag)
        result = find_ancestor_artifact(jd, "prod_001", "eq", "checkpoint")
        assert result is not None
        assert result.endswith("eq_001/artifacts/equilibrated.chk")

    def test_find_ancestor_merged_from_solv(self, full_dag):
        jd = str(full_dag)
        result = find_ancestor_artifact(jd, "solv_001", "prep", "merged_pdb")
        assert result is not None
        assert result.endswith("prep_001/artifacts/merge/merged.pdb")

    def test_find_ancestor_skips_intermediate(self, full_dag):
        """prod_001 -> eq_001 -> topo_001: the topo's system_xml is 2 hops away."""
        jd = str(full_dag)
        result = find_ancestor_artifact(jd, "prod_001", "topo", "system_xml")
        assert result is not None
        assert "topo_001" in result

    def test_find_ancestor_missing_returns_none(self, full_dag):
        jd = str(full_dag)
        result = find_ancestor_artifact(jd, "prep_001", "topo", "system_xml")
        assert result is None

    def test_resolve_node_inputs_blocks_pending_parent(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prep")
        create_node(jd, "solv", parent_node_ids=["prep_001"])

        inputs = resolve_node_inputs(jd, "solv_001", "solv")

        assert "pdb_file" not in inputs
        assert "input_resolution_error" in inputs
        assert "prep_001" in inputs["input_resolution_error"]
        assert "pending" in inputs["input_resolution_error"]

    def test_resolve_node_inputs_does_not_skip_nearest_completed_prep_missing_artifact(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(
            jd,
            "prep_001",
            artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
        )
        create_node(jd, "prep", parent_node_ids=["prep_001"])
        complete_node(
            jd,
            "prep_002",
            artifacts={"audit": "artifacts/audit.json"},
        )
        create_node(jd, "solv", parent_node_ids=["prep_002"])

        inputs = resolve_node_inputs(jd, "solv_001", "solv")

        assert "pdb_file" not in inputs
        assert "input_resolution_error" in inputs
        assert "prep_002" in inputs["input_resolution_error"]
        assert "merged_pdb" in inputs["input_resolution_error"]

    def test_resolve_node_inputs_blocks_failed_parent_with_artifacts(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "topo")
        update_node(
            jd,
            "topo_001",
            {
                "artifacts": {
                    "system_xml": "artifacts/system.xml",
                    "topology_pdb": "artifacts/topology.pdb",
                    "state_xml": "artifacts/state.xml",
                }
            },
        )
        fail_node(jd, "topo_001", errors=["topology invalid"])
        create_node(jd, "eq", parent_node_ids=["topo_001"])

        inputs = resolve_node_inputs(jd, "eq_001", "eq")

        assert "system_xml_file" not in inputs
        assert "topology_pdb_file" not in inputs
        assert "input_resolution_error" in inputs
        assert "topo_001" in inputs["input_resolution_error"]
        assert "failed" in inputs["input_resolution_error"]

    def test_resolve_node_inputs_blocks_pending_dependency(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001",
                      artifacts={"merged_pdb": "artifacts/merge/merged.pdb"})
        create_node(jd, "topo")
        create_node(
            jd,
            "solv",
            parent_node_ids=["prep_001"],
            dependency_node_ids=["topo_001"],
        )

        inputs = resolve_node_inputs(jd, "solv_001", "solv")

        assert "pdb_file" not in inputs
        assert "input_resolution_error" in inputs
        assert "topo_001" in inputs["input_resolution_error"]
        assert "Dependency node" in inputs["input_resolution_error"]

    def test_resolve_node_inputs_eq(self, full_dag):
        jd = str(full_dag)
        inputs = resolve_node_inputs(jd, "eq_001", "eq")
        assert "system_xml_file" in inputs
        assert "topology_pdb_file" in inputs
        assert inputs["system_xml_file"].endswith("system.xml")
        assert inputs["topology_pdb_file"].endswith("topology.pdb")
        # The first eq node from topo has no eq/prod ancestor, so no
        # restart source is surfaced — it runs from the topo state.xml
        # as a fresh equilibration.
        assert "restart_from" not in inputs

    def test_resolve_node_inputs_min(self, full_dag):
        jd = str(full_dag)
        create_node(jd, "min", parent_node_ids=["topo_001"])

        inputs = resolve_node_inputs(jd, "min_001", "min")

        assert inputs["system_xml_file"].endswith("topo_001/artifacts/system.xml")
        assert inputs["topology_pdb_file"].endswith(
            "topo_001/artifacts/topology.pdb"
        )
        assert inputs["state_xml_file"].endswith("topo_001/artifacts/state.xml")

    def test_resolve_node_inputs_eq_uses_min_state(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001", artifacts={"merged_pdb": "x.pdb"})
        create_node(jd, "topo", parent_node_ids=["prep_001"])
        complete_node(
            jd, "topo_001",
            artifacts={"system_xml": "artifacts/system.xml",
                       "topology_pdb": "artifacts/topology.pdb",
                       "state_xml": "artifacts/state.xml"},
        )
        create_node(jd, "min", parent_node_ids=["topo_001"])
        complete_node(
            jd, "min_001",
            artifacts={"state": "artifacts/minimized.xml",
                       "minimized_structure": "artifacts/minimized_structure.pdb",
                       "minimization_report": "artifacts/minimization_report.json"},
            metadata={"final_step": 0},
        )
        create_node(jd, "eq", parent_node_ids=["min_001"])

        inputs = resolve_node_inputs(jd, "eq_001", "eq")

        assert inputs["system_xml_file"].endswith("topo_001/artifacts/system.xml")
        assert inputs["restart_from"].endswith("min_001/artifacts/minimized.xml")
        assert inputs["restart_from_node_id"] == "min_001"
        assert inputs["restart_from_node_type"] == "min"

    def test_resolve_node_inputs_eq_chain_uses_prior_eq_state(self, full_dag):
        """eq → eq chaining: the second eq node restarts from the first
        eq's saved state (XML preferred over chk). This enables NPT →
        NVT → NPT multi-stage equilibration as a sequence of eq nodes,
        which is the user-facing entry point for free ensemble
        chaining."""
        import json
        jd = str(full_dag)
        # full_dag completes eq_001 with the legacy chk-only artifact set;
        # extend it with the portable XML state artifact so resolve_node_inputs
        # picks the XML path.
        eq1_path = full_dag / "nodes" / "eq_001" / "node.json"
        eq1 = json.loads(eq1_path.read_text())
        eq1["artifacts"]["state"] = "artifacts/equilibrated.xml"
        eq1_path.write_text(json.dumps(eq1))

        create_node(jd, "eq", parent_node_ids=["eq_001"])
        inputs = resolve_node_inputs(jd, "eq_002", "eq")
        assert "restart_from" in inputs
        assert inputs["restart_from"].endswith("equilibrated.xml"), (
            "eq → eq chaining must surface the XML state of the parent eq "
            "so the new eq node can resume from it (cross-ensemble safe)"
        )
        # Topology still resolves from the shared topo ancestor.
        assert inputs["system_xml_file"].endswith("topo_001/artifacts/system.xml")

    def test_resolve_node_inputs_prod(self, full_dag):
        jd = str(full_dag)
        inputs = resolve_node_inputs(jd, "prod_001", "prod")
        assert "system_xml_file" in inputs
        assert "topology_pdb_file" in inputs
        assert "restart_from" in inputs
        assert inputs["restart_from"].endswith("equilibrated.chk")

    def test_resolve_node_inputs_prod_extension(self, full_dag):
        """prod with a prod parent restarts from the prod parent's checkpoint,
        not from the eq ancestor — this is the extension-run case."""
        jd = str(full_dag)
        complete_node(jd, "prod_001",
                      artifacts={"checkpoint": "artifacts/checkpoint.chk",
                                 "trajectory": "artifacts/trajectory.dcd"})
        create_node(jd, "prod", parent_node_ids=["prod_001"])

        inputs = resolve_node_inputs(jd, "prod_002", "prod")
        assert "restart_from" in inputs
        assert inputs["restart_from"].endswith("prod_001/artifacts/checkpoint.chk")
        # topo inputs still resolve to the shared topo ancestor
        assert inputs["system_xml_file"].endswith("topo_001/artifacts/system.xml")

    def test_resolve_node_inputs_prod_chain_blocks_unfinished_parent(self, full_dag):
        """Deep chain prod_003 → prod_002 → prod_001 → eq_001: unfinished
        direct parents block auto-resolution rather than being skipped."""
        jd = str(full_dag)
        # prod_001 is the only prod with a saved checkpoint
        complete_node(jd, "prod_001",
                      artifacts={"checkpoint": "artifacts/checkpoint.chk",
                                 "trajectory": "artifacts/trajectory.dcd"})
        # prod_002 exists but never completed — no checkpoint artifact
        create_node(jd, "prod", parent_node_ids=["prod_001"])
        # prod_003 continues; prod_002 is nearest but unfinished, so resolver
        # must not skip it and auto-restart from an older ancestor.
        create_node(jd, "prod", parent_node_ids=["prod_002"])

        inputs = resolve_node_inputs(jd, "prod_003", "prod")
        assert "restart_from" not in inputs
        assert "input_resolution_error" in inputs
        assert "prod_002" in inputs["input_resolution_error"]

    def test_resolve_node_inputs_prod_falls_back_to_eq(self, full_dag):
        """With no intermediate prod ancestor on the path, resolve falls back
        to the eq ancestor's checkpoint (legacy eq→prod case)."""
        jd = str(full_dag)
        # No prod_001 artifacts registered; prod_001 exists from full_dag but
        # has no checkpoint key. The direct parent of prod_001 is eq_001.
        inputs = resolve_node_inputs(jd, "prod_001", "prod")
        assert inputs["restart_from"].endswith("equilibrated.chk")

    def test_resolve_node_inputs_prod_prefers_state_over_checkpoint(
        self, job_dir
    ):
        """When the eq ancestor has *both* ``state`` (XML) and
        ``checkpoint`` (binary) artifacts, resolve_node_inputs returns
        the state path. Binary checkpoints are GPU-architecture-
        specific and silently corrupt on cross-node handoff, so the
        XML state is the correct default."""
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001", artifacts={"merged_pdb": "x.pdb"})
        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(jd, "solv_001",
                      artifacts={"solvated_pdb": "x.pdb",
                                 "box_dimensions": "x.json"})
        create_node(jd, "topo", parent_node_ids=["solv_001"])
        complete_node(jd, "topo_001",
                      artifacts={"system_xml": "artifacts/system.xml",
                                 "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"})
        create_node(jd, "eq", parent_node_ids=["topo_001"])
        complete_node(jd, "eq_001",
                      artifacts={"checkpoint": "artifacts/equilibrated.chk",
                                 "state": "artifacts/equilibrated.xml"})
        create_node(jd, "prod", parent_node_ids=["eq_001"])

        inputs = resolve_node_inputs(jd, "prod_001", "prod")
        assert inputs["restart_from"].endswith("equilibrated.xml"), (
            "resolve_node_inputs must prefer state (XML) over checkpoint "
            "(binary) so the handoff is cross-node portable"
        )

    def test_resolve_node_inputs_prod_falls_back_to_checkpoint_when_no_state(
        self, full_dag
    ):
        """When the eq ancestor only carries a ``checkpoint`` artifact
        (no ``state``), resolve_node_inputs falls back to the checkpoint
        rather than returning no restart source. The XML state is the
        preferred vehicle when both are present."""
        jd = str(full_dag)
        inputs = resolve_node_inputs(jd, "prod_001", "prod")
        assert inputs["restart_from"].endswith("equilibrated.chk")

    def test_resolve_node_inputs_prod_walks_per_ancestor_state_then_checkpoint(
        self, job_dir
    ):
        """Headline regression: when a near prod ancestor carries only a
        ``checkpoint`` and a far prod ancestor carries a ``state``, the
        resolver MUST pick the near ancestor's checkpoint. The previous
        implementation walked all prods looking for state first and would
        silently roll the run back across an unsaved prod step.

        DAG: prod_003 -> prod_002(checkpoint, no state) -> prod_001(state)
             -> eq_001 -> topo_001
        Expected restart_from: prod_002/checkpoint.chk
        """
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001", artifacts={"merged_pdb": "x.pdb"})
        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(jd, "solv_001",
                      artifacts={"solvated_pdb": "x.pdb",
                                 "box_dimensions": "x.json"})
        create_node(jd, "topo", parent_node_ids=["solv_001"])
        complete_node(
            jd, "topo_001",
            artifacts={"system_xml": "artifacts/system.xml",
                       "topology_pdb": "artifacts/topology.pdb",
                       "state_xml": "artifacts/state.xml"},
        )
        create_node(jd, "eq", parent_node_ids=["topo_001"])
        complete_node(
            jd, "eq_001",
            artifacts={"checkpoint": "artifacts/equilibrated.chk",
                       "state": "artifacts/equilibrated.xml"},
            metadata={"final_step": 0},
        )
        # Far prod with state — the buggy ordering would prefer this.
        create_node(jd, "prod", parent_node_ids=["eq_001"])
        complete_node(
            jd, "prod_001",
            artifacts={"state": "artifacts/state.xml",
                       "trajectory": "artifacts/trajectory.dcd"},
            metadata={"final_step": 100},
        )
        # Near prod with only a checkpoint.
        create_node(jd, "prod", parent_node_ids=["prod_001"])
        complete_node(
            jd, "prod_002",
            artifacts={"checkpoint": "artifacts/checkpoint.chk",
                       "trajectory": "artifacts/trajectory.dcd"},
            metadata={"final_step": 200},
        )
        create_node(jd, "prod", parent_node_ids=["prod_002"])

        inputs = resolve_node_inputs(jd, "prod_003", "prod")
        # The fix: per-ancestor BFS — for prod_002 we try state, miss,
        # try checkpoint, hit. We never walk to prod_001.
        assert inputs["restart_from"].endswith(
            "prod_002/artifacts/checkpoint.chk"
        ), inputs["restart_from"]
        assert inputs["restart_from_node_id"] == "prod_002"

    def test_resolve_node_inputs_prod_uses_nearest_state_when_present(
        self, job_dir
    ):
        """Sanity: when the nearest prod has a state, that state wins —
        nothing changed for the common case."""
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001", artifacts={"merged_pdb": "x.pdb"})
        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(jd, "solv_001",
                      artifacts={"solvated_pdb": "x.pdb",
                                 "box_dimensions": "x.json"})
        create_node(jd, "topo", parent_node_ids=["solv_001"])
        complete_node(
            jd, "topo_001",
            artifacts={"system_xml": "artifacts/system.xml",
                       "topology_pdb": "artifacts/topology.pdb",
                       "state_xml": "artifacts/state.xml"},
        )
        create_node(jd, "eq", parent_node_ids=["topo_001"])
        complete_node(
            jd, "eq_001",
            artifacts={"state": "artifacts/equilibrated.xml"},
            metadata={"final_step": 0},
        )
        create_node(jd, "prod", parent_node_ids=["eq_001"])
        complete_node(
            jd, "prod_001",
            artifacts={"state": "artifacts/state.xml",
                       "trajectory": "artifacts/trajectory.dcd"},
            metadata={"final_step": 200},
        )
        create_node(jd, "prod", parent_node_ids=["prod_001"])
        inputs = resolve_node_inputs(jd, "prod_002", "prod")
        assert inputs["restart_from"].endswith("prod_001/artifacts/state.xml")
        assert inputs["restart_from_node_id"] == "prod_001"

    def test_read_ancestor_final_step_uses_resolver_chosen_node(
        self, job_dir
    ):
        """``read_ancestor_final_step`` must read ``final_step`` from the
        same ancestor whose artifact ``_resolve_md_restart`` chose, not
        from the nearest prod / eq. The two diverge whenever the chosen
        artifact lives on a non-nearest ancestor — under the BFS-order
        fix this happens when a prod was registered without artifacts
        but a sibling has them, or in the regression scenario above."""
        from mdclaw._node import read_ancestor_final_step

        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001", artifacts={"merged_pdb": "x.pdb"})
        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(jd, "solv_001",
                      artifacts={"solvated_pdb": "x.pdb",
                                 "box_dimensions": "x.json"})
        create_node(jd, "topo", parent_node_ids=["solv_001"])
        complete_node(
            jd, "topo_001",
            artifacts={"system_xml": "artifacts/system.xml",
                       "topology_pdb": "artifacts/topology.pdb",
                       "state_xml": "artifacts/state.xml"},
        )
        create_node(jd, "eq", parent_node_ids=["topo_001"])
        complete_node(
            jd, "eq_001",
            artifacts={"state": "artifacts/equilibrated.xml"},
            metadata={"final_step": 0},
        )
        create_node(jd, "prod", parent_node_ids=["eq_001"])
        complete_node(
            jd, "prod_001",
            artifacts={"state": "artifacts/state.xml",
                       "trajectory": "artifacts/trajectory.dcd"},
            metadata={"final_step": 100},
        )
        create_node(jd, "prod", parent_node_ids=["prod_001"])
        complete_node(
            jd, "prod_002",
            artifacts={"checkpoint": "artifacts/checkpoint.chk",
                       "trajectory": "artifacts/trajectory.dcd"},
            metadata={"final_step": 200},
        )
        create_node(jd, "prod", parent_node_ids=["prod_002"])

        inputs = resolve_node_inputs(jd, "prod_003", "prod")
        # Resolver picks prod_002's checkpoint (nearest with an artifact).
        assert inputs["restart_from_node_id"] == "prod_002"
        # final_step must come from prod_002 (the chosen ancestor),
        # not prod_001 (the nearest prod with a *state*).
        step = read_ancestor_final_step(
            jd, "prod_003", restart_node_id="prod_002",
        )
        assert step == 200, (
            f"final_step must come from prod_002; got {step!r}"
        )
        # Default path replays the same BFS — also returns 200.
        assert read_ancestor_final_step(jd, "prod_003") == 200

    def test_resolve_node_inputs_prod_refuses_to_skip_completed_empty_ancestor(
        self, job_dir,
    ):
        """A completed prod ancestor that registers no restart artifact
        (state / checkpoint) is a broken DAG: skipping past it would
        silently roll the run back across whatever the user's tool
        produced. The resolver must surface ``restart_from_error``
        rather than walking up to the older prod_001 state.

        DAG: prod_003 -> prod_002(completed, trajectory only) ->
             prod_001(state)
        """
        from mdclaw._node import read_ancestor_final_step

        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001", artifacts={"merged_pdb": "x.pdb"})
        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(jd, "solv_001",
                      artifacts={"solvated_pdb": "x.pdb",
                                 "box_dimensions": "x.json"})
        create_node(jd, "topo", parent_node_ids=["solv_001"])
        complete_node(
            jd, "topo_001",
            artifacts={"system_xml": "artifacts/system.xml",
                       "topology_pdb": "artifacts/topology.pdb",
                       "state_xml": "artifacts/state.xml"},
        )
        create_node(jd, "eq", parent_node_ids=["topo_001"])
        complete_node(
            jd, "eq_001",
            artifacts={"state": "artifacts/equilibrated.xml"},
            metadata={"final_step": 0},
        )
        # Older prod with state — the buggy resolver would jump here.
        create_node(jd, "prod", parent_node_ids=["eq_001"])
        complete_node(
            jd, "prod_001",
            artifacts={"state": "artifacts/state.xml",
                       "trajectory": "artifacts/trajectory.dcd"},
            metadata={"final_step": 100},
        )
        # prod_002: completed, but no state and no checkpoint.
        create_node(jd, "prod", parent_node_ids=["prod_001"])
        complete_node(
            jd, "prod_002",
            artifacts={"trajectory": "artifacts/trajectory.dcd"},
            metadata={"final_step": 200},
        )
        create_node(jd, "prod", parent_node_ids=["prod_002"])

        inputs = resolve_node_inputs(jd, "prod_003", "prod")
        assert "restart_from" not in inputs, (
            "Resolver must not silently skip a completed ancestor that "
            "produced no restart artifact"
        )
        assert "restart_from_error" in inputs
        assert "prod_002" in inputs["restart_from_error"]
        assert (
            "neither" in inputs["restart_from_error"].lower()
            or "state" in inputs["restart_from_error"].lower()
        )

        # ``read_ancestor_final_step`` follows the same picker, so it
        # returns ``None`` (not 100 from prod_001).
        assert read_ancestor_final_step(jd, "prod_003") is None

    def test_resolve_node_inputs_eq_chain_refuses_completed_empty_eq(
        self, job_dir,
    ):
        """eq → eq chaining: a completed eq parent without state /
        checkpoint must surface ``restart_from_error`` rather than
        making the new eq node start fresh from the topo state.xml."""
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001", artifacts={"merged_pdb": "x.pdb"})
        create_node(jd, "topo", parent_node_ids=["prep_001"])
        complete_node(
            jd, "topo_001",
            artifacts={"system_xml": "artifacts/system.xml",
                       "topology_pdb": "artifacts/topology.pdb",
                       "state_xml": "artifacts/state.xml"},
        )
        create_node(jd, "eq", parent_node_ids=["topo_001"])
        # eq_001 is completed but only carries final_structure; the
        # checkpoint / state were never written (broken DAG).
        complete_node(
            jd, "eq_001",
            artifacts={"final_structure": "artifacts/equilibrated.pdb"},
            metadata={"final_step": 250000},
        )
        create_node(jd, "eq", parent_node_ids=["eq_001"])

        inputs = resolve_node_inputs(jd, "eq_002", "eq")
        assert "restart_from" not in inputs
        assert "restart_from_error" in inputs
        assert "eq_001" in inputs["restart_from_error"]

    def test_resolve_node_inputs_prod_continue_from_prefers_state(
        self, job_dir
    ):
        """--continue-from also prefers state over checkpoint."""
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001", artifacts={"merged_pdb": "x.pdb"})
        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(jd, "solv_001",
                      artifacts={"solvated_pdb": "x.pdb",
                                 "box_dimensions": "x.json"})
        create_node(jd, "topo", parent_node_ids=["solv_001"])
        complete_node(jd, "topo_001",
                      artifacts={"system_xml": "artifacts/system.xml",
                                 "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"})
        create_node(jd, "eq", parent_node_ids=["topo_001"])
        complete_node(jd, "eq_001",
                      artifacts={"checkpoint": "artifacts/equilibrated.chk",
                                 "state": "artifacts/equilibrated.xml"})
        create_node(jd, "prod", parent_node_ids=["eq_001"])
        complete_node(jd, "prod_001",
                      artifacts={"checkpoint": "artifacts/checkpoint.chk",
                                 "state": "artifacts/state.xml"})
        create_node(jd, "prod", continue_from="prod_001")
        inputs = resolve_node_inputs(jd, "prod_002", "prod")
        assert inputs["restart_from"].endswith(
            "prod_001/artifacts/state.xml"
        ), "continue_from must prefer state XML over checkpoint too"

    def test_read_ancestor_final_step_returns_eq_value(self, job_dir):
        """read_ancestor_final_step picks up metadata.final_step from
        the eq ancestor so run_production can restore simulation.currentStep
        after loadState."""
        from mdclaw._node import read_ancestor_final_step
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001", artifacts={"merged_pdb": "x.pdb"})
        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(jd, "solv_001",
                      artifacts={"solvated_pdb": "x.pdb",
                                 "box_dimensions": "x.json"})
        create_node(jd, "topo", parent_node_ids=["solv_001"])
        complete_node(jd, "topo_001",
                      artifacts={"system_xml": "artifacts/system.xml",
                                 "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"})
        create_node(jd, "eq", parent_node_ids=["topo_001"])
        complete_node(jd, "eq_001",
                      artifacts={"state": "artifacts/equilibrated.xml"},
                      metadata={"final_step": 0})
        create_node(jd, "prod", parent_node_ids=["eq_001"])
        assert read_ancestor_final_step(jd, "prod_001") == 0

    def test_read_ancestor_final_step_prefers_prod_over_eq(self, job_dir):
        """For prod→prod extension, read the *prod* ancestor's final_step
        so the cumulative step counter continues correctly."""
        from mdclaw._node import read_ancestor_final_step
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001", artifacts={"merged_pdb": "x.pdb"})
        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(jd, "solv_001",
                      artifacts={"solvated_pdb": "x.pdb",
                                 "box_dimensions": "x.json"})
        create_node(jd, "topo", parent_node_ids=["solv_001"])
        complete_node(jd, "topo_001",
                      artifacts={"system_xml": "artifacts/system.xml",
                                 "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"})
        create_node(jd, "eq", parent_node_ids=["topo_001"])
        complete_node(jd, "eq_001",
                      artifacts={"state": "artifacts/equilibrated.xml"},
                      metadata={"final_step": 0})
        create_node(jd, "prod", parent_node_ids=["eq_001"])
        complete_node(jd, "prod_001",
                      artifacts={"state": "artifacts/state.xml"},
                      metadata={"final_step": 250000})
        create_node(jd, "prod", parent_node_ids=["prod_001"])
        assert read_ancestor_final_step(jd, "prod_002") == 250000

    def test_read_ancestor_final_step_returns_none_when_missing(
        self, full_dag
    ):
        """Nodes without ``final_step`` metadata: the helper returns
        ``None`` so the caller falls back to ``simulation.currentStep=0``
        after loadState — same observable behaviour as loadCheckpoint
        for a fresh prod (eq → prod)."""
        from mdclaw._node import read_ancestor_final_step
        assert read_ancestor_final_step(str(full_dag), "prod_001") is None

    def test_read_ancestor_final_step_explicit_none_skips_bfs_fallback(
        self, job_dir,
    ):
        """``restart_node_id=None`` is the run-side signal "external
        restart file — there is no DAG ancestor whose ``final_step``
        applies to ``simulation.currentStep``". The helper must
        return ``None`` *without* falling back to the BFS picker;
        otherwise an external state.xml would still inherit a DAG
        ancestor's step counter and silently roll the timeline back
        or forward.

        This is distinct from omitting ``restart_node_id`` entirely,
        which IS supposed to trigger the BFS fallback for legacy
        / non-node-mode callers (covered by
        ``test_read_ancestor_final_step_uses_resolver_chosen_node``).
        """
        from mdclaw._node import read_ancestor_final_step

        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001", artifacts={"merged_pdb": "x.pdb"})
        create_node(jd, "topo", parent_node_ids=["prep_001"])
        complete_node(
            jd, "topo_001",
            artifacts={"system_xml": "artifacts/system.xml",
                       "topology_pdb": "artifacts/topology.pdb",
                       "state_xml": "artifacts/state.xml"},
        )
        create_node(jd, "eq", parent_node_ids=["topo_001"])
        complete_node(
            jd, "eq_001",
            artifacts={"state": "artifacts/equilibrated.xml"},
            metadata={"final_step": 250000},
        )
        create_node(jd, "prod", parent_node_ids=["eq_001"])

        # Sentinel-vs-None contract:
        # - omitted → BFS fallback picks eq_001 → 250000.
        assert read_ancestor_final_step(jd, "prod_001") == 250000
        # - explicit None → "external file" → return None without
        #   running the BFS, even though eq_001 would have matched.
        assert read_ancestor_final_step(
            jd, "prod_001", restart_node_id=None,
        ) is None
        # - explicit ancestor id → read from that ancestor.
        assert read_ancestor_final_step(
            jd, "prod_001", restart_node_id="eq_001",
        ) == 250000

    # ------------------------------------------------------------------
    # eq_final_ensemble / eq_pressure_bar propagation (added with the
    # eq→prod ensemble auto-inheritance fix). Without these keys,
    # run_production cannot match its barostat to the eq's saved state
    # and loadState fails with an opaque OpenMM message.
    # ------------------------------------------------------------------

    def _build_eq_dag_for_prod(self, jd):
        """Build prep → solv → topo → eq → prod scaffold; tests stamp
        eq metadata via complete_node and assert resolve_node_inputs
        for prod_001."""
        create_node(jd, "prep")
        complete_node(jd, "prep_001", artifacts={"merged_pdb": "x.pdb"})
        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(jd, "solv_001",
                      artifacts={"solvated_pdb": "x.pdb",
                                 "box_dimensions": "x.json"})
        create_node(jd, "topo", parent_node_ids=["solv_001"])
        complete_node(jd, "topo_001",
                      artifacts={"system_xml": "artifacts/system.xml",
                                 "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"})
        create_node(jd, "eq", parent_node_ids=["topo_001"])

    def test_resolve_node_inputs_prod_surfaces_npt_eq_ensemble(self, job_dir):
        """NPT eq → prod resolver returns eq_final_ensemble and
        eq_pressure_bar so run_production can add a matching barostat."""
        jd = str(job_dir)
        self._build_eq_dag_for_prod(jd)
        complete_node(jd, "eq_001",
                      artifacts={"state": "artifacts/equilibrated.xml"},
                      metadata={"final_ensemble": "NPT",
                                "pressure_bar": 1.0,
                                "final_step": 0})
        create_node(jd, "prod", parent_node_ids=["eq_001"])

        inputs = resolve_node_inputs(jd, "prod_001", "prod")
        assert inputs.get("eq_final_ensemble") == "NPT"
        assert inputs.get("eq_pressure_bar") == 1.0

    def test_resolve_node_inputs_prod_nvt_eq_no_pressure_bar(self, job_dir):
        """NVT eq has pressure_bar=None; resolver returns
        eq_final_ensemble='NVT' but does NOT include eq_pressure_bar
        (the float check filters None) so prod's default-None stays."""
        jd = str(job_dir)
        self._build_eq_dag_for_prod(jd)
        complete_node(jd, "eq_001",
                      artifacts={"state": "artifacts/equilibrated.xml"},
                      metadata={"final_ensemble": "NVT",
                                "pressure_bar": None,
                                "final_step": 0})
        create_node(jd, "prod", parent_node_ids=["eq_001"])

        inputs = resolve_node_inputs(jd, "prod_001", "prod")
        assert inputs.get("eq_final_ensemble") == "NVT"
        assert "eq_pressure_bar" not in inputs

    def test_resolve_node_inputs_prod_legacy_eq_omits_ensemble_keys(
        self, job_dir
    ):
        """Legacy eq nodes (predating the final_ensemble metadata field)
        complete with no ensemble info; resolver returns neither key so
        prod auto-inherit becomes a no-op and we fall back to the
        guardrail at loadState. Backwards compatible."""
        jd = str(job_dir)
        self._build_eq_dag_for_prod(jd)
        complete_node(jd, "eq_001",
                      artifacts={"state": "artifacts/equilibrated.xml"},
                      metadata={"final_step": 0})  # no final_ensemble
        create_node(jd, "prod", parent_node_ids=["eq_001"])

        inputs = resolve_node_inputs(jd, "prod_001", "prod")
        assert "eq_final_ensemble" not in inputs
        assert "eq_pressure_bar" not in inputs

    def test_analyze_resolves_prod_trajectory_chain_single_prod(
        self, full_dag
    ):
        """An analyze node directly parented on a single prod node with
        one trajectory resolves to that trajectory plus the topo's
        ``topology_pdb`` (mdtraj-compatible)."""
        from mdclaw._node import resolve_node_inputs
        jd = str(full_dag)
        complete_node(jd, "prod_001",
                      artifacts={"trajectory": "artifacts/trajectory.dcd",
                                 "state": "artifacts/state.xml"})
        create_node(
            jd,
            "analyze",
            parent_node_ids=["prod_001"],
            conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
        )
        inputs = resolve_node_inputs(jd, "analyze_001", "analyze")
        assert "topology_file" in inputs
        assert inputs["topology_file"].endswith("topo_001/artifacts/topology.pdb")
        chain = inputs["trajectory_chain"]
        assert len(chain) == 1
        assert chain[0].endswith("prod_001/artifacts/trajectory.dcd")

    def test_analyze_resolves_energy_chain_alongside_trajectory(
        self, full_dag
    ):
        """Each prod's ``energy`` artifact (StateDataReporter CSV) is
        chained in parallel to the ``trajectory`` so concat_trajectory
        can strip + stride them together. Order matches the trajectory
        chain exactly."""
        from mdclaw._node import resolve_node_inputs
        jd = str(full_dag)
        complete_node(jd, "prod_001",
                      artifacts={"trajectory": "artifacts/trajectory.dcd",
                                 "energy": "artifacts/energy.dat",
                                 "state": "artifacts/state.xml"})
        create_node(jd, "prod", continue_from="prod_001")
        complete_node(jd, "prod_002",
                      artifacts={"trajectory": "artifacts/trajectory.dcd",
                                 "energy": "artifacts/energy.dat"})
        create_node(
            jd,
            "analyze",
            parent_node_ids=["prod_002"],
            conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
        )
        inputs = resolve_node_inputs(jd, "analyze_001", "analyze")
        energy_chain = inputs.get("energy_chain")
        assert energy_chain is not None
        assert len(energy_chain) == 2
        assert "prod_001/artifacts/energy.dat" in energy_chain[0]
        assert "prod_002/artifacts/energy.dat" in energy_chain[1]
        # Lengths must match the trajectory chain — rows-per-frame
        # alignment is the whole point of pairing them.
        assert len(inputs["trajectory_chain"]) == len(energy_chain)

    def test_analyze_energy_chain_skips_prods_without_energy_artifact(
        self, full_dag
    ):
        """A prod ancestor that produced a trajectory but crashed
        before the energy reporter flushed (or legacy prod nodes from
        before the energy artifact existed) should be silently skipped
        — we can't recover what isn't there, and falsely dropping the
        whole concat just because one leg lacks a CSV would be
        disproportionate."""
        from mdclaw._node import resolve_node_inputs
        jd = str(full_dag)
        complete_node(jd, "prod_001",
                      artifacts={"trajectory": "artifacts/trajectory.dcd",
                                 "energy": "artifacts/energy.dat"})
        create_node(jd, "prod", continue_from="prod_001")
        complete_node(jd, "prod_002",
                      artifacts={"trajectory": "artifacts/trajectory.dcd"})
        create_node(
            jd,
            "analyze",
            parent_node_ids=["prod_002"],
            conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
        )
        inputs = resolve_node_inputs(jd, "analyze_001", "analyze")
        # 2 trajectories, but only 1 energy
        assert len(inputs["trajectory_chain"]) == 2
        assert len(inputs["energy_chain"]) == 1

    def test_analyze_resolves_trajectory_chain_in_chronological_order(
        self, full_dag
    ):
        """prod_001 → prod_002 → prod_003 with continue_from: analyze on
        prod_003 must return DCDs in the order [prod_001, prod_002, prod_003]
        (oldest first). Reverse order breaks time-series concatenation."""
        from mdclaw._node import resolve_node_inputs
        jd = str(full_dag)
        complete_node(jd, "prod_001",
                      artifacts={"trajectory": "artifacts/trajectory.dcd",
                                 "state": "artifacts/state.xml"})
        create_node(jd, "prod", continue_from="prod_001")
        complete_node(jd, "prod_002",
                      artifacts={"trajectory": "artifacts/trajectory.dcd",
                                 "state": "artifacts/state.xml"})
        create_node(jd, "prod", continue_from="prod_002")
        complete_node(jd, "prod_003",
                      artifacts={"trajectory": "artifacts/trajectory.dcd",
                                 "state": "artifacts/state.xml"})
        create_node(
            jd,
            "analyze",
            parent_node_ids=["prod_003"],
            conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
        )
        inputs = resolve_node_inputs(jd, "analyze_001", "analyze")
        chain = inputs["trajectory_chain"]
        assert len(chain) == 3
        assert "prod_001/artifacts" in chain[0]
        assert "prod_002/artifacts" in chain[1]
        assert "prod_003/artifacts" in chain[2]

    def test_analyze_blocks_prod_parent_without_trajectory_artifact(
        self, full_dag
    ):
        """A prod parent that never completed must block auto-resolution."""
        from mdclaw._node import resolve_node_inputs
        jd = str(full_dag)
        complete_node(jd, "prod_001",
                      artifacts={"trajectory": "artifacts/trajectory.dcd"})
        # prod_002 exists but has no artifacts (never completed)
        create_node(jd, "prod", continue_from="prod_001")
        create_node(
            jd,
            "analyze",
            parent_node_ids=["prod_002"],
            conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
        )
        inputs = resolve_node_inputs(jd, "analyze_001", "analyze")
        assert "trajectory_chain" not in inputs
        assert "input_resolution_error" in inputs
        assert "prod_002" in inputs["input_resolution_error"]

    def test_analyze_parent_analyze_resolves_combined_trajectory(
        self, full_dag
    ):
        """An analyze node whose parent is another analyze node
        resolves to the parent's combined_trajectory + reference_pdb —
        the Phase 2 input shape for rmsd / distance / q_value / fit."""
        from mdclaw._node import resolve_node_inputs
        jd = str(full_dag)
        # Phase 1 concat shape (parent=prod)
        complete_node(jd, "prod_001",
                      artifacts={"trajectory": "artifacts/trajectory.dcd"})
        create_node(
            jd,
            "analyze",
            parent_node_ids=["prod_001"],
            conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
        )
        complete_node(jd, "analyze_001",
                      artifacts={"combined_trajectory": "artifacts/combined.dcd",
                                 "reference_pdb": "artifacts/combined.pdb"})
        # Phase 2 shape (parent=analyze)
        create_node(
            jd,
            "analyze",
            parent_node_ids=["analyze_001"],
            conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
        )
        inputs = resolve_node_inputs(jd, "analyze_002", "analyze")
        assert inputs["trajectory_file"].endswith(
            "analyze_001/artifacts/combined.dcd"
        )
        assert inputs["reference_pdb"].endswith(
            "analyze_001/artifacts/combined.pdb"
        )
        # topology still resolves through the earlier topo ancestor
        assert inputs["topology_file"].endswith(
            "topo_001/artifacts/topology.pdb"
        )
        # Phase 1 keys must NOT appear in the Phase 2 resolution
        assert "trajectory_chain" not in inputs

    def test_analyze_parent_fit_prefers_fitted_over_combined(
        self, full_dag
    ):
        """When a parent analyze node exposes BOTH ``fitted_trajectory``
        and ``combined_trajectory``, Phase 2 tools get the fitted one —
        so a fit → rmsd chain picks up the aligned frames automatically."""
        from mdclaw._node import resolve_node_inputs
        jd = str(full_dag)
        complete_node(jd, "prod_001",
                      artifacts={"trajectory": "artifacts/trajectory.dcd"})
        create_node(
            jd,
            "analyze",
            parent_node_ids=["prod_001"],
            conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
        )
        # A single analyze node that emitted both artifacts (unusual
        # in practice but allowed — fit_trajectory does exactly this
        # when re-emitting reference_pdb and writing fitted.dcd)
        complete_node(jd, "analyze_001",
                      artifacts={"combined_trajectory": "artifacts/combined.dcd",
                                 "fitted_trajectory": "artifacts/fitted.dcd",
                                 "reference_pdb": "artifacts/combined.pdb"})
        create_node(
            jd,
            "analyze",
            parent_node_ids=["analyze_001"],
            conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
        )
        inputs = resolve_node_inputs(jd, "analyze_002", "analyze")
        assert inputs["trajectory_file"].endswith(
            "analyze_001/artifacts/fitted.dcd"
        ), "fitted_trajectory must win when both artifacts are present"

    def test_analyze_rejects_mixed_prod_and_analyze_parents(self, full_dag):
        """Multi-parent analyze is allowed (Phase 3) but parents must
        be uniformly prod or uniformly analyze. Mixing the two shapes
        confuses resolve_node_inputs (prods need chain-walking,
        analyze nodes already expose a ready trajectory), so reject
        mixed cases at create_node time."""
        jd = str(full_dag)
        complete_node(jd, "prod_001",
                      artifacts={"trajectory": "artifacts/trajectory.dcd"})
        create_node(
            jd,
            "analyze",
            parent_node_ids=["prod_001"],
            conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
        )
        complete_node(jd, "analyze_001",
                      artifacts={"combined_trajectory": "artifacts/combined.dcd",
                                 "reference_pdb": "artifacts/combined.pdb"})
        r = create_node(
            jd, "analyze", parent_node_ids=["prod_001", "analyze_001"]
        )
        assert r["success"] is False
        assert "cannot mix" in r["error"]

    def test_analyze_rejects_non_prod_non_analyze_parent(self, full_dag):
        """An analyze node parented on something other than prod or
        analyze (e.g. directly on topo) is rejected up-front with a
        structured error explaining the two valid shapes."""
        jd = str(full_dag)
        r = create_node(jd, "analyze", parent_node_ids=["topo_001"])
        assert r["success"] is False
        assert "prod" in r["error"] and "analyze" in r["error"]

    def test_analyze_rejects_eq_parent_at_create_time(self, full_dag):
        """Analyze nodes require a prod or analyze parent. Creating one
        directly above eq must fail at create_node — there is no valid
        trajectory source through an eq ancestor (eq doesn't emit
        ``trajectory`` artifacts), so failing fast is the right call
        instead of silently producing an empty-chain analyze."""
        jd = str(full_dag)
        r = create_node(jd, "analyze", parent_node_ids=["eq_001"])
        assert r["success"] is False
        assert "prod" in r["error"] or "analyze" in r["error"]

    def test_resolve_node_inputs_solv(self, full_dag):
        jd = str(full_dag)
        inputs = resolve_node_inputs(jd, "solv_001", "solv")
        assert "pdb_file" in inputs
        assert inputs["pdb_file"].endswith("merged.pdb")

    def test_resolve_node_inputs_topo(self, full_dag):
        jd = str(full_dag)
        inputs = resolve_node_inputs(jd, "topo_001", "topo")
        assert "pdb_file" in inputs
        assert inputs["pdb_file"].endswith("solvated.pdb")

    # ── Modern artifact triple (system.xml + topology.pdb + state.xml) ────────

    @pytest.fixture
    def modern_dag(self, job_dir):
        """prep→solv→topo→eq DAG where topo emits the XML triple
        (``system.xml`` + ``topology.pdb`` + ``state.xml``) — the only
        topology contract supported on the run side."""
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(
            jd, "prep_001",
            artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
        )
        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(
            jd, "solv_001",
            artifacts={
                "solvated_pdb": "artifacts/solvated.pdb",
                "box_dimensions": "artifacts/box_dimensions.json",
            },
        )
        create_node(jd, "topo", parent_node_ids=["solv_001"])
        complete_node(
            jd, "topo_001",
            artifacts={
                "system_xml": "artifacts/system.xml",
                "topology_pdb": "artifacts/topology.pdb",
                "state_xml": "artifacts/state.xml",
            },
        )
        create_node(jd, "eq", parent_node_ids=["topo_001"])
        return job_dir

    def test_resolve_modern_eq_uses_xml_triple(self, modern_dag):
        jd = str(modern_dag)
        inputs = resolve_node_inputs(jd, "eq_001", "eq")
        assert "system_xml_file" in inputs
        assert "topology_pdb_file" in inputs
        assert "state_xml_file" in inputs
        assert inputs["system_xml_file"].endswith(
            "topo_001/artifacts/system.xml"
        )
        assert inputs["topology_pdb_file"].endswith(
            "topo_001/artifacts/topology.pdb"
        )
        assert inputs["state_xml_file"].endswith(
            "topo_001/artifacts/state.xml"
        )

    def test_resolve_modern_prod_uses_xml_triple(self, modern_dag):
        jd = str(modern_dag)
        complete_node(
            jd, "eq_001",
            artifacts={
                "state": "artifacts/equilibrated.xml",
                "checkpoint": "artifacts/equilibrated.chk",
            },
        )
        create_node(jd, "prod", parent_node_ids=["eq_001"])
        inputs = resolve_node_inputs(jd, "prod_001", "prod")
        assert inputs["system_xml_file"].endswith("system.xml")
        assert inputs["topology_pdb_file"].endswith("topology.pdb")
        assert inputs["state_xml_file"].endswith(
            "topo_001/artifacts/state.xml"
        )
        assert inputs["restart_from"].endswith("equilibrated.xml")

    def test_resolve_modern_analyze_uses_topology_pdb(self, modern_dag):
        """Analyze branch picks up topology.pdb (mdtraj-compatible) as
        ``topology_file`` so atom-selection DSL works directly off the
        XML triple's PDB."""
        jd = str(modern_dag)
        complete_node(
            jd, "eq_001",
            artifacts={"state": "artifacts/equilibrated.xml"},
        )
        create_node(jd, "prod", parent_node_ids=["eq_001"])
        complete_node(
            jd, "prod_001",
            artifacts={
                "trajectory": "artifacts/trajectory.dcd",
                "state": "artifacts/state.xml",
            },
        )
        create_node(
            jd,
            "analyze",
            parent_node_ids=["prod_001"],
            conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
        )
        inputs = resolve_node_inputs(jd, "analyze_001", "analyze")
        assert inputs["topology_file"].endswith(
            "topo_001/artifacts/topology.pdb"
        )

    def test_resolve_modern_topo_surfaces_implicit_solvent_metadata(
        self, job_dir
    ):
        """Modern topo nodes built with implicit solvent stamp
        ``metadata.implicit_solvent`` on node.json. The resolver must
        surface it as ``topology_implicit_solvent`` so eq/prod can validate
        their runtime ``--implicit-solvent`` flag against the build-time
        choice (catches OBC2-built-but-GBn2-requested silent mismatches).
        """
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(
            jd, "prep_001",
            artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
        )
        create_node(jd, "topo", parent_node_ids=["prep_001"])
        complete_node(
            jd, "topo_001",
            artifacts={
                "system_xml": "artifacts/system.xml",
                "topology_pdb": "artifacts/topology.pdb",
                "state_xml": "artifacts/state.xml",
            },
            metadata={
                "implicit_solvent": "OBC2",
                "hmr": True,
                "solvent_type": "implicit",
            },
        )
        create_node(jd, "eq", parent_node_ids=["topo_001"])

        inputs = resolve_node_inputs(jd, "eq_001", "eq")
        assert inputs["topology_implicit_solvent"] == "OBC2"
        assert inputs["topology_hmr"] is True
        assert inputs["topology_solvent_type"] == "implicit"

        create_node(jd, "prod", parent_node_ids=["eq_001"])
        complete_node(
            jd, "eq_001",
            artifacts={"state": "artifacts/equilibrated.xml"},
        )
        prod_inputs = resolve_node_inputs(jd, "prod_001", "prod")
        # Same topo metadata must propagate down the prod path too —
        # eq and prod share the topo ancestor's saved system.xml and
        # therefore must see the same build-time implicit_solvent.
        assert prod_inputs["topology_implicit_solvent"] == "OBC2"

    def test_resolve_modern_topo_without_implicit_metadata_returns_none(
        self, modern_dag
    ):
        """Explicit-solvent / vacuum topo nodes (no
        ``metadata.implicit_solvent``) must surface the field as ``None``
        so the run-side guard skips the check rather than blocking on
        missing metadata."""
        jd = str(modern_dag)
        # ``modern_dag`` fixture completes topo_001 without metadata, so
        # the resolver should report None for all three build-time hints.
        inputs = resolve_node_inputs(jd, "eq_001", "eq")
        assert inputs["topology_implicit_solvent"] is None
        assert inputs["topology_hmr"] is None
        assert inputs["topology_solvent_type"] is None

    def test_resolver_pins_to_a_single_topo_for_modern_triple(self, job_dir):
        """If topo_002 has only system_xml and topo_001 (older) has the full
        triple, the resolver MUST NOT mix system_xml from topo_002 with
        topology_pdb / state_xml from topo_001 — the two topo nodes refer to
        different physical Systems. The expected outcome is an explicit
        input_resolution_error, not a silent walk to the older topo."""
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(
            jd, "prep_001",
            artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
        )
        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(
            jd, "solv_001",
            artifacts={
                "solvated_pdb": "artifacts/solvated.pdb",
                "box_dimensions": "artifacts/box_dimensions.json",
            },
        )

        # topo_001 carries a complete triple.
        create_node(jd, "topo", parent_node_ids=["solv_001"])
        complete_node(
            jd, "topo_001",
            artifacts={
                "system_xml": "artifacts/system.xml",
                "topology_pdb": "artifacts/topology.pdb",
                "state_xml": "artifacts/state.xml",
            },
        )

        # topo_002 carries ONLY system_xml.
        create_node(jd, "topo", parent_node_ids=["topo_001"])
        complete_node(
            jd, "topo_002",
            artifacts={"system_xml": "artifacts/system.xml"},
        )

        # eq node directly above topo_002 — the broken topo.
        create_node(jd, "eq", parent_node_ids=["topo_002"])

        inputs = resolve_node_inputs(jd, "eq_001", "eq")
        # Must NOT have silently mixed topo_001's topology with topo_002's system.
        assert "system_xml_file" not in inputs
        assert "topology_pdb_file" not in inputs
        assert "input_resolution_error" in inputs
        msg = inputs["input_resolution_error"]
        assert "topo_002" in msg
        assert "topology_pdb" in msg

    def test_resolver_does_not_skip_nearest_topo_missing_system_xml(self, job_dir):
        """A nearest topo that lacks system_xml is still the selected topo.

        The resolver must report that broken nearest topo rather than walking
        upward to an older complete topo, because that would silently bind a
        different physical System into the run node.
        """
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(
            jd, "prep_001",
            artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
        )
        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(
            jd, "solv_001",
            artifacts={
                "solvated_pdb": "artifacts/solvated.pdb",
                "box_dimensions": "artifacts/box_dimensions.json",
            },
        )

        create_node(jd, "topo", parent_node_ids=["solv_001"])
        complete_node(
            jd, "topo_001",
            artifacts={
                "system_xml": "artifacts/system.xml",
                "topology_pdb": "artifacts/topology.pdb",
                "state_xml": "artifacts/state.xml",
            },
        )

        create_node(jd, "topo", parent_node_ids=["topo_001"])
        complete_node(
            jd, "topo_002",
            artifacts={"topology_pdb": "artifacts/topology.pdb"},
        )
        create_node(jd, "eq", parent_node_ids=["topo_002"])

        inputs = resolve_node_inputs(jd, "eq_001", "eq")

        assert "system_xml_file" not in inputs
        assert "topology_pdb_file" not in inputs
        assert "input_resolution_error" in inputs
        msg = inputs["input_resolution_error"]
        assert "topo_002" in msg
        assert "system_xml" in msg



# ── Structured (non-path) artifact propagation ─────────────────────────────


class TestStructuredArtifactPropagation:
    """Covers DAG propagation of ligand chemistry and box dimensions."""

    @pytest.fixture
    def dag_with_ligand(self, job_dir):
        """prep (with ligand_chemistry) -> solv (with box_dimensions.json) -> topo."""
        jd = str(job_dir)
        prep_artifacts = job_dir / "nodes" / "prep_001" / "artifacts"
        sdf_path = prep_artifacts / "split" / "AP5.sdf"
        ligand_chemistry = [
            {
                "sdf": str(sdf_path),
                "residue_name": "AP5",
            }
        ]

        create_node(jd, "prep")
        complete_node(
            jd,
            "prep_001",
            artifacts={
                "merged_pdb": "artifacts/merge/merged.pdb",
                "ligand_chemistry": ligand_chemistry,
            },
        )

        create_node(jd, "solv", parent_node_ids=["prep_001"])
        # Write a real box_dimensions.json so resolve_node_inputs can load it.
        solv_artifacts = job_dir / "nodes" / "solv_001" / "artifacts"
        solv_artifacts.mkdir(parents=True, exist_ok=True)
        box = {"box_a": 77.78, "box_b": 77.78, "box_c": 77.78,
               "alpha": 90.0, "beta": 90.0, "gamma": 90.0, "is_cubic": True}
        (solv_artifacts / "box_dimensions.json").write_text(json.dumps(box))
        complete_node(
            jd,
            "solv_001",
            artifacts={
                "solvated_pdb": "artifacts/solvated.pdb",
                "box_dimensions": "artifacts/box_dimensions.json",
            },
        )

        create_node(jd, "topo", parent_node_ids=["solv_001"])
        return job_dir, ligand_chemistry, box

    def test_find_ancestor_returns_str_for_path_artifact(self, dag_with_ligand):
        """Contract: string-valued artifacts are resolved to abs paths."""
        job_dir, _lp, _box = dag_with_ligand
        result = find_ancestor_artifact(str(job_dir), "topo_001", "solv",
                                        "solvated_pdb")
        assert isinstance(result, str)
        assert result.endswith("solv_001/artifacts/solvated.pdb")

    def test_find_ancestor_returns_list_for_structured_artifact(self,
                                                                dag_with_ligand):
        """Structured artifacts are stored relative and resolved for execution."""
        job_dir, lp, _box = dag_with_ligand
        stored = read_node(str(job_dir), "prep_001")["artifacts"]["ligand_chemistry"]
        assert stored[0]["sdf"] == "artifacts/split/AP5.sdf"
        result = find_ancestor_artifact(str(job_dir), "topo_001", "prep",
                                        "ligand_chemistry")
        assert isinstance(result, list)
        assert result == lp

    def test_resolve_topo_inputs_three_level_dag(self, dag_with_ligand):
        """prep grandparent -> solv parent -> topo child: ligand_chemistry
        must be propagated all the way to topo."""
        job_dir, lp, box = dag_with_ligand
        inputs = resolve_node_inputs(str(job_dir), "topo_001", "topo")

        # pdb_file from solv parent
        assert "pdb_file" in inputs
        assert inputs["pdb_file"].endswith("solvated.pdb")

        # ligand_chemistry from prep grandparent (stored relative, resolved absolute)
        assert "ligand_chemistry" in inputs
        assert inputs["ligand_chemistry"] == lp

        # box_dimensions loaded inline from solv's JSON file
        assert "box_dimensions" in inputs
        assert inputs["box_dimensions"] == box

    def test_resolve_solv_inputs_includes_ligand_chemistry(self, dag_with_ligand):
        """prep -> solv must carry ligand charge metadata before ionization."""
        job_dir, lp, _box = dag_with_ligand
        inputs = resolve_node_inputs(str(job_dir), "solv_001", "solv")

        assert inputs["pdb_file"].endswith("merged.pdb")
        assert inputs["ligand_chemistry"] == lp

    def test_resolve_topo_omits_keys_when_prep_has_no_params(self, job_dir):
        """If prep never wrote ligand params, resolve_node_inputs omits them."""
        jd = str(job_dir)
        create_node(jd, "prep")
        complete_node(jd, "prep_001",
                      artifacts={"merged_pdb": "artifacts/merge/merged.pdb"})
        create_node(jd, "solv", parent_node_ids=["prep_001"])
        complete_node(jd, "solv_001",
                      artifacts={"solvated_pdb": "artifacts/solvated.pdb"})
        create_node(jd, "topo", parent_node_ids=["solv_001"])

        inputs = resolve_node_inputs(jd, "topo_001", "topo")
        assert "pdb_file" in inputs
        assert "ligand_chemistry" not in inputs
        assert "box_dimensions" not in inputs


# ── Event integration ──────────────────────────────────────────────────────


class TestNodeEvents:

    def test_create_node_writes_event(self, job_dir):
        create_node(str(job_dir), "prep")
        events_dir = job_dir / "events"
        assert events_dir.is_dir()
        event_files = list(events_dir.glob("*.json"))
        assert len(event_files) == 1
        ev = json.loads(event_files[0].read_text())
        assert ev["event_type"] == "node_created"
        assert ev["node_id"] == "prep_001"

    def test_begin_writes_event(self, job_dir):
        create_node(str(job_dir), "eq")
        begin_node(str(job_dir), "eq_001")
        events = list((job_dir / "events").glob("*tool_started*"))
        assert len(events) == 1

    def test_complete_writes_event(self, job_dir):
        create_node(str(job_dir), "eq")
        complete_node(str(job_dir), "eq_001",
                      artifacts={"chk": "artifacts/eq.chk"})
        events = list((job_dir / "events").glob("*tool_completed*"))
        assert len(events) == 1

    def test_fail_writes_event(self, job_dir):
        create_node(str(job_dir), "eq")
        fail_node(str(job_dir), "eq_001", errors=["boom"])
        events = list((job_dir / "events").glob("*tool_failed*"))
        assert len(events) == 1
        ev = json.loads(events[0].read_text())
        assert ev["details"]["errors"] == ["boom"]


# ── Source node (DAG root) ──────────────────────────────────────────────────


class TestSourceNode:
    """Source is the DAG-root node type for structure acquisition."""

    def test_source_is_valid_node_type(self, job_dir):
        result = create_node(str(job_dir), "source")
        assert result["success"] is True
        assert result["node_id"] == "source_001"

    def test_source_as_dag_root_no_parent(self, job_dir):
        result = create_node(str(job_dir), "source")
        node = read_node(str(job_dir), result["node_id"])
        assert node["parent_node_ids"] == []


class TestSourceStudyContext:
    """Soft study-first signal on source-node creation (non-blocking)."""

    def test_bare_job_dir_source_flags_missing_study_context(self, job_dir):
        # job_dir fixture is a bare tmp dir, not under a study.
        result = create_node(str(job_dir), "source")
        assert result["success"] is True
        assert result["study_context"]["code"] == "study_context_missing"
        assert result["study_context"]["linked"] is False
        # Warning is durable on the node so inspect_job surfaces it.
        node = read_node(str(job_dir), result["node_id"])
        assert any("study_context_missing" in w for w in node["warnings"])

    def test_source_under_study_job_params_is_clean(self, job_dir):
        # Simulate what bootstrap_md_workflow writes into job params.
        update_job_params(str(job_dir), {"study_dir": "/some/study", "study_job_id": "main"})
        result = create_node(str(job_dir), "source")
        assert result["success"] is True
        assert "study_context" not in result
        node = read_node(str(job_dir), result["node_id"])
        assert node["warnings"] == []

    def test_source_under_canonical_study_layout_is_clean(self, tmp_path):
        # Canonical <study>/jobs/<job_id>/ filesystem layout, no params yet.
        study_dir = tmp_path / "study_abc"
        (study_dir / "jobs" / "main").mkdir(parents=True)
        (study_dir / "study.json").write_text("{}")
        jd = study_dir / "jobs" / "main"
        result = create_node(str(jd), "source")
        assert result["success"] is True
        assert "study_context" not in result

    def test_non_source_nodes_never_flag_study_context(self, job_dir):
        create_node(str(job_dir), "source")
        result = create_node(str(job_dir), "prep", parent_node_ids=["source_001"])
        assert result["success"] is True
        assert "study_context" not in result

    def test_source_rejects_parent_node_ids(self, job_dir):
        """source is the DAG root — parents are forbidden by invariant."""
        jd = str(job_dir)
        # Build a valid existing node first (so the rejection isn't from
        # a missing-reference error)
        create_node(jd, "source")
        result = create_node(jd, "source", parent_node_ids=["source_001"])
        assert result["success"] is False
        assert "DAG root" in result["error"]
        assert "parent_node_ids" in result["error"]
        # Index unchanged: only the original source_001 exists
        progress = json.loads((job_dir / "progress.json").read_text())
        assert list(progress["nodes"].keys()) == ["source_001"]

    def test_source_rejects_dependency_node_ids(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "source")
        result = create_node(jd, "source", dependency_node_ids=["source_001"])
        assert result["success"] is False
        assert "DAG root" in result["error"]
        assert "dependency_node_ids" in result["error"]
        progress = json.loads((job_dir / "progress.json").read_text())
        assert list(progress["nodes"].keys()) == ["source_001"]

    def test_source_lifecycle(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "source", label="PDB 1AKE")
        begin_node(jd, "source_001")
        complete_node(
            jd,
            "source_001",
            artifacts={"structure_file": "artifacts/1AKE.pdb"},
            metadata={
                "source_type": "pdb",
                "source_id": "1AKE",
                "sha256": "deadbeef",
            },
        )
        node = read_node(jd, "source_001")
        assert node["status"] == "completed"
        assert node["label"] == "PDB 1AKE"
        assert node["artifacts"]["structure_file"] == "artifacts/1AKE.pdb"
        assert node["metadata"]["source_type"] == "pdb"

    def test_prep_resolves_structure_file_from_source(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "source")
        # Create the actual file so resolve gives a usable path
        (job_dir / "nodes" / "source_001" / "artifacts" / "1AKE.pdb").write_text("HEADER\n")
        complete_node(
            jd,
            "source_001",
            artifacts={"structure_file": "artifacts/1AKE.pdb"},
        )
        create_node(jd, "prep", parent_node_ids=["source_001"])
        inputs = resolve_node_inputs(jd, "prep_001", "prep")
        assert "structure_file" in inputs
        assert inputs["structure_file"].endswith("source_001/artifacts/1AKE.pdb")

    def test_prep_resolves_single_structure_source_bundle(self, job_dir):
        from mdclaw.source_bundle import build_source_bundle, write_source_bundle

        jd = str(job_dir)
        create_node(jd, "source")
        source_node_dir = job_dir / "nodes" / "source_001"
        source_file = source_node_dir / "artifacts" / "1AKE.pdb"
        source_file.write_text("HEADER\n")
        bundle = build_source_bundle(
            source_type="pdb",
            source_id="1AKE",
            structure_paths=[source_file],
            source_node_dir=source_node_dir,
        )
        rel_bundle = write_source_bundle(source_node_dir, bundle)
        complete_node(
            jd,
            "source_001",
            artifacts={
                "structure_file": bundle["structures"][0]["candidate_file"],
                "source_bundle": rel_bundle,
            },
        )
        create_node(jd, "prep", parent_node_ids=["source_001"])

        inputs = resolve_node_inputs(jd, "prep_001", "prep")

        assert inputs["structure_file"].endswith(
            "source_001/artifacts/candidates/candidate_001.pdb"
        )
        assert inputs["source_bundle_file"].endswith("source_001/artifacts/source_bundle.json")
        assert inputs["source_structure_id"] == "candidate_001"

    def test_prep_source_bundle_surfaces_multiple_candidates(self, job_dir):
        from mdclaw.source_bundle import build_source_bundle, write_source_bundle

        jd = str(job_dir)
        create_node(jd, "source")
        source_node_dir = job_dir / "nodes" / "source_001"
        first = source_node_dir / "artifacts" / "candidate_a.pdb"
        second = source_node_dir / "artifacts" / "candidate_b.pdb"
        first.write_text("HEADER A\n")
        second.write_text("HEADER B\n")
        bundle = build_source_bundle(
            source_type="generated",
            source_id="ensemble",
            structure_paths=[first, second],
            source_node_dir=source_node_dir,
        )
        rel_bundle = write_source_bundle(source_node_dir, bundle)
        complete_node(
            jd,
            "source_001",
            artifacts={
                "structure_file": bundle["structures"][0]["candidate_file"],
                "source_bundle": rel_bundle,
            },
        )
        create_node(jd, "prep", parent_node_ids=["source_001"])

        inputs = resolve_node_inputs(jd, "prep_001", "prep")

        assert "structure_file" not in inputs
        assert "input_resolution_error" not in inputs
        assert inputs["source_structure_count"] == 2
        assert inputs["source_bundle_file"].endswith("source_bundle.json")

    def test_prepare_resolver_selects_explicit_source_candidate(self, job_dir):
        from mdclaw.source_bundle import build_source_bundle, write_source_bundle
        from mdclaw.structure.prepare_complex import _resolve_prepare_node_structure_file

        jd = str(job_dir)
        create_node(jd, "source")
        source_node_dir = job_dir / "nodes" / "source_001"
        first = source_node_dir / "artifacts" / "candidate_a.pdb"
        second = source_node_dir / "artifacts" / "candidate_b.pdb"
        first.write_text("HEADER A\n")
        second.write_text("HEADER B\n")
        bundle = build_source_bundle(
            source_type="generated",
            source_id="ensemble",
            structure_paths=[first, second],
            source_node_dir=source_node_dir,
        )
        rel_bundle = write_source_bundle(source_node_dir, bundle)
        complete_node(
            jd,
            "source_001",
            artifacts={
                "structure_file": bundle["structures"][0]["candidate_file"],
                "source_bundle": rel_bundle,
            },
        )
        create_node(jd, "prep", parent_node_ids=["source_001"])

        inputs = _resolve_prepare_node_structure_file(
            jd,
            "prep_001",
            None,
            {"structure_id": "candidate_002"},
        )

        assert inputs["structure_file"].endswith(
            "source_001/artifacts/candidates/candidate_002.pdb"
        )
        assert inputs["source_structure_id"] == "candidate_002"
        assert inputs["source_selection"] == {"structure_id": "candidate_002"}
        assert Path(inputs["source_selection_file"]).is_file()

    def test_prep_omits_structure_file_when_no_source_ancestor(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prep")
        inputs = resolve_node_inputs(jd, "prep_001", "prep")
        assert "structure_file" not in inputs

    def test_rejects_second_source_root(self, job_dir):
        jd = str(job_dir)
        assert create_node(jd, "source")["success"] is True
        result = create_node(jd, "source")
        assert result["success"] is False
        assert "already has a source root" in result["error"]

    def test_rejects_prep_with_multiple_source_lineages(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "source")
        complete_node(jd, "source_001",
                      artifacts={"structure_file": "artifacts/a.pdb"})
        # Simulate a legacy/hand-edited second source lineage in progress.json.
        progress_path = job_dir / "progress.json"
        progress = json.loads(progress_path.read_text())
        progress["nodes"]["source_002"] = {
            "type": "source",
            "status": "completed",
            "parents": [],
        }
        progress_path.write_text(json.dumps(progress))
        result = create_node(jd, "prep", parent_node_ids=["source_001", "source_002"])
        assert result["success"] is False
        assert "multiple source ancestors" in result["error"]

    def test_prep_with_single_source_through_intermediate_ignored(self, job_dir):
        """If only one source ancestor exists, resolve still works even when
        there are non-source siblings on the parent list."""
        jd = str(job_dir)
        create_node(jd, "source")
        (job_dir / "nodes" / "source_001" / "artifacts" / "src.pdb").write_text("X")
        complete_node(jd, "source_001",
                      artifacts={"structure_file": "artifacts/src.pdb"})
        # A second prep without a source parent (e.g. legacy)
        create_node(jd, "prep")
        complete_node(jd, "prep_001",
                      artifacts={"merged_pdb": "artifacts/merged.pdb"})
        # New prep: single source ancestor
        create_node(jd, "prep", parent_node_ids=["source_001"])
        inputs = resolve_node_inputs(jd, "prep_002", "prep")
        assert inputs.get("structure_file", "").endswith("source_001/artifacts/src.pdb")

    def test_single_source_can_branch_into_multiple_preps(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "source")
        (job_dir / "nodes" / "source_001" / "artifacts" / "src.pdb").write_text("X")
        complete_node(
            jd,
            "source_001",
            artifacts={"structure_file": "artifacts/src.pdb"},
        )
        first = create_node(jd, "prep", parent_node_ids=["source_001"], label="protein_only")
        second = create_node(jd, "prep", parent_node_ids=["source_001"], label="protein_ligand")
        assert first["success"] is True
        assert second["success"] is True

        first_inputs = resolve_node_inputs(jd, "prep_001", "prep")
        second_inputs = resolve_node_inputs(jd, "prep_002", "prep")
        assert first_inputs["structure_file"].endswith("source_001/artifacts/src.pdb")
        assert second_inputs["structure_file"].endswith("source_001/artifacts/src.pdb")

    def test_custom_analysis_structured_artifact_paths_resolve(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prod")
        (job_dir / "nodes" / "prod_001" / "artifacts" / "trajectory.dcd").write_text("DCD")
        complete_node(
            jd,
            "prod_001",
            artifacts={"trajectory": "artifacts/trajectory.dcd"},
        )
        create_node(
            jd,
            "analyze",
            parent_node_ids=["prod_001"],
            conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
        )
        artifacts_dir = job_dir / "nodes" / "analyze_001" / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "result.json").write_text("{}")
        (artifacts_dir / "analysis_manifest.json").write_text("{}")
        complete_node(
            jd,
            "analyze_001",
            artifacts={
                "result_json": "artifacts/result.json",
                "analysis_manifest": "artifacts/analysis_manifest.json",
                "report": {"result_json": "artifacts/result.json"},
            },
        )
        create_node(
            jd,
            "analyze",
            parent_node_ids=["analyze_001"],
            conditions=ANALYSIS_PRODUCTION_CHAIN_CONDITIONS,
        )

        result_json = find_ancestor_artifact(
            jd, "analyze_002", "analyze", "result_json"
        )
        manifest = find_ancestor_artifact(
            jd, "analyze_002", "analyze", "analysis_manifest"
        )
        report = find_ancestor_artifact(
            jd, "analyze_002", "analyze", "report"
        )
        assert result_json.endswith("analyze_001/artifacts/result.json")
        assert manifest.endswith("analyze_001/artifacts/analysis_manifest.json")
        assert report["result_json"].endswith("analyze_001/artifacts/result.json")


# ── Tool registration ─────────────────────────────────────────────────────


class TestNodeServerRegistration:

    def test_create_node_in_tools(self):
        from mdclaw.node import TOOLS
        assert "create_node" in TOOLS

    def test_multi_agent_node_tools_registered(self):
        from mdclaw.node import TOOLS
        for tool_name in (
            "rebuild_progress_index",
            "claim_node",
            "release_node_claim",
            "manage_node_need",
        ):
            assert tool_name in TOOLS
        # The open-need verbs were consolidated into manage_node_need.
        for removed in ("add_node_need", "clear_node_need", "record_node_need_attempt"):
            assert removed not in TOOLS

    def test_registry_has_node(self):
        from mdclaw._registry import SERVER_REGISTRY
        assert "node" in SERVER_REGISTRY
        assert SERVER_REGISTRY["node"] == "mdclaw.node"
