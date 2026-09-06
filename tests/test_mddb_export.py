"""Offline bundle integration tests using small synthetic (not simulated) DCDs."""

import json
import subprocess
import sys

import mdtraj as md
import numpy as np
import pytest
import yaml

from mdclaw.evidence import export_mddb


METADATA = dict(name="Synthetic export test", authors=["Test fixture"],
                contact="test@example.invalid", license="Synthetic test data only",
                linkcense="https://example.invalid/license", method="Synthetic trajectory fixture")


def node(job, nid, kind, parents=(), artifacts=None, metadata=None):
    path = job / "nodes" / nid / "node.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(node_id=nid, node_type=kind, status="completed",
                                   parent_node_ids=list(parents), dependency_node_ids=[],
                                   conditions={}, metadata=metadata or {}, artifacts=artifacts or {})))
    return path.parent


def fixture_job(job, ligand="LIG"):
    top = md.Topology()
    chain = top.add_chain("A")
    for name, atoms in [("HIS", [("N", md.element.nitrogen), ("CA", md.element.carbon)]),
                        (ligand, [("C1", md.element.carbon), ("O1", md.element.oxygen)]),
                        ("POP", [("C1", md.element.carbon), ("C2", md.element.carbon)]),
                        ("HOH", [("O", md.element.oxygen), ("H1", md.element.hydrogen), ("H2", md.element.hydrogen)]),
                        ("NA", [("NA", md.element.sodium)]), ("ZN", [("ZN", md.element.zinc)])]:
        residue = top.add_residue(name, chain)
        added = [top.add_atom(n, e, residue) for n, e in atoms]
        for atom in added[1:]:
            top.add_bond(added[0], atom)
    xyz = np.arange(7 * top.n_atoms * 3, dtype=np.float32).reshape(7, top.n_atoms, 3) / 1000
    traj = md.Trajectory(xyz, top, unitcell_lengths=np.ones((7, 3)) * 3,
                         unitcell_angles=np.ones((7, 3)) * 90)
    topo = node(job, "topo", "topo", artifacts={"topology_pdb": "system.pdb"},
                metadata={"effective_forcefield": "ff14SB", "water_model": "tip3p"})
    traj[0].save_pdb(str(topo / "system.pdb"))
    pdb = topo / "system.pdb"
    pdb.write_text(pdb.read_text().replace("HIS", "HIE"))
    add_prod(job, traj, "prod")
    return traj


def add_prod(job, traj, nid):
    prod = node(job, nid, "prod", ["topo"], artifacts={"trajectory": "trajectory.dcd", "energy": "energy.csv"},
                metadata={"temperature_kelvin": 300, "timestep_fs": 4, "output_frequency_ps": 10,
                          "system_signature": {"ensemble": "NPT"}})
    traj.save_dcd(str(prod / "trajectory.dcd"))
    (prod / "energy.csv").write_text('"Step","Time (ps)"\n' + ''.join(f'{i * 2500},{i * 10}\n' for i in range(1, 8)))
    return prod


def target(job, nid="prod"):
    return dict(job_dir=str(job), node_id=nid, label=f"{job.name}-{nid}")


def export(tmp_path, **kwargs):
    return export_mddb(str(tmp_path / "bundle"), metadata=METADATA, **kwargs)


