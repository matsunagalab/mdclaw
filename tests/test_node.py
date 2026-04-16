"""Tests for node-based job graph management (schema v3).

Covers: _lock.py, _node.py lifecycle, node_server.py registration.
"""

import json
from pathlib import Path

import pytest

from mdclaw._node import (
    SCHEMA_VERSION,
    begin_node,
    complete_node,
    create_node,
    fail_node,
    find_nodes,
    get_ancestors,
    get_children,
    init_progress_v3,
    read_node,
    resolve_artifact,
    schema_major,
    update_job_summaries,
    update_node,
    update_node_status,
)


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


# ── schema_major ───────────────────────────────────────────────────────────


class TestSchemaMajor:

    def test_no_progress_returns_3(self, tmp_path):
        assert schema_major(str(tmp_path)) == SCHEMA_VERSION

    def test_v2_string(self, job_dir):
        (job_dir / "progress.json").write_text(
            json.dumps({"schema_version": "2.0"})
        )
        assert schema_major(str(job_dir)) == 2

    def test_v3_int(self, job_dir):
        init_progress_v3(str(job_dir))
        assert schema_major(str(job_dir)) == 3


# ── create_node ────────────────────────────────────────────────────────────


class TestCreateNode:

    def test_basic_creation(self, job_dir):
        result = create_node(str(job_dir), "prep")
        assert result["success"] is True
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


# ── update_node / update_node_status ───────────────────────────────────────


class TestUpdateNode:

    def test_merge_dict(self, job_dir):
        create_node(str(job_dir), "prep")
        update_node(str(job_dir), "prep_001", {
            "artifacts": {"merged_pdb": "artifacts/merged.pdb"}
        })
        update_node(str(job_dir), "prep_001", {
            "artifacts": {"ligand_params": "artifacts/ligand_params.json"}
        })
        node = read_node(str(job_dir), "prep_001")
        assert node["artifacts"]["merged_pdb"] == "artifacts/merged.pdb"
        assert node["artifacts"]["ligand_params"] == "artifacts/ligand_params.json"

    def test_append_warnings(self, job_dir):
        create_node(str(job_dir), "prep")
        update_node(str(job_dir), "prep_001", {"warnings": ["w1"]})
        update_node(str(job_dir), "prep_001", {"warnings": ["w2"]})
        node = read_node(str(job_dir), "prep_001")
        assert node["warnings"] == ["w1", "w2"]

    def test_overwrite_scalar(self, job_dir):
        create_node(str(job_dir), "prep")
        update_node(str(job_dir), "prep_001", {"status": "running"})
        node = read_node(str(job_dir), "prep_001")
        assert node["status"] == "running"


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
        result = create_node(str(job_dir), "eq")
        path = resolve_artifact(str(job_dir), "eq_001", "artifacts/equilibrated.chk")
        expected = job_dir / "nodes" / "eq_001" / "artifacts" / "equilibrated.chk"
        assert path == expected.resolve()


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


# ── Tool registration ─────────────────────────────────────────────────────


class TestNodeServerRegistration:

    def test_create_node_in_tools(self):
        from mdclaw.node_server import TOOLS
        assert "create_node" in TOOLS

    def test_registry_has_node(self):
        from mdclaw._registry import SERVER_REGISTRY
        assert "node" in SERVER_REGISTRY
        assert SERVER_REGISTRY["node"] == "mdclaw.node_server"
