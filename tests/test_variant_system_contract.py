"""Real force-field regressions on generated peptides, independent of SMO."""

import pytest

pytestmark = pytest.mark.slow


def peptide(sequence, tmp_path, chain="Q"):
    from rdkit import Chem
    from rdkit.Chem import AllChem
    from openmm.app import PDBFile

    mol = Chem.AddHs(Chem.MolFromFASTA(sequence))
    assert AllChem.EmbedMolecule(mol, randomSeed=712) == 0
    AllChem.UFFOptimizeMolecule(mol, maxIters=100)
    mol = Chem.RemoveHs(mol)
    for atom in mol.GetAtoms():
        atom.GetPDBResidueInfo().SetChainId(chain)
    path = tmp_path / (sequence + chain + ".pdb")
    Chem.MolToPDBFile(mol, str(path))
    return PDBFile(str(path))


def write_prepared(modeller, path):
    from openmm.app import PDBFile

    with path.open("w") as out:
        PDBFile.writeFile(modeller.topology, modeller.positions, out, keepIds=True)
    return str(path)


def residue_charge(result, number, chain="Q"):
    import xml.etree.ElementTree as ET
    from pathlib import Path

    root = ET.parse(result["system_xml"]).getroot()
    force = next(f for f in root.find("Forces") if f.get("type") == "NonbondedForce")
    q = [float(p.get("q")) for p in force.find("Particles")]
    lines = [
        line
        for line in Path(result["topology_pdb"]).read_text().splitlines()
        if line.startswith(("ATOM  ", "HETATM"))
    ]
    return sum(
        q[i]
        for i, line in enumerate(lines)
        if line[21].strip() == chain and int(line[22:26]) == number
    )


@pytest.mark.parametrize("forcefield,water", [("ff19SB", "opc"), ("ff14SB", "tip3p")])
@pytest.mark.parametrize(
    "base,variant,charge",
    [
        ("H", "HID", 0),
        ("H", "HIE", 0),
        ("H", "HIP", 1),
        ("D", "ASH", 0),
        ("E", "GLH", 0),
        ("K", "LYN", 0),
        ("C", "CYM", -1),
        ("C", "CYS", 0),
    ],
)
def test_variant_actual_template_charge(tmp_path, forcefield, water, base, variant, charge):
    from openmm.app import Modeller, ForceField
    from mdclaw.amber.build_system import build_amber_system

    pdb = peptide("A" + base + "A", tmp_path)
    mod = Modeller(pdb.topology, pdb.positions)
    mod.addHydrogens(
        ForceField("amber14/protein.ff14SB.xml", "amber14/tip3p.xml"),
        variants=[None, "CYS" if variant == "CYM" else variant, None],
    )
    if variant == "CYM":
        mod.delete([a for a in mod.topology.atoms() if a.residue.index == 1 and a.name == "HG"])
    list(mod.topology.residues())[1].name = variant
    result = build_amber_system(
        pdb_file=write_prepared(mod, tmp_path / "prepared.pdb"),
        forcefield=forcefield,
        water_model=water,
        hmr=False,
        pablo_auto_download=False,
        output_dir=str(tmp_path / "built"),
    )
    assert result["success"], result
    assert abs(residue_charge(result, 2) - charge) < 1e-4
    assert abs(result["system_net_charge_e"] - charge) < 1e-4
    assert result["topology_validation"]["input_conservation"]["status"] == "passed"


@pytest.mark.parametrize("sequence,site,expected", [("CA", 1, 1), ("AC", 2, -1)])
def test_terminal_cyx_charge_is_not_forced_to_zero(tmp_path, sequence, site, expected):
    from openmm.app import Modeller, ForceField
    from mdclaw.amber.build_system import build_amber_system

    one = peptide(sequence, tmp_path, "Q")
    two = peptide(sequence, tmp_path, "R")
    from openmm import unit, Vec3

    shifted = [
        p + Vec3(0, 0, 0.4) for p in two.positions.value_in_unit(unit.nanometer)
    ] * unit.nanometer
    mod = Modeller(one.topology, one.positions)
    mod.add(two.topology, shifted)
    sulfurs = [a for a in mod.topology.atoms() if a.name == "SG"]
    mod.topology.addBond(*sulfurs)
    mod.addHydrogens(ForceField("amber14/protein.ff14SB.xml", "amber14/tip3p.xml"))
    for r in mod.topology.residues():
        if r.name == "CYS":
            r.name = "CYX"
    pair = {"cys1": {"chain": "Q", "resnum": site}, "cys2": {"chain": "R", "resnum": site}}
    result = build_amber_system(
        pdb_file=write_prepared(mod, tmp_path / "prepared.pdb"),
        forcefield="ff14SB",
        water_model="tip3p",
        disulfide_bonds=[pair],
        hmr=False,
        pablo_auto_download=False,
        output_dir=str(tmp_path / "built"),
    )
    assert result["success"], result
    assert abs(residue_charge(result, site) - expected) < 1e-4
    assert result["topology_validation"]["disulfides"]["status"] == "passed"
