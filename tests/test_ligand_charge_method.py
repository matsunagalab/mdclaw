"""``ligand_charge_method`` of build_amber_system / build_decoupled_system.

NAGL (``openff-gnn-am1bcc-1.0.0``) stays the default; ``am1bcc`` skips it and
has the GAFF template generator fit AM1-BCC for every ligand. The T4L ABFE
benchmark needed the choice: NAGL put -0.58 e on indole's N1 where AmberTools'
AM1-BCC puts -0.20 e, and the model then hydrated indole 3.5 kcal/mol too
strongly (2026-10-01).
"""

import importlib

import pytest

pytest.importorskip("openmm")
pytest.importorskip("openmmforcefields")

ob = importlib.import_module("mdclaw.amber.openmm_build")


class _Molecule:
    def __init__(self, charges="from-sdf"):
        self.total_charge = 0.0
        self.partial_charges = charges


def test_am1bcc_requests_skip_nagl_and_clear_supplied_charges(monkeypatch):
    monkeypatch.setattr(ob, "_assign_nagl_partial_charges",
                        lambda *a, **k: pytest.fail("NAGL must not run when AM1-BCC is requested"))
    molecule = _Molecule()
    records = ob._request_am1bcc_partial_charges([{"residue_name": "IND", "ligand_instance_id": "A:IND:400"}],
                                                 [molecule])
    assert molecule.partial_charges is None            # the template generator fits AM1-BCC itself
    assert records == [{"residue_name": "IND", "ligand_instance_id": "A:IND:400", "formal_charge_e": 0.0,
                        "method": "am1bcc", "charge_engine": records[0]["charge_engine"], "status": "requested"}]
    assert records[0]["charge_engine"] in ("ambertools_sqm", "openeye")
    assert ob.LIGAND_CHARGE_METHODS == ("nagl", "am1bcc")


def test_an_unknown_method_is_refused_and_the_node_stays_pending(tmp_path):
    from mdclaw._node import create_node, read_node
    from mdclaw.amber.build_system import build_amber_system
    from mdclaw.fep.abfe import build_decoupled_system

    for tool in (build_amber_system, build_decoupled_system):
        refused = tool(pdb_file=str(tmp_path / "absent.pdb"), ligand_charge_method="resp")
        assert refused["success"] is False and refused["code"] == "invalid_parameter_value", refused
        assert "am1bcc" in str(refused.get("context") or refused.get("errors"))
    # case and surrounding blanks are not an error
    assert build_amber_system(pdb_file=str(tmp_path / "absent.pdb"), ligand_charge_method=" AM1BCC ")["code"] != \
        "invalid_parameter_value"

    job = tmp_path / "job"
    job.mkdir()
    source = create_node(str(job), "source")["node_id"]
    prep = create_node(str(job), "prep", parent_node_ids=[source])["node_id"]
    for tool in (build_amber_system, build_decoupled_system):
        topo = create_node(str(job), "topo", parent_node_ids=[prep])["node_id"]
        refused = tool(job_dir=str(job), node_id=topo, ligand_charge_method="resp")
        assert refused["code"] == "invalid_parameter_value", refused
        assert read_node(str(job), topo)["status"] == "pending"


