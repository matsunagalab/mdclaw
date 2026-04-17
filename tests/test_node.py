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
    find_ancestor_artifact,
    find_nodes,
    get_ancestors,
    get_children,
    init_progress_v3,
    read_node,
    resolve_artifact,
    resolve_node_inputs,
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
                      artifacts={"parm7": "artifacts/system.parm7",
                                 "rst7": "artifacts/system.rst7"})

        create_node(jd, "eq", parent_node_ids=["topo_001"])
        complete_node(jd, "eq_001",
                      artifacts={"checkpoint": "artifacts/equilibrated.chk",
                                 "final_structure": "artifacts/equilibrated.pdb"})

        create_node(jd, "prod", parent_node_ids=["eq_001"])
        return job_dir

    def test_find_ancestor_parm7_from_eq(self, full_dag):
        jd = str(full_dag)
        result = find_ancestor_artifact(jd, "eq_001", "topo", "parm7")
        assert result is not None
        assert result.endswith("topo_001/artifacts/system.parm7")

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
        """prod_001 -> eq_001 -> topo_001: parm7 is 2 hops away."""
        jd = str(full_dag)
        result = find_ancestor_artifact(jd, "prod_001", "topo", "parm7")
        assert result is not None
        assert "topo_001" in result

    def test_find_ancestor_missing_returns_none(self, full_dag):
        jd = str(full_dag)
        result = find_ancestor_artifact(jd, "prep_001", "topo", "parm7")
        assert result is None

    def test_resolve_node_inputs_eq(self, full_dag):
        jd = str(full_dag)
        inputs = resolve_node_inputs(jd, "eq_001", "eq")
        assert "prmtop_file" in inputs
        assert "inpcrd_file" in inputs
        assert inputs["prmtop_file"].endswith("system.parm7")
        assert inputs["inpcrd_file"].endswith("system.rst7")

    def test_resolve_node_inputs_prod(self, full_dag):
        jd = str(full_dag)
        inputs = resolve_node_inputs(jd, "prod_001", "prod")
        assert "prmtop_file" in inputs
        assert "inpcrd_file" in inputs
        assert "restart_from" in inputs
        assert inputs["restart_from"].endswith("equilibrated.chk")

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


# ── Structured (non-path) artifact propagation ─────────────────────────────


