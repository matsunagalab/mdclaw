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
    assert inputs["ligands"] is None and inputs["forced_references"] is None
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


def peptide_job(job):
    top = md.Topology()
    chain = top.add_chain("A")
    for number in (1, 5, 9):  # PDB numbering gaps are not chain breaks.
        residue = top.add_residue("GLY", chain, resSeq=number)
        for name, element in [("N", md.element.nitrogen), ("CA", md.element.carbon),
                              ("C", md.element.carbon), ("O", md.element.oxygen)]:
            top.add_atom(name, element, residue)
    top.create_standard_bonds()
    xyz = np.arange(7 * top.n_atoms * 3, dtype=np.float32).reshape(7, top.n_atoms, 3) / 1000
    traj = md.Trajectory(xyz, top)
    topo = node(job, "topo", "topo", artifacts={"topology_pdb": "system.pdb"})
    traj[0].save_pdb(str(topo / "system.pdb"))
    add_prod(job, traj, "prod")
    return traj


def bond_pairs(topology):
    return {tuple(sorted((a.index, b.index))) for a, b in topology.bonds}


@pytest.mark.parametrize("selection,chains", [
    ("resid 0 or resid 2", 2),
    ("resid 0 or resid 1", 1),
    ("name CA or name C or name N", 1),
])
def test_selected_peptide_preserves_bonds_and_chain_breaks(tmp_path, selection, chains):
    job = tmp_path / "job"
    traj = peptide_job(job)
    source = md.load_topology(str(job / "nodes/topo/system.pdb"))
    indices = source.select(selection)
    result = export(tmp_path, job_dir=str(job), selection=selection)
    assert result["success"], result
    pdb = tmp_path / "bundle/project/md_001/system.pdb"
    converted = md.load(str(pdb))
    assert bond_pairs(converted.topology) == bond_pairs(source.subset(indices))
    assert converted.topology.n_chains == chains
    assert [r.resSeq for r in converted.topology.residues] == [
        r.resSeq for r in source.subset(indices).residues]
    np.testing.assert_allclose(converted.xyz, traj.xyz[:1, indices], atol=0.0001)


@pytest.mark.parametrize("source_case", ["crosslink", "existing_break", "insertion_codes"])
def test_discontinuous_selection_preserves_pdb_topology_details(tmp_path, source_case):
    job = tmp_path / "job"
    peptide_job(job)
    source_pdb = job / "nodes/topo/system.pdb"
    lines = source_pdb.read_text().splitlines(keepends=True)
    atoms = [line for line in lines if line.startswith(("ATOM  ", "HETATM"))]
    if source_case == "crosslink":
        lines.insert(-1, "CONECT" + atoms[0][6:11] + atoms[8][6:11] + "\n")
    elif source_case == "existing_break":
        lines.insert(lines.index(atoms[8]), "TER\n")
    else:
        for i, atom in enumerate(atoms):
            lines[lines.index(atom)] = atom[:22] + "   1" + " AB"[i // 4] + atom[27:]
    source_pdb.write_text(''.join(lines))
    source = md.load_topology(str(source_pdb))
    indices = source.select("resid 0 or resid 2")
    result = export(tmp_path, job_dir=str(job), selection="resid 0 or resid 2")
    assert result["success"], result
    pdb = tmp_path / "bundle/project/md_001/system.pdb"
    converted = md.load_topology(str(pdb))
    assert bond_pairs(converted) == bond_pairs(source.subset(indices))
    assert converted.n_chains == 2
    output_lines = pdb.read_text().splitlines()
    assert sum(line.startswith("TER") for line in output_lines) == 2
    output_atoms = [line for line in output_lines if line.startswith(("ATOM  ", "HETATM"))]
    source_atoms = [line for line in lines if line.startswith(("ATOM  ", "HETATM"))]
    assert [line[:30] for line in output_atoms] == [source_atoms[i][:30] for i in indices]


@pytest.mark.parametrize("damage", ["add_bond", "remove_bond"])
def test_export_rejects_changed_roundtrip_bonds(tmp_path, monkeypatch, damage):
    from mdclaw.evidence import mddb

    job = tmp_path / "job"
    peptide_job(job)
    write_pdb = mddb._write_pdb

    def damage_pdb(path, *args, **kwargs):
        write_pdb(path, *args, **kwargs)
        lines = path.read_text().splitlines(keepends=True)
        atoms = [line for line in lines if line.startswith(("ATOM  ", "HETATM"))]
        if damage == "add_bond":
            # An explicit N-N bond absent from the selected source topology.
            lines.insert(-1, "CONECT" + atoms[0][6:11] + atoms[4][6:11] + "\n")
        else:
            lines.insert(lines.index(atoms[4]), "TER\n")
        path.write_text(''.join(lines))

    monkeypatch.setattr(mddb, "_write_pdb", damage_pdb)
    result = export(tmp_path, job_dir=str(job))
    assert not result["success"], result
    assert "bond" in str(result).lower()
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
        "combined_trajectory": "combined.dcd", "reference_pdb": "reference.pdb", "frame_times_ns": "times.npy"},
        metadata={"source_trajectories": [str(job / "nodes/prod/trajectory.dcd")]})
    traj.save_dcd(str(combined / "combined.dcd"))
    traj[0].save_pdb(str(combined / "reference.pdb"))
    np.save(combined / "times.npy", np.arange(7) * 0.01)
    result = export(tmp_path, job_dir=str(job))
    assert result["success"], result
    assert result["manifest"]["runs"][0]["trajectory_node_id"] == "concat"
    params = yaml.safe_load((tmp_path / "bundle/project/inputs.yaml").read_text())["mds"][0]
    assert params["ff"] == ["ff14SB"]
    assert params["wat"] == "tip3p"
    assert params["temp"] == 300
    assert params["timestep"] == 4
    assert params["ensemble"] == "NPT"