@pytest.mark.parametrize("stride,chunk", [(1, 2), (2, 2), (3, 1), (10, 100)])
def test_paired_export_roundtrip_and_identity(tmp_path, stride, chunk):
    job = tmp_path / "job"
    traj = fixture_job(job)
    before = {p: p.read_bytes() for p in job.rglob("*") if p.is_file()}
    result = export(tmp_path, job_dir=str(job), stride=stride, chunk=chunk)
    assert result["success"], result
    run = result["manifest"]["runs"][0]
    assert run["atom_indices"] == [0, 1, 2, 3, 4, 5, 10]  # retain lipid/ligand/Zn
    root = tmp_path / "bundle" / "project"
    pdb, dcd = root / "md_001/system.pdb", root / "md_001/trajectory.dcd"
    converted = md.load(str(dcd), top=str(pdb))
    np.testing.assert_allclose(converted.xyz, traj.xyz[::stride, run["atom_indices"]], atol=1e-6)
    np.testing.assert_allclose(converted.unitcell_lengths, traj.unitcell_lengths[::stride])
    assert "HIE" in pdb.read_text()
    inputs = yaml.safe_load((root / "inputs.yaml").read_text())
    assert inputs["input_topology_filepath"] == "no"
    assert inputs["mds"][0]["framestep"] == pytest.approx(0.01 * stride)
    assert inputs["mds"][0]["timestep"] == 4
    assert inputs["mds"][0]["ff"] == ["ff14SB"]
    assert (root / inputs["mds"][0]["input_structure_filepath"]).is_file()
    assert {p: p.read_bytes() for p in job.rglob("*") if p.is_file()} == before
    assert (tmp_path / "bundle/report.json").is_file()
    assert (tmp_path / "bundle/references.bib").is_file()
    assert not export(tmp_path, job_dir=str(job))["success"]  # no overwrite


def test_metadata_and_leaf_decisions_before_writing(tmp_path):
    job = tmp_path / "job"
    traj = fixture_job(job)
    result = export_mddb(str(tmp_path / "bundle"), job_dir=str(job))
    assert result["code"] == "mddb_metadata_required"
    add_prod(job, traj, "replica")
    assert export(tmp_path, job_dir=str(job))["code"] == "report_selection_required"
    assert not (tmp_path / "bundle").exists()


def test_replicas_remain_separate_trajectories(tmp_path):
    job = tmp_path / "job"
    traj = fixture_job(job)
    add_prod(job, traj, "replica")
    result = export(tmp_path, targets=[target(job), target(job, "replica")], grouping="replicas")
    assert result["success"], result
    inputs = yaml.safe_load((tmp_path / "bundle/project/inputs.yaml").read_text())
    assert len(inputs["mds"]) == 2
    assert [r["n_frames"] for r in result["manifest"]["runs"]] == [7, 7]
    assert inputs["mdref"] == 0