class TestStructuredArtifactPropagation:
    """Covers the DAG-based propagation of ``ligand_params`` / ``metal_params``
    / ``box_dimensions`` from prep/solv ancestors to the topo node.
    """

    @pytest.fixture
    def dag_with_ligand(self, job_dir):
        """prep (with ligand_params) -> solv (with box_dimensions.json) -> topo."""
        jd = str(job_dir)
        ligand_params = [
            {
                "mol2": "/abs/path/to/AP5.mol2",
                "frcmod": "/abs/path/to/AP5.frcmod",
                "residue_name": "AP5",
                "parameter_source": "amber_geostd",
            }
        ]

        create_node(jd, "prep")
        complete_node(
            jd,
            "prep_001",
            artifacts={
                "merged_pdb": "artifacts/merge/merged.pdb",
                "ligand_params": ligand_params,
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
        return job_dir, ligand_params, box

    def test_find_ancestor_returns_str_for_path_artifact(self, dag_with_ligand):
        """Contract: string-valued artifacts are resolved to abs paths."""
        job_dir, _lp, _box = dag_with_ligand
        result = find_ancestor_artifact(str(job_dir), "topo_001", "solv",
                                        "solvated_pdb")
        assert isinstance(result, str)
        assert result.endswith("solv_001/artifacts/solvated.pdb")

    def test_find_ancestor_returns_list_for_structured_artifact(self,
                                                                dag_with_ligand):
        """Contract: list/dict artifacts are returned as-is."""
        job_dir, lp, _box = dag_with_ligand
        result = find_ancestor_artifact(str(job_dir), "topo_001", "prep",
                                        "ligand_params")
        assert isinstance(result, list)
        assert result == lp  # absolute paths preserved, no path-join applied

    def test_find_ancestor_missing_structured(self, dag_with_ligand):
        """Absent structured artifact still returns None."""
        job_dir, _lp, _box = dag_with_ligand
        result = find_ancestor_artifact(str(job_dir), "topo_001", "prep",
                                        "metal_params")
        assert result is None

    def test_resolve_topo_inputs_three_level_dag(self, dag_with_ligand):
        """prep grandparent -> solv parent -> topo child: ligand_params
        must be propagated all the way to topo."""
        job_dir, lp, box = dag_with_ligand
        inputs = resolve_node_inputs(str(job_dir), "topo_001", "topo")

        # pdb_file from solv parent
        assert "pdb_file" in inputs
        assert inputs["pdb_file"].endswith("solvated.pdb")

        # ligand_params from prep grandparent (structured pass-through)
        assert "ligand_params" in inputs
        assert inputs["ligand_params"] == lp

        # box_dimensions loaded inline from solv's JSON file
        assert "box_dimensions" in inputs
        assert inputs["box_dimensions"] == box

        # metal_params absent → key omitted (not None)
        assert "metal_params" not in inputs

    def test_resolve_topo_omits_keys_when_prep_has_no_params(self, job_dir):
        """If prep never wrote ligand/metal params, resolve_node_inputs
        must silently omit them (not surface as None)."""
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
        assert "ligand_params" not in inputs
        assert "metal_params" not in inputs
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


# ── Fetch node (DAG root) ──────────────────────────────────────────────────


class TestFetchNode:
    """Fetch is the DAG-root node type for structure acquisition."""

    def test_fetch_is_valid_node_type(self, job_dir):
        result = create_node(str(job_dir), "fetch")
        assert result["success"] is True
        assert result["node_id"] == "fetch_001"

    def test_fetch_as_dag_root_no_parent(self, job_dir):
        result = create_node(str(job_dir), "fetch")
        node = read_node(str(job_dir), result["node_id"])
        assert node["parent_node_ids"] == []

    def test_fetch_rejects_parent_node_ids(self, job_dir):
        """fetch is the DAG root — parents are forbidden by invariant."""
        jd = str(job_dir)
        # Build a valid existing node first (so the rejection isn't from
        # a missing-reference error)
        create_node(jd, "fetch")
        result = create_node(jd, "fetch", parent_node_ids=["fetch_001"])
        assert result["success"] is False
        assert "DAG root" in result["error"]
        assert "parent_node_ids" in result["error"]
        # Index unchanged: only the original fetch_001 exists
        progress = json.loads((job_dir / "progress.json").read_text())
        assert list(progress["nodes"].keys()) == ["fetch_001"]

    def test_fetch_rejects_dependency_node_ids(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "fetch")
        result = create_node(jd, "fetch", dependency_node_ids=["fetch_001"])
        assert result["success"] is False
        assert "DAG root" in result["error"]
        assert "dependency_node_ids" in result["error"]
        progress = json.loads((job_dir / "progress.json").read_text())
        assert list(progress["nodes"].keys()) == ["fetch_001"]

    def test_fetch_lifecycle(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "fetch", label="PDB 1AKE")
        begin_node(jd, "fetch_001")
        complete_node(
            jd,
            "fetch_001",
            artifacts={"structure_file": "artifacts/1AKE.pdb"},
            metadata={
                "source_type": "pdb",
                "source_id": "1AKE",
                "sha256": "deadbeef",
            },
        )
        node = read_node(jd, "fetch_001")
        assert node["status"] == "completed"
        assert node["label"] == "PDB 1AKE"
        assert node["artifacts"]["structure_file"] == "artifacts/1AKE.pdb"
        assert node["metadata"]["source_type"] == "pdb"

    def test_prep_resolves_structure_file_from_fetch(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "fetch")
        # Create the actual file so resolve gives a usable path
        (job_dir / "nodes" / "fetch_001" / "artifacts" / "1AKE.pdb").write_text("HEADER\n")
        complete_node(
            jd,
            "fetch_001",
            artifacts={"structure_file": "artifacts/1AKE.pdb"},
        )
        create_node(jd, "prep", parent_node_ids=["fetch_001"])
        inputs = resolve_node_inputs(jd, "prep_001", "prep")
        assert "structure_file" in inputs
        assert inputs["structure_file"].endswith("fetch_001/artifacts/1AKE.pdb")

    def test_prep_omits_structure_file_when_no_fetch_ancestor(self, job_dir):
        jd = str(job_dir)
        create_node(jd, "prep")
        inputs = resolve_node_inputs(jd, "prep_001", "prep")
        assert "structure_file" not in inputs

    def test_prep_omits_when_multiple_fetch_ancestors(self, job_dir):
        """v1 contract: multi-fetch -> prep is unsupported; auto-resolve
        returns empty so prepare_complex falls back to explicit --structure-file."""
        jd = str(job_dir)
        create_node(jd, "fetch")
        complete_node(jd, "fetch_001",
                      artifacts={"structure_file": "artifacts/a.pdb"})
        create_node(jd, "fetch")
        complete_node(jd, "fetch_002",
                      artifacts={"structure_file": "artifacts/b.pdb"})
        create_node(jd, "prep", parent_node_ids=["fetch_001", "fetch_002"])
        inputs = resolve_node_inputs(jd, "prep_001", "prep")
        assert "structure_file" not in inputs

    def test_prep_with_single_fetch_through_intermediate_ignored(self, job_dir):
        """If only one fetch ancestor exists, resolve still works even when
        there are non-fetch siblings on the parent list."""
        jd = str(job_dir)
        create_node(jd, "fetch")
        (job_dir / "nodes" / "fetch_001" / "artifacts" / "src.pdb").write_text("X")
        complete_node(jd, "fetch_001",
                      artifacts={"structure_file": "artifacts/src.pdb"})
        # A second prep without a fetch parent (e.g. legacy)
        create_node(jd, "prep")
        complete_node(jd, "prep_001",
                      artifacts={"merged_pdb": "artifacts/merged.pdb"})
        # New prep: single fetch ancestor
        create_node(jd, "prep", parent_node_ids=["fetch_001"])
        inputs = resolve_node_inputs(jd, "prep_002", "prep")
        assert inputs.get("structure_file", "").endswith("fetch_001/artifacts/src.pdb")


# ── Tool registration ─────────────────────────────────────────────────────


class TestNodeServerRegistration:

    def test_create_node_in_tools(self):
        from mdclaw.node_server import TOOLS
        assert "create_node" in TOOLS

    def test_registry_has_node(self):
        from mdclaw._registry import SERVER_REGISTRY
        assert "node" in SERVER_REGISTRY
        assert SERVER_REGISTRY["node"] == "mdclaw.node_server"
