"""Tests for progress.json auto-update from tool results.

progress.json is the source of truth for job state.  These tests verify
that the _progress.py helpers correctly create and merge progress data
after each tool in the pipeline.
"""

import json

import pytest

from mdclaw._progress import update_progress_from_result, _merge_progress


# ---------------------------------------------------------------------------
# Fixtures: synthetic tool result dicts
# ---------------------------------------------------------------------------

@pytest.fixture
def job_dir(tmp_path):
    """A clean job directory."""
    d = tmp_path / "job_test1234"
    d.mkdir()
    return d


@pytest.fixture
def prepare_result(job_dir):
    """Synthetic prepare_complex result (success)."""
    split_dir = job_dir / "split"
    split_dir.mkdir()
    return {
        "success": True,
        "job_id": "test1234",
        "output_dir": str(split_dir),
        "source_file": "1AKE.pdb",
        "overall_status": "success",
        "merged_pdb": str(job_dir / "merge" / "merged.pdb"),
        "proteins": [
            {"chain_id": "Axp", "success": True, "statistics": {"final_residues": 214}},
        ],
        "ligands": [
            {
                "ligand_id": "AP5", "success": True,
                "mol2_file": str(split_dir / "AP5.geostd.mol2"),
                "frcmod_file": str(split_dir / "AP5.geostd.frcmod"),
                "parameter_source": "amber_geostd",
            },
        ],
        "preparation_summary": {"protonation_method": "pdb2pqr+propka", "protonation_ph": 7.4},
        "warnings": [],
    }


@pytest.fixture
def solvate_result(job_dir):
    """Synthetic solvate_structure result (success)."""
    return {
        "success": True,
        "output_file": str(job_dir / "solvate" / "solvated.pdb"),
        "output_dir": str(job_dir / "solvate"),
        "parameters": {"dist": 15.0, "saltcon": 0.15, "water_model": "opc"},
        "statistics": {"total_atoms": 49917},
        "box_dimensions": {"box_a": 77.8, "box_b": 77.8, "box_c": 77.8, "is_cubic": True},
    }


@pytest.fixture
def topology_result(job_dir):
    """Synthetic build_amber_system result (success)."""
    return {
        "success": True,
        "parm7": str(job_dir / "topology" / "system.parm7"),
        "rst7": str(job_dir / "topology" / "system.rst7"),
        "forcefield": "ff19SB",
        "water_model": "opc",
        "output_dir": str(job_dir / "topology"),
    }


# ---------------------------------------------------------------------------
# Tests: prepare_complex creates progress.json
# ---------------------------------------------------------------------------

class TestPrepareProgress:

    def test_creates_progress_json(self, job_dir, prepare_result):
        update_progress_from_result("prepare_complex", prepare_result, prepare_result["output_dir"])
        pj = job_dir / "progress.json"
        assert pj.exists()
        data = json.loads(pj.read_text())
        assert data["schema_version"] == "2.0"
        assert data["job_id"] == "test1234"
        assert data["status"] == "success"
        assert "prepare" in data["completed_steps"]
        assert data["current_step"] == "prepare"

    def test_artifacts_populated(self, job_dir, prepare_result):
        update_progress_from_result("prepare_complex", prepare_result, prepare_result["output_dir"])
        data = json.loads((job_dir / "progress.json").read_text())
        assert data["artifacts"]["merged_pdb"] == prepare_result["merged_pdb"]
        assert len(data["artifacts"]["ligand_params"]) == 1
        assert data["artifacts"]["ligand_params"][0]["parameter_source"] == "amber_geostd"

    def test_system_info(self, job_dir, prepare_result):
        update_progress_from_result("prepare_complex", prepare_result, prepare_result["output_dir"])
        data = json.loads((job_dir / "progress.json").read_text())
        assert data["system"]["num_residues"] == 214
        assert "AP5" in data["system"]["ligands"]

    def test_preparation_summary_copied(self, job_dir, prepare_result):
        update_progress_from_result("prepare_complex", prepare_result, prepare_result["output_dir"])
        data = json.loads((job_dir / "progress.json").read_text())
        assert data["preparation"]["protonation_method"] == "pdb2pqr+propka"

    def test_next_step_set(self, job_dir, prepare_result):
        update_progress_from_result("prepare_complex", prepare_result, prepare_result["output_dir"])
        data = json.loads((job_dir / "progress.json").read_text())
        assert data["next_step"]["skill"] == "solvation"

    def test_blocking_ligand_failure(self, job_dir, prepare_result):
        prepare_result["success"] = True  # protein ok
        prepare_result["overall_status"] = "completed_with_blocking_ligand_failure"
        prepare_result["workflow_recommendation"] = {
            "blocking_ligands": [{"ligand_id": "AP5", "recommended_next_action": "use_curated_params"}],
            "options": ["provide_curated_params_and_rerun", "stop"],
        }
        prepare_result["ligands"][0]["success"] = False
        prepare_result["ligands"][0].pop("mol2_file")

        update_progress_from_result("prepare_complex", prepare_result, prepare_result["output_dir"])
        data = json.loads((job_dir / "progress.json").read_text())
        assert data["status"] == "completed_with_blocking_ligand_failure"
        assert data["blocking"] is not None
        assert data["next_step"] is None
        # prepare itself completed — the blocking is a ligand-level issue
        assert "prepare" in data["completed_steps"]


# ---------------------------------------------------------------------------
# Tests: solvate_structure merges into existing
# ---------------------------------------------------------------------------

