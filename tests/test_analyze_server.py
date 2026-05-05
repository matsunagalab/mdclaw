"""Tests for analyze server registration and lightweight analyses."""

import json

import numpy as np
import pytest

from mdclaw.analyze_server import (
    analyze_contact_frequency,
    analyze_rmsf,
    register_analysis_result,
)


def _write_artifact(job_dir, node_id, rel_path, content="x\n"):
    path = job_dir / "nodes" / node_id / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _make_analysis_node(job_dir):
    from mdclaw._node import complete_node, create_node

    create_node(str(job_dir), "prod")
    _write_artifact(job_dir, "prod_001", "artifacts/trajectory.dcd")
    complete_node(
        str(job_dir),
        "prod_001",
        artifacts={"trajectory": "artifacts/trajectory.dcd"},
    )
    create_node(str(job_dir), "analyze", parent_node_ids=["prod_001"])


def test_register_analysis_result_from_manifest(tmp_path):
    job_dir = tmp_path / "job"
    _make_analysis_node(job_dir)
    _write_artifact(job_dir, "analyze_001", "artifacts/result.json", "{}\n")
    _write_artifact(job_dir, "analyze_001", "artifacts/contacts.csv", "frame,value\n")
    manifest = {
        "analysis_type": "custom",
        "name": "ligand_contacts",
        "summary": "Contacts remained stable.",
        "metrics": {"mean_contact_occupancy": 0.75, "n_frames": 20},
        "artifacts": {
            "result_json": "artifacts/result.json",
            "contact_csv": "artifacts/contacts.csv",
        },
        "method": {"library": "mdtraj", "cutoff_nm": 0.45},
        "provenance": {"input_node_id": "analyze_000"},
        "producer_agent": "test-agent",
    }
    manifest_path = _write_artifact(
        job_dir,
        "analyze_001",
        "artifacts/analysis_manifest.json",
        json.dumps(manifest),
    )

    result = register_analysis_result(
        str(job_dir),
        "analyze_001",
        manifest_file=str(manifest_path),
    )

    assert result["success"] is True
    node = json.loads((job_dir / "nodes" / "analyze_001" / "node.json").read_text())
    assert node["status"] == "completed"
    assert node["artifacts"]["result_json"] == "artifacts/result.json"
    assert node["artifacts"]["analysis_manifest"] == "artifacts/analysis_manifest.json"
    assert node["metadata"]["analysis_name"] == "ligand_contacts"
    assert node["metadata"]["metrics"]["mean_contact_occupancy"] == 0.75
    assert "artifact_sha256" in node["metadata"]


def test_register_analysis_result_fails_on_missing_artifact(tmp_path):
    job_dir = tmp_path / "job"
    _make_analysis_node(job_dir)
    manifest_path = _write_artifact(
        job_dir,
        "analyze_001",
        "artifacts/analysis_manifest.json",
        json.dumps({"artifacts": {"missing": "artifacts/missing.csv"}}),
    )

    result = register_analysis_result(
        str(job_dir),
        "analyze_001",
        manifest_file=str(manifest_path),
    )

    assert result["success"] is False
    node = json.loads((job_dir / "nodes" / "analyze_001" / "node.json").read_text())
    assert node["status"] == "failed"
    assert "missing.csv" in node["metadata"]["errors"][0]


@pytest.fixture
def tiny_mdtraj_inputs(tmp_path, alanine_dipeptide_pdb):
    md = pytest.importorskip("mdtraj")
    pytest.importorskip("matplotlib")

    top = md.load_pdb(alanine_dipeptide_pdb)
    xyz = np.repeat(top.xyz, 4, axis=0)
    xyz[1:, :, 0] += np.linspace(0.01, 0.03, 3)[:, None]
    traj = md.Trajectory(xyz=xyz, topology=top.topology)
    dcd_path = tmp_path / "tiny.dcd"
    pdb_path = tmp_path / "tiny.pdb"
    traj.save_dcd(str(dcd_path))
    top.save_pdb(str(pdb_path))
    return str(dcd_path), str(pdb_path)


def test_analyze_rmsf_direct_writes_artifacts(tmp_path, tiny_mdtraj_inputs):
    dcd_path, pdb_path = tiny_mdtraj_inputs

    result = analyze_rmsf(
        trajectory_file=dcd_path,
        reference_pdb=pdb_path,
        selection="all",
        align_selection="all",
        by_residue=False,
        output_name="rmsf_test",
        _out_dir_override=str(tmp_path / "rmsf"),
    )

    assert result["success"] is True
    assert result["n_frames"] == 4
    assert result["mean_rmsf_nm"] >= 0.0
    assert (tmp_path / "rmsf" / "rmsf_test.npy").is_file()
    assert (tmp_path / "rmsf" / "rmsf_test.csv").is_file()
    assert (tmp_path / "rmsf" / "rmsf_test.png").is_file()


def test_analyze_contact_frequency_direct_writes_artifacts(tmp_path, tiny_mdtraj_inputs):
    dcd_path, pdb_path = tiny_mdtraj_inputs

    result = analyze_contact_frequency(
        trajectory_file=dcd_path,
        reference_pdb=pdb_path,
        selection_group1="all",
        cutoff_nm=0.5,
        mode="residue",
        by_residue=True,
        output_name="contacts_test",
        _out_dir_override=str(tmp_path / "contacts"),
    )

    assert result["success"] is True
    assert result["n_frames"] == 4
    assert result["max_contact_frequency"] >= 0.0
    assert (tmp_path / "contacts" / "contacts_test.npy").is_file()
    assert (tmp_path / "contacts" / "contacts_test.csv").is_file()
    assert (tmp_path / "contacts" / "contacts_test.png").is_file()