def test_incompatible_replicas_must_be_separate_projects(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    fixture_job(a)
    fixture_job(b, ligand="ALT")
    targets = [target(a), target(b)]
    failed = export(tmp_path, targets=targets, grouping="replicas")
    assert not failed["success"] and "topology differs" in str(failed)
    assert not (tmp_path / "bundle").exists()
    result = export(tmp_path, targets=targets, grouping="separate")
    assert result["success"], result
    assert len(result["inputs_files"]) == 2


@pytest.mark.parametrize("selection,success", [("resname LIG", True), ("water", False), ("all", False), ("name ABSENT", False)])
def test_explicit_selection(tmp_path, selection, success):
    job = tmp_path / "job"
    fixture_job(job)
    result = export(tmp_path, job_dir=str(job), selection=selection)
    assert result["success"] is success, result
    if success:
        assert result["manifest"]["runs"][0]["atom_indices"] == [2, 3]
    else:
        assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("damage", ["atom_count", "topology_hash", "energy_count", "irregular", "frame_count", "empty", "multimodel"])
def test_invalid_sources_fail_without_bundle(tmp_path, damage):
    job = tmp_path / "job"
    traj = fixture_job(job)
    prod = job / "nodes/prod"
    data = json.loads((prod / "node.json").read_text())
    if damage == "atom_count":
        traj.atom_slice([0, 1]).save_dcd(str(prod / "trajectory.dcd"))
    elif damage == "topology_hash":
        data["metadata"]["system_signature"]["topology_pdb_sha256"] = "wrong"
    elif damage == "energy_count":
        (prod / "energy.csv").write_text('"Time (ps)"\n10\n20\n')
    elif damage in ("irregular", "frame_count"):
        data["artifacts"]["frame_times_ns"] = "times.npy"
        np.save(prod / "times.npy", [0.01, 0.02, 0.04] if damage == "irregular" else [0.01, 0.02])
    elif damage == "empty":
        traj[:0].save_dcd(str(prod / "trajectory.dcd"))
    elif damage == "multimodel":
        traj[:2].save_pdb(str(job / "nodes/topo/system.pdb"))
    (prod / "node.json").write_text(json.dumps(data))
    result = export(tmp_path, job_dir=str(job))
    assert not result["success"], result
    assert not (tmp_path / "bundle").exists()


def test_analysis_target_resolves_unique_ancestor_not_dependencies(tmp_path):
    job = tmp_path / "job"
    fixture_job(job)
    node(job, "analysis", "analyze", ["prod"])
    result = export(tmp_path, job_dir=str(job))
    assert result["success"], result
    run = result["manifest"]["runs"][0]
    assert run["requested_node_id"] == "analysis"
    assert run["trajectory_node_id"] == "prod"


def test_existing_combined_trajectory_uses_recorded_reference(tmp_path):
    job = tmp_path / "job"
    traj = fixture_job(job)
    combined = node(job, "concat", "analyze", ["prod"], artifacts={
        "combined_trajectory": "combined.dcd", "reference_pdb": "reference.pdb", "frame_times_ns": "times.npy"})
    traj.save_dcd(str(combined / "combined.dcd"))
    traj[0].save_pdb(str(combined / "reference.pdb"))
    np.save(combined / "times.npy", np.arange(7) * 0.01)
    result = export(tmp_path, job_dir=str(job))
    assert result["success"], result
    assert result["manifest"]["runs"][0]["trajectory_node_id"] == "concat"


def test_cli_export_executes_without_node_context(tmp_path):
    job = tmp_path / "job"
    fixture_job(job)
    completed = subprocess.run([sys.executable, "-m", "mdclaw._cli", "export_mddb",
                                "--job-dir", str(job), "--output-dir", str(tmp_path / "bundle"),
                                "--metadata", json.dumps(METADATA)], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["success"]


@pytest.mark.parametrize("metadata", [{"temp": 310}, {"framestep": 1}, {"authors": [123]},
                                      {"pdb_ids": "invalid"}, {"groups": {}}, {"timestep": True},
                                      {"framestep": float("nan")}, {"mds": []}])
def test_bad_or_conflicting_metadata_fails(tmp_path, metadata):
    job = tmp_path / "job"
    fixture_job(job)
    result = export_mddb(str(tmp_path / "bundle"), job_dir=str(job), metadata={**METADATA, **metadata})
    assert not result["success"], result
    assert not (tmp_path / "bundle").exists()


def test_runtime_settings_and_actual_frame_times_override_nominal_metadata(tmp_path):
    job = tmp_path / "job"
    fixture_job(job)
    prod = job / "nodes/prod"
    path = prod / "node.json"
    data = json.loads(path.read_text())
    data["metadata"]["output_frequency_ps"] = 9  # requested, not actual CSV spacing
    data["artifacts"].update(integrator="integrator.xml", runtime_system="system.xml")
    (prod / "integrator.xml").write_text('<Integrator type="LangevinMiddleIntegrator" stepSize="0.002" temperature="310"/>')
    (prod / "system.xml").write_text('<System openmmVersion="8.5.1"><Constraints/><Forces/></System>')
    path.write_text(json.dumps(data))
    result = export(tmp_path, job_dir=str(job), stride=2)
    assert result["success"], result
    inputs = yaml.safe_load((tmp_path / "bundle/project/inputs.yaml").read_text())
    params = inputs["mds"][0]
    assert params["framestep"] == pytest.approx(0.02)
    assert params["temp"] == 310 and params["timestep"] == 2
    assert params["program"] == "OpenMM" and params["version"] == "8.5.1"


def test_ambiguous_analysis_source_needs_explicit_target(tmp_path):
    job = tmp_path / "job"
    traj = fixture_job(job)
    add_prod(job, traj, "other")
    node(job, "analysis", "analyze", ["prod", "other"])
    result = export(tmp_path, job_dir=str(job))
    assert not result["success"] and "Ambiguous source" in str(result)
    assert not (tmp_path / "bundle").exists()


def test_node_directory_is_immutable(tmp_path):
    job = tmp_path / "job"
    fixture_job(job)
    out = job / "nodes/prod/bundle"
    result = export_mddb(str(out), job_dir=str(job), metadata=METADATA)
    assert not result["success"]
    assert not out.exists()