class TestSolvateProgress:

    def test_merges_solvation(self, job_dir, prepare_result, solvate_result):
        # First create progress.json via prepare
        update_progress_from_result("prepare_complex", prepare_result, prepare_result["output_dir"])
        # Then merge solvation
        update_progress_from_result("solvate_structure", solvate_result, solvate_result["output_dir"])

        data = json.loads((job_dir / "progress.json").read_text())
        assert "solvate" in data["completed_steps"]
        assert data["solvation"]["water_model"] == "opc"
        assert data["solvation"]["box_size_angstrom"] == [77.8, 77.8, 77.8]
        assert data["system"]["num_atoms_total"] == 49917
        assert data["artifacts"]["solvated_pdb"] == solvate_result["output_file"]
        assert data["next_step"]["skill"] == "topology"

    def test_preserves_prepare_fields(self, job_dir, prepare_result, solvate_result):
        update_progress_from_result("prepare_complex", prepare_result, prepare_result["output_dir"])
        update_progress_from_result("solvate_structure", solvate_result, solvate_result["output_dir"])

        data = json.loads((job_dir / "progress.json").read_text())
        # prepare fields preserved
        assert data["artifacts"]["merged_pdb"] == prepare_result["merged_pdb"]
        assert data["preparation"]["protonation_method"] == "pdb2pqr+propka"
        assert data["system"]["num_residues"] == 214


# ---------------------------------------------------------------------------
# Tests: build_amber_system merges topology
# ---------------------------------------------------------------------------

class TestTopologyProgress:

    def test_sets_next_step(self, job_dir, prepare_result, solvate_result, topology_result):
        update_progress_from_result("prepare_complex", prepare_result, prepare_result["output_dir"])
        update_progress_from_result("solvate_structure", solvate_result, solvate_result["output_dir"])
        update_progress_from_result("build_amber_system", topology_result, topology_result["output_dir"])

        data = json.loads((job_dir / "progress.json").read_text())
        assert data["next_step"]["skill"] == "md-equilibration"
        assert data["artifacts"]["parm7"] == topology_result["parm7"]
        assert data["artifacts"]["rst7"] == topology_result["rst7"]
        assert data["forcefield"]["protein"] == "ff19SB"


# ---------------------------------------------------------------------------
# Tests: completed_steps accumulate correctly
# ---------------------------------------------------------------------------

class TestStepsAccumulate:

    def test_three_steps(self, job_dir, prepare_result, solvate_result, topology_result):
        update_progress_from_result("prepare_complex", prepare_result, prepare_result["output_dir"])
        update_progress_from_result("solvate_structure", solvate_result, solvate_result["output_dir"])
        update_progress_from_result("build_amber_system", topology_result, topology_result["output_dir"])

        data = json.loads((job_dir / "progress.json").read_text())
        assert data["completed_steps"] == ["prepare", "solvate", "topology"]
        assert data["current_step"] == "topology"


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_unknown_tool_noop(self, job_dir):
        """Non-pipeline tools don't touch progress.json."""
        update_progress_from_result("inspect_molecules", {"success": True}, str(job_dir))
        assert not (job_dir / "progress.json").exists()

    def test_failure_records_status_not_completed(self, job_dir, prepare_result, solvate_result):
        """Failed solvation records status but doesn't add to completed_steps."""
        update_progress_from_result("prepare_complex", prepare_result, prepare_result["output_dir"])

        solvate_result["success"] = False
        solvate_result["errors"] = ["packmol failed"]
        update_progress_from_result("solvate_structure", solvate_result, solvate_result["output_dir"])

        data = json.loads((job_dir / "progress.json").read_text())
        assert "solvate" not in data["completed_steps"]
        assert data["status"] == "solvation_failed"
        assert "packmol failed" in data["warnings"]

    def test_merge_preserves_existing(self, job_dir):
        """_merge_progress doesn't delete existing keys."""
        pj = job_dir / "progress.json"
        pj.write_text(json.dumps({
            "schema_version": "2.0",
            "custom_field": "keep_me",
            "artifacts": {"structure_file": "1AKE.pdb", "parm7": None},
        }))

        _merge_progress(job_dir, {
            "artifacts": {"parm7": "/path/to/system.parm7"},
            "status": "topology_done",
        })

        data = json.loads(pj.read_text())
        assert data["custom_field"] == "keep_me"
        assert data["artifacts"]["structure_file"] == "1AKE.pdb"
        assert data["artifacts"]["parm7"] == "/path/to/system.parm7"

    def test_duplicate_steps_not_added(self, job_dir):
        """Calling the same step twice doesn't duplicate completed_steps."""
        pj = job_dir / "progress.json"
        pj.write_text(json.dumps({"completed_steps": ["prepare"]}))

        _merge_progress(job_dir, {"completed_steps": ["prepare"]})

        data = json.loads(pj.read_text())
        assert data["completed_steps"] == ["prepare"]

    def test_runs_upsert(self, job_dir):
        """runs[] updates existing entries by run_id, appends new ones."""
        pj = job_dir / "progress.json"
        pj.write_text(json.dumps({
            "runs": [{"run_id": "run_001_300K", "status": "equilibrated"}],
        }))

        _merge_progress(job_dir, {
            "runs": [
                {"run_id": "run_001_300K", "status": "completed", "trajectory": "/t.dcd"},
                {"run_id": "run_002_310K", "status": "pending"},
            ],
        })

        data = json.loads(pj.read_text())
        assert len(data["runs"]) == 2
        assert data["runs"][0]["run_id"] == "run_001_300K"
        assert data["runs"][0]["status"] == "completed"
        assert data["runs"][0]["trajectory"] == "/t.dcd"
        assert data["runs"][1]["run_id"] == "run_002_310K"
