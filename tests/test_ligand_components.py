"""Source-preserving, explicitly requested peptide-to-ligand representation."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("rdkit")
pytest.importorskip("gemmi")
from mdclaw.structure.split import split_molecules
from mdclaw.structure.clean_ligand import clean_ligand
from mdclaw.structure.ligand_components import route_ligand_components, validate_ligand_conversion


@pytest.fixture
def component(tmp_path):
    # Synthetic connected COCO graph with two polymer residue labels. This
    # fixture tests routing/identity; a real capped peptide is tested below.
    source = tmp_path / "source.pdb"
    source.write_text(
        "ATOM      1  C1  ALA B   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      2  O1  ALA B   1       1.430   0.000   0.000  1.00  0.00           O\n"
        "ATOM      3  C1  GLY B   2       2.100   1.180   0.000  1.00  0.00           C\n"
        "ATOM      4  O1  GLY B   2       3.520   1.100   0.000  1.00  0.00           O\nEND\n")
    split = split_molecules(str(source), output_dir=str(tmp_path / "split"), select_chains=["B"])
    assert split["success"], split
    spec = {"selection": "B:1-2", "residue_name": "LIG", "smiles": "COCO"}
    return source, split, spec


def test_explicit_conversion_reuses_cleaner_and_records_source_atoms(component):
    source, split, spec = component
    route_ligand_components(source, split, [spec])
    assert not split["protein_files"] and len(split["ligand_files"]) == 1
    path = split["ligand_files"][0]
    clean = clean_ligand(path, "LIG", smiles="COCO", optimize=False, fetch_smiles=False)
    assert clean["success"], clean
    audit = validate_ligand_conversion(path, clean, spec)
    assert len(audit["heavy_atom_mapping"]) == 4
    assert {r["source"]["resname"] for r in audit["heavy_atom_mapping"]} == {"ALA", "GLY"}
    assert {r["prepared"]["resname"] for r in audit["heavy_atom_mapping"]} == {"LIG"}


@pytest.mark.parametrize("change", ["partial", "duplicate", "missing_chain", "unknown_key", "changed_split"])
def test_invalid_source_declarations_fail_before_routing(component, change):
    source, split, spec = component
    specs = [copy.deepcopy(spec)]
    if change == "partial":
        specs[0]["selection"] = "B:1-1"
    elif change == "duplicate":
        specs *= 2
    elif change == "missing_chain":
        specs[0]["selection"] = "Q:1-2"
    elif change == "unknown_key":
        specs[0]["force"] = True
    else:
        path = Path(split["protein_files"][0])
        path.write_text(path.read_text().replace("   1.430", "   4.430"))
    with pytest.raises(ValueError):
        route_ligand_components(source, split, specs)


def test_wrong_declared_chemistry_cannot_be_hidden_by_ligand_names(component):
    source, split, spec = component
    route_ligand_components(source, split, [spec])
    path = split["ligand_files"][0]
    clean = clean_ligand(path, "LIG", smiles="COCO", optimize=False, fetch_smiles=False)
    assert clean["success"]
    with pytest.raises(ValueError, match="SMILES"):
        validate_ligand_conversion(path, clean, {**spec, "smiles": "CCOO"})


def test_external_covalent_link_is_not_silently_cut(component, tmp_path):
    import gemmi

    source, _, spec = component
    source.write_text(source.read_text().replace("END\n",
        "ATOM      5  N   ALA C   1       4.800   1.100   0.000  1.00  0.00           N\nEND\n"))
    structure = gemmi.read_structure(str(source))
    structure.setup_entities()
    connection = gemmi.Connection()
    connection.name = "external"
    connection.type = gemmi.ConnectionType.Covale
    connection.partner1 = gemmi.AtomAddress("B", gemmi.SeqId(2, " "), "GLY", "O1")
    connection.partner2 = gemmi.AtomAddress("C", gemmi.SeqId(1, " "), "ALA", "N")
    structure.connections.append(connection)
    linked = tmp_path / "linked.cif"
    structure.make_mmcif_document().write_file(str(linked))
    split = split_molecules(str(linked), output_dir=str(tmp_path / "linked_split"), select_chains=["B"])
    assert split["success"], split
    with pytest.raises(ValueError, match="covalent link"):
        route_ligand_components(linked, split, [spec])


def test_external_pdb_conect_is_not_silently_cut(component, tmp_path):
    source, _, spec = component
    source.write_text(source.read_text().replace("END\n",
        "ATOM      5  N   ALA C   1       4.800   1.100   0.000  1.00  0.00           N\n"
        "CONECT    4    5\nEND\n"))
    split = split_molecules(str(source), output_dir=str(tmp_path / "conect_split"), select_chains=["B"])
    assert split["success"]
    with pytest.raises(ValueError, match="CONECT covalent link"):
        route_ligand_components(source, split, [spec])


@pytest.mark.integration
def test_real_4mn3_declarative_prep_preserves_single_ligand(tmp_path):
    from mdclaw._node import create_node, read_node
    from mdclaw.research.source_node import register_local_structure
    from mdclaw.study.workflow import bootstrap_md_workflow
    source = os.environ.get("MDCLAW_4MN3_CIF")
    if not source:
        pytest.skip("set MDCLAW_4MN3_CIF to the cached public deposit")
    smiles = "CC(=O)N[C@@H](Cc1ccccc1)C(=O)N[C@@H](C)C(=O)N[C@@H](Cc1ccc(O)cc1)C(=O)N[C@@H](CCCC[N+](C)(C)C)C(=O)N[C@@H](CO)C(N)=O"
    workflow = bootstrap_md_workflow(str(tmp_path / "study"), "Test explicit ligand representation")
    assert workflow["success"], workflow
    job = workflow["job_dir"]
    source_node = create_node(job, "source")
    registered = register_local_structure(source, job, source_node["node_id"])
    assert registered["success"], registered
    prep = create_node(job, "prep")
    declarations = [{"selection": "B:1-7", "residue_name": "LIG", "smiles": smiles}]
    invocation = subprocess.run(
        [sys.executable, "-m", "mdclaw._cli", "--job-dir", job, "--node-id", prep["node_id"],
         "prepare_complex", "--select-chains", "A", "B", "--include-types", "protein",
         "--protonation-method", "standard", "--ligand-components", json.dumps(declarations)],
        capture_output=True, text=True)
    assert invocation.returncode == 0, invocation.stdout + invocation.stderr
    result = json.loads(invocation.stdout)
    assert result["success"], result
    assert len(result["proteins"]) == len(result["ligands"]) == 1
    conversion = result["ligands"][0]["source_conversion"]
    assert len(conversion["heavy_atom_mapping"]) == 50
    assert len({r["merged_atom_index"] for r in conversion["heavy_atom_mapping"]}) == 50
    assert result["ligands"][0]["net_charge"] == 1
    node = read_node(job, prep["node_id"])
    assert node["status"] == "completed"
    chemistry = Path(job) / "nodes" / prep["node_id"] / "artifacts/ligand_chemistry.json"
    assert len(json.loads(chemistry.read_text())[0]["source_conversion"]["heavy_atom_mapping"]) == 50
    # A declaration never grants permission to replace the original source.
    from mdclaw.structure.prepare_complex import prepare_complex

    branch = create_node(job, "prep", parent_node_ids=[source_node["node_id"]])
    rejected = prepare_complex(result["merged_pdb"], job_dir=job, node_id=branch["node_id"],
                               ligand_components=declarations)
    assert not rejected["success"] and rejected["code"] == "input_resolution_blocked"