def test_the_decoupled_ligand_records_the_charge_model_both_legs_must_share():
    from mdclaw.fep.abfe import _ligand_charges

    record = {"residue_name": "IND", "ligand_instance_id": "A:IND:400"}

    def built(*assignments):
        return {"forcefield_provenance": {"ligand_charge_assignment": list(assignments)}}

    nagl = _ligand_charges(built({"residue_name": "IND", "ligand_instance_id": "A:IND:400", "method": "nagl",
                                  "nagl_model": "openff-gnn-am1bcc-1.0.0.pt"}), record, "nagl")
    assert nagl["model"] == "nagl:openff-gnn-am1bcc-1.0.0.pt" and nagl["assigned"] == "nagl"
    asked = _ligand_charges(built({"residue_name": "IND", "method": "am1bcc", "charge_engine": "ambertools_sqm"}),
                            record, "am1bcc")
    fallback = _ligand_charges(built({"residue_name": "IND", "method": "am1bcc_fallback", "fallback_reason": "x"}),
                               record, "nagl")
    # the same AM1-BCC fit whether it was asked for or reached as NAGL's fallback
    assert asked["model"] == fallback["model"] == "am1bcc"
    assert asked["charge_engine"] == "ambertools_sqm" and fallback["requested"] == "nagl"
    # another ligand's record is not this one's
    other = _ligand_charges(built({"residue_name": "BEN", "ligand_instance_id": "A:BEN:1", "method": "am1bcc"},
                                  {"residue_name": "IND", "ligand_instance_id": "A:IND:400", "method": "nagl",
                                   "nagl_model": "m.pt"}), record, "nagl")
    assert other["model"] == "nagl:m.pt"


def _indole(tmp_path):
    Chem = pytest.importorskip("rdkit.Chem")
    from rdkit.Chem import AllChem

    mol = Chem.AddHs(Chem.MolFromSmiles("c1ccc2[nH]ccc2c1"))
    AllChem.EmbedMolecule(mol, randomSeed=7)
    AllChem.MMFFOptimizeMolecule(mol)
    counts = {}
    for atom in mol.GetAtoms():
        symbol = atom.GetSymbol()
        counts[symbol] = counts.get(symbol, 0) + 1
        atom.SetMonomerInfo(Chem.AtomPDBResidueInfo(f"{symbol}{counts[symbol]}".ljust(4)[:4], residueName="IND",
                                                    residueNumber=1, chainId="A", isHeteroAtom=True))
    pdb, sdf = tmp_path / "ind.pdb", tmp_path / "ind.sdf"
    Chem.MolToPDBFile(mol, str(pdb))
    writer = Chem.SDWriter(str(sdf))
    writer.write(mol)
    writer.close()
    return pdb, [{"sdf": str(sdf), "residue_name": "IND", "smiles": "c1ccc2[nH]ccc2c1", "net_charge": 0,
                  "ligand_instance_id": "A:IND:1"}]


@pytest.mark.slow
def test_am1bcc_charges_reach_the_built_system(tmp_path):
    """Indole in vacuum: the NAGL default and the AM1-BCC request give the System
    different N1 charges (-0.58 e vs -0.20 e with AmberTools sqm) and say so."""
    import openmm
    from openmm import unit
    from pathlib import Path

    from mdclaw.amber.build_system import build_amber_system

    pdb, ligands = _indole(tmp_path)
    charges = {}
    for method in ("nagl", "am1bcc"):
        built = build_amber_system(pdb_file=str(pdb), ligand_chemistry=ligands, ligand_charge_method=method,
                                   output_dir=str(tmp_path / method))
        assert built["success"], built.get("errors")
        provenance = built["forcefield_provenance"]
        assert provenance["ligand_charge_method"] == method == built["parameters"]["ligand_charge_method"]
        (record,) = provenance["ligand_charge_assignment"]
        assert record["method"] == method and record["status"] == "success"
        system = openmm.XmlSerializer.deserialize(Path(built["system_xml"]).read_text())
        nonbonded = next(f for f in system.getForces() if isinstance(f, openmm.NonbondedForce))
        atoms = [line for line in open(built["topology_pdb"]) if line.startswith(("ATOM", "HETATM"))]
        (nitrogen,) = [i for i, line in enumerate(atoms) if line[12:16].strip().startswith("N")]
        q = [nonbonded.getParticleParameters(i)[0].value_in_unit(unit.elementary_charge) for i in range(len(atoms))]
        assert sum(q) == pytest.approx(0.0, abs=1e-4)
        charges[method] = q[nitrogen]
    assert charges["nagl"] < -0.5
    assert -0.35 < charges["am1bcc"] < -0.1