def combined_job(job):
    """Two recorded continuation segments and their already combined trajectory."""
    traj = fixture_job(job)
    second = add_prod(job, traj, "prod2")
    path = second / "node.json"
    data = json.loads(path.read_text())
    data["parent_node_ids"] = ["prod"]
    path.write_text(json.dumps(data))
    combined = node(job, "concat", "analyze", ["prod2"], artifacts={
        "combined_trajectory": "combined.dcd", "reference_pdb": "reference.pdb", "frame_times_ns": "times.npy"},
        metadata={"source_trajectories": [str(job / f"nodes/{nid}/trajectory.dcd") for nid in ("prod", "prod2")],
                  "frames_per_source": [7, 7]})
    md.join([traj, traj]).save_dcd(str(combined / "combined.dcd"))
    traj[0].save_pdb(str(combined / "reference.pdb"))
    np.save(combined / "times.npy", np.arange(14) * 0.01)
    return combined


def test_combined_conditions_use_runtime_and_combined_frame_spacing(tmp_path):
    job = tmp_path / "job"
    combined = combined_job(job)
    for nid in ("prod", "prod2"):
        path = job / "nodes" / nid / "node.json"
        data = json.loads(path.read_text())
        data["artifacts"].update(integrator="integrator.xml", runtime_system="system.xml")
        path.write_text(json.dumps(data))
        (path.parent / "integrator.xml").write_text(
            '<Integrator type="LangevinMiddleIntegrator" stepSize="0.002" temperature="310"/>')
        (path.parent / "system.xml").write_text('<System openmmVersion="8.5.1"><Constraints/><Forces/></System>')
    # The combined artifact has already been sampled differently from the inputs.
    np.save(combined / "times.npy", np.arange(14) * 0.03)
    before = {p: p.read_bytes() for p in job.rglob("*") if p.is_file()}
    result = export(tmp_path, job_dir=str(job), stride=2)
    assert result["success"], result
    params = yaml.safe_load((tmp_path / "bundle/project/inputs.yaml").read_text())["mds"][0]
    assert params["temp"] == 310 and params["timestep"] == 2
    assert params["program"] == "OpenMM" and params["version"] == "8.5.1"
    assert params["framestep"] == pytest.approx(0.06)
    assert result["manifest"]["runs"][0]["n_frames"] == 7
    assert {p: p.read_bytes() for p in job.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("field,override", [("temp", {}), ("temp", {"temp": 300}),
                                           ("timestep", {}), ("ensemble", {}), ("ff", {}), ("wat", {})])
def test_combined_rejects_mixed_conditions_before_writing(tmp_path, field, override):
    job = tmp_path / "job"
    combined_job(job)
    path = job / "nodes/prod2/node.json"
    data = json.loads(path.read_text())
    if field in ("ff", "wat"):
        topo = node(job, "topo2", "topo", ["prod"], artifacts={"topology_pdb": "system.pdb"},
                    metadata={"effective_forcefield": "ff19SB" if field == "ff" else "ff14SB",
                              "water_model": "opc" if field == "wat" else "tip3p"})
        (topo / "system.pdb").write_bytes((job / "nodes/topo/system.pdb").read_bytes())
        data["parent_node_ids"] = ["topo2"]
    elif field == "ensemble":
        data["metadata"]["system_signature"]["ensemble"] = "NVT"
    else:
        data["metadata"][{"temp": "temperature_kelvin", "timestep": "timestep_fs"}[field]] = 310 if field == "temp" else 2
    path.write_text(json.dumps(data))
    result = export_mddb(str(tmp_path / "bundle"), job_dir=str(job), metadata={**METADATA, **override})
    assert result["code"] == "mddb_export_failed", result
    assert f"conflicting {field}" in str(result)
    assert "prod2" in str(result) and "prod" in str(result)
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("both", [False, True])
def test_combined_missing_conditions_require_explicit_metadata(tmp_path, both):
    job = tmp_path / "job"
    combined_job(job)
    for nid in (("prod", "prod2") if both else ("prod2",)):
        path = job / "nodes" / nid / "node.json"
        data = json.loads(path.read_text())
        del data["metadata"]["temperature_kelvin"]
        path.write_text(json.dumps(data))
    result = export(tmp_path, job_dir=str(job))
    assert result["code"] == "mddb_metadata_required", result
    assert result["missing_fields"] == ["temp"]
    assert result["node_id"] == "concat"
    assert not (tmp_path / "bundle").exists()
    if not both:
        conflict = export_mddb(str(tmp_path / "bundle"), job_dir=str(job), metadata={**METADATA, "temp": 310})
        assert conflict["code"] == "mddb_export_failed", conflict
        assert "temp conflicts" in str(conflict)
    result = export_mddb(str(tmp_path / "bundle"), job_dir=str(job), metadata={**METADATA, "temp": 300})
    assert result["success"], result
    params = yaml.safe_load((tmp_path / "bundle/project/inputs.yaml").read_text())["mds"][0]
    assert params["temp"] == 300


@pytest.mark.parametrize("solvent", ["explicit", "implicit", "vacuum"])
def test_combined_missing_water_model_depends_on_solvent_regime(tmp_path, solvent):
    job = tmp_path / "job"
    combined_job(job)
    path = job / "nodes/topo/node.json"
    data = json.loads(path.read_text())
    data["metadata"].update(water_model=None, solvent_type=solvent)
    path.write_text(json.dumps(data))
    result = export(tmp_path, job_dir=str(job))
    if solvent == "explicit":
        assert result["code"] == "mddb_metadata_required", result
        assert result["missing_fields"] == ["wat"]
        assert not (tmp_path / "bundle").exists()
    else:
        assert result["success"], result
        params = yaml.safe_load((tmp_path / "bundle/project/inputs.yaml").read_text())["mds"][0]
        assert "wat" not in params


@pytest.mark.parametrize("damage", ["missing", "unknown", "relative", "dependency", "failed"])
def test_combined_requires_traceable_completed_sources(tmp_path, damage):
    job = tmp_path / "job"
    combined = combined_job(job)
    path = combined / "node.json"
    data = json.loads(path.read_text())
    if damage == "missing":
        del data["metadata"]["source_trajectories"]
    elif damage == "unknown":
        data["metadata"]["source_trajectories"] = [str(tmp_path / "unknown.dcd")]
    elif damage == "relative":
        data["metadata"]["source_trajectories"] = ["nodes/prod/trajectory.dcd"]
    elif damage == "dependency":
        # A dependency is evidence history, but is not a parent-lineage source.
        add_prod(job, md.load(str(combined / "combined.dcd"), top=str(combined / "reference.pdb"))[:7], "other")
        data["dependency_node_ids"] = ["other"]
        data["metadata"]["source_trajectories"] = [str(job / "nodes/other/trajectory.dcd")]
    else:
        source_path = job / "nodes/prod/node.json"
        source = json.loads(source_path.read_text())
        source["status"] = "failed"
        source_path.write_text(json.dumps(source))
    path.write_text(json.dumps(data))
    result = export(tmp_path, targets=[target(job, "concat")])
    assert result["code"] == "mddb_export_failed", result
    assert not (tmp_path / "bundle").exists()


def test_combined_does_not_inherit_unselected_ancestor_conditions(tmp_path):
    job = tmp_path / "job"
    combined = combined_job(job)
    path = job / "nodes/prod/node.json"
    data = json.loads(path.read_text())
    data["metadata"]["temperature_kelvin"] = 999
    path.write_text(json.dumps(data))
    path = combined / "node.json"
    data = json.loads(path.read_text())
    data["metadata"]["source_trajectories"] = [str(job / "nodes/prod2/trajectory.dcd")]
    data["metadata"]["frames_per_source"] = [7]
    path.write_text(json.dumps(data))
    traj = md.load(str(job / "nodes/prod2/trajectory.dcd"), top=str(combined / "reference.pdb"))
    traj.save_dcd(str(combined / "combined.dcd"))
    np.save(combined / "times.npy", np.arange(7) * 0.01)
    result = export(tmp_path, job_dir=str(job))
    assert result["success"], result
    params = yaml.safe_load((tmp_path / "bundle/project/inputs.yaml").read_text())["mds"][0]
    assert params["temp"] == 300


def test_concat_tool_then_cli_export_preserves_conditions(tmp_path, monkeypatch):
    from mdclaw.analyze.concat import concat_trajectory

    job = tmp_path / "job"
    combined = combined_job(job)
    monkeypatch.chdir(tmp_path)
    joined = concat_trajectory(
        trajectory_files=[str(job / f"nodes/{nid}/trajectory.dcd") for nid in ("prod", "prod2")],
        topology_file=str(job / "nodes/topo/system.pdb"), energy_files=[], selection="all", chunk=2,
    )
    assert joined["success"], joined
    # Record the real tool output in the existing synthetic DAG fixture.
    path = combined / "node.json"
    data = json.loads(path.read_text())
    data["artifacts"].update({key: joined[key] for key in ("combined_trajectory", "reference_pdb")})
    data["metadata"].update({key: joined[key] for key in ("source_trajectories", "frames_per_source")})
    path.write_text(json.dumps(data))
    completed = subprocess.run([sys.executable, "-m", "mdclaw._cli", "export_mddb",
                                "--job-dir", str(job), "--output-dir", str(tmp_path / "bundle"),
                                "--metadata", json.dumps(METADATA)], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["success"]
    params = yaml.safe_load((tmp_path / "bundle/project/inputs.yaml").read_text())["mds"][0]
    assert params["ff"] == ["ff14SB"] and params["wat"] == "tip3p"
    assert params["temp"] == 300 and params["timestep"] == 4 and params["ensemble"] == "NPT"
    assert params["framestep"] == pytest.approx(0.01)
    converted = md.load(str(tmp_path / "bundle/project/md_001/trajectory.dcd"),
                        top=str(tmp_path / "bundle/project/md_001/system.pdb"))
    assert converted.n_frames == 14 and converted.n_atoms == 7


def test_cli_export_executes_without_node_context(tmp_path):
    job = tmp_path / "job"
    fixture_job(job)
    completed = subprocess.run([sys.executable, "-m", "mdclaw._cli", "export_mddb",
                                "--job-dir", str(job), "--output-dir", str(tmp_path / "bundle"),
                                "--metadata", json.dumps(METADATA)], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["success"]


@pytest.mark.parametrize("references", [["Q9BYF1", "P0DTC2"], {"A": "Q9BYF1", "B": "noref"}])
def test_cli_export_includes_ligand_and_uniprot_references(tmp_path, references):
    job = tmp_path / "job"
    fixture_job(job)
    ligands = [{"pubchem": 2244, "vmd_selection": "resname LIG"},
               {"drugbank": "DB00945", "vmd_selection": ["resname LIG"]},
               {"chembl": "CHEMBL25"}]
    metadata = {**METADATA, "ligands": ligands, "forced_references": references}
    completed = subprocess.run([sys.executable, "-m", "mdclaw._cli", "export_mddb",
                                "--job-dir", str(job), "--output-dir", str(tmp_path / "bundle"),
                                "--metadata", json.dumps(metadata)], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["success"]
    inputs = yaml.safe_load((tmp_path / "bundle/project/inputs.yaml").read_text())
    assert inputs["ligands"] == ligands
    assert inputs["forced_references"] == references


def test_cli_invalid_ligand_metadata_leaves_no_bundle(tmp_path):
    job = tmp_path / "job"
    fixture_job(job)
    metadata = {**METADATA, "ligands": [{"pubchem": 2244, "selection": "resname LIG"}]}
    completed = subprocess.run([sys.executable, "-m", "mdclaw._cli", "export_mddb",
                                "--job-dir", str(job), "--output-dir", str(tmp_path / "bundle"),
                                "--metadata", json.dumps(metadata)], capture_output=True, text=True, timeout=60)
    result = json.loads(completed.stdout)
    assert completed.returncode != 0 and result["code"] == "mddb_export_failed", result
    assert "metadata.ligands[0]" in str(result) and "vmd_selection" in str(result)
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("grouping", ["replicas", "separate"])
def test_reference_metadata_is_project_level(tmp_path, grouping):
    job = tmp_path / "job"
    traj = fixture_job(job)
    add_prod(job, traj, "replica")
    references = {"ligands": [2244], "forced_references": {"A": "Q9BYF1"}}
    result = export_mddb(str(tmp_path / "bundle"), metadata={**METADATA, **references}, grouping=grouping,
                         targets=[target(job), target(job, "replica")])
    assert result["success"], result
    projects = sorted((tmp_path / "bundle").glob("*/inputs.yaml"))
    assert len(projects) == (2 if grouping == "separate" else 1)
    for path in projects:
        inputs = yaml.safe_load(path.read_text())
        assert inputs["ligands"] == [{"pubchem": "2244"}]
        assert inputs["forced_references"] == references["forced_references"]
        assert all("ligands" not in entry and "forced_references" not in entry for entry in inputs["mds"])


@pytest.mark.parametrize("ligands,expected", [
    ([2244, "1986"], [{"pubchem": "2244"}, {"pubchem": "1986"}]),
    ([{"pubchem": "2244", "drugbank": "DB00945", "chembl": "CHEMBL25"}],
     [{"pubchem": "2244", "drugbank": "DB00945", "chembl": "CHEMBL25"}]),
    ([{"pubchem": None, "chembl": "CHEMBL25", "vmd_selection": None}], [{"chembl": "CHEMBL25"}]),
    ([], []), (None, None),
])
def test_ligand_metadata_normalization_does_not_mutate_input(tmp_path, ligands, expected):
    job = tmp_path / "job"
    fixture_job(job)
    metadata = {**METADATA, "ligands": ligands, "forced_references": {"A": "Q9BYF1"}}
    original = json.dumps(metadata, sort_keys=True)
    result = export_mddb(str(tmp_path / "bundle"), job_dir=str(job), metadata=metadata)
    assert result["success"], result
    inputs = yaml.safe_load((tmp_path / "bundle/project/inputs.yaml").read_text())
    assert inputs["ligands"] == expected
    assert inputs["forced_references"] == {"A": "Q9BYF1"}
    assert json.dumps(metadata, sort_keys=True) == original


@pytest.mark.parametrize("references", [[], {}, None, ["P12345-2"], {"A": "P12345-2"}])
def test_optional_uniprot_reference_shapes(tmp_path, references):
    job = tmp_path / "job"
    fixture_job(job)
    result = export_mddb(str(tmp_path / "bundle"), job_dir=str(job),
                         metadata={**METADATA, "forced_references": references})
    assert result["success"], result
    inputs = yaml.safe_load((tmp_path / "bundle/project/inputs.yaml").read_text())
    assert inputs["forced_references"] == references


@pytest.mark.parametrize("ligands", [
    "2244", {"pubchem": 2244}, ["aspirin"], [True], [2244.0], [0], [-1], ["0"],
    [None], [{}], [{"pubchem": None}], [{"vmd_selection": "resname LIG"}],
    [{"pubchem": True}], [{"pubchem": 0}], [{"pubchem": "aspirin"}],
    [{"pubchem": []}], [{"pubchem": 1.2}], [{"drugbank": 945}], [{"chembl": " "}],
    [{"pubchem": 2244, "selection": "resname LIG"}],
    [{"pubchem": 2244, "unknown": "value"}],
    [{"pubchem": 2244, "vmd_selection": " "}],
    [{"pubchem": 2244, "vmd_selection": []}],
    [{"pubchem": 2244, "vmd_selection": ["resname LIG", 1]}],
    [{"pubchem": 2244, "vmd_selection": {"resname": "LIG"}}],
])
def test_invalid_ligand_metadata_fails_before_writing(tmp_path, ligands):
    job = tmp_path / "job"
    fixture_job(job)
    result = export_mddb(str(tmp_path / "bundle"), job_dir=str(job), metadata={**METADATA, "ligands": ligands})
    assert result["code"] == "mddb_export_failed", result
    assert "metadata.ligands" in str(result)
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("references", ["Q9BYF1", 1, True, [None], [1], [" "],
                                       {"A": None}, {"A": ["Q9BYF1"]}, {"A": " "},
                                       {" ": "Q9BYF1"}, {1: "Q9BYF1"}])
def test_invalid_uniprot_metadata_fails_before_writing(tmp_path, references):
    job = tmp_path / "job"
    fixture_job(job)
    result = export_mddb(str(tmp_path / "bundle"), job_dir=str(job),
                         metadata={**METADATA, "forced_references": references})
    assert result["code"] == "mddb_export_failed", result
    assert "metadata.forced_references" in str(result)
    assert not (tmp_path / "bundle").exists()


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
