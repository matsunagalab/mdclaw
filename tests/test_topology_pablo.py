"""Tests for mdclaw._topology_pablo (Pablo bridge helper)."""

import textwrap
from pathlib import Path

import pytest

# Skip the whole module when openff-pablo is not installed (CI image without
# the openmmforcefields-unification env update). The import-guarded fallback
# in the helper itself is exercised by a dedicated test below.
pytest.importorskip("openff.pablo")
pytest.importorskip("openmm")

from mdclaw import _topology_pablo as tp


HYDROGENATED_DIALA = textwrap.dedent(
    """\
    ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N
    ATOM      2  H1  ALA A   1      -0.500   0.800   0.000  1.00  0.00           H
    ATOM      3  H2  ALA A   1      -0.500  -0.800   0.000  1.00  0.00           H
    ATOM      4  H3  ALA A   1       0.500   0.000  -0.800  1.00  0.00           H
    ATOM      5  CA  ALA A   1       1.460   0.000   0.000  1.00  0.00           C
    ATOM      6  HA  ALA A   1       1.800   1.000   0.300  1.00  0.00           H
    ATOM      7  CB  ALA A   1       2.000  -0.500  -1.300  1.00  0.00           C
    ATOM      8  HB1 ALA A   1       3.080  -0.460  -1.300  1.00  0.00           H
    ATOM      9  HB2 ALA A   1       1.660  -1.530  -1.500  1.00  0.00           H
    ATOM     10  HB3 ALA A   1       1.660   0.150  -2.110  1.00  0.00           H
    ATOM     11  C   ALA A   1       2.000  -0.860   1.140  1.00  0.00           C
    ATOM     12  O   ALA A   1       2.290  -2.050   0.990  1.00  0.00           O
    """
).strip()


def _hydrogenated_dipeptide_pdb(tmp_path: Path) -> Path:
    """Use PDBFixer to build a tiny hydrogenated alanine dipeptide PDB.

    Pablo's CCD-based loader is strict: it wants every hydrogen present and
    spelled as the CCD dictates. Going through PDBFixer gives us a topology
    Pablo can match without us hand-curating every H name.
    """
    raw = tmp_path / "diala_raw.pdb"
    raw.write_text(textwrap.dedent("""\
        ATOM      1  N   ALA A   1      -1.057   2.012   0.000  1.00  0.00           N
        ATOM      2  CA  ALA A   1       0.000   1.012   0.000  1.00  0.00           C
        ATOM      3  C   ALA A   1       1.230   1.860   0.000  1.00  0.00           C
        ATOM      4  O   ALA A   1       1.230   3.080   0.000  1.00  0.00           O
        ATOM      5  CB  ALA A   1       0.000   0.181  -1.247  1.00  0.00           C
        ATOM      6  N   ALA A   2       2.323   1.180   0.000  1.00  0.00           N
        ATOM      7  CA  ALA A   2       3.553   2.028   0.000  1.00  0.00           C
        ATOM      8  C   ALA A   2       4.610   1.028   0.000  1.00  0.00           C
        ATOM      9  O   ALA A   2       4.396  -0.196   0.000  1.00  0.00           O
        ATOM     10  CB  ALA A   2       3.553   2.860   1.247  1.00  0.00           C
        ATOM     11  OXT ALA A   2       5.825   1.668   0.000  1.00  0.00           O
        TER
        END
        """))

    from openmm.app import PDBFile
    pytest.importorskip("pdbfixer")
    from pdbfixer import PDBFixer

    fixer = PDBFixer(filename=str(raw))
    fixer.findMissingResidues()
    fixer.findMissingAtoms()
    fixer.addMissingAtoms()
    fixer.addMissingHydrogens(7.0)
    out = tmp_path / "diala_h.pdb"
    with out.open("w") as fh:
        PDBFile.writeFile(fixer.topology, fixer.positions, fh, keepIds=True)
    return out


def test_load_topology_uses_pablo_for_canonical_protein(tmp_path):
    pdb = _hydrogenated_dipeptide_pdb(tmp_path)
    result = tp.load_topology(pdb)
    assert result.used_pablo is True
    assert "pablo_topology_fallback" not in result.guardrail_codes
    # 23 atoms is the expected ALA-ALA hydrogenated count (10 heavy + 13 H).
    assert result.topology.getNumAtoms() == 23
    assert len(result.positions) == 23


def test_load_topology_falls_back_on_unknown_residue(tmp_path):
    """A bad PDB Pablo cannot parse must trigger the fallback warning code."""
    pdb = tmp_path / "bad.pdb"
    pdb.write_text(textwrap.dedent("""\
        ATOM      1  N   XXX A   1       0.000   0.000   0.000  1.00  0.00           N
        ATOM      2  C   XXX A   1       1.000   0.000   0.000  1.00  0.00           C
        END
        """))
    result = tp.load_topology(pdb)
    assert result.used_pablo is False
    assert "pablo_topology_fallback" in result.guardrail_codes
    assert any("Pablo" in w for w in result.warnings)
    # Fallback PDBFile still produces a topology so the build can continue.
    assert result.topology.getNumAtoms() == 2


def test_build_modaa_residue_definitions_returns_definitions():
    defs = tp.build_modaa_residue_definitions(
        [("BNZ", "c1ccccc1")],  # benzene as a stand-in
    )
    assert len(defs) == 1


def test_build_modaa_residue_definitions_handles_bad_smiles():
    # Should not raise even with an obviously-bad SMILES.
    defs = tp.build_modaa_residue_definitions([("BAD", "this-is-not-smiles")])
    assert defs == []


def test_add_disulfide_bonds_links_sg_atoms(tmp_path):
    """Build a minimal topology with two CYS residues and confirm SG-SG bonding."""
    from openmm.app import Element, Topology

    top = Topology()
    chain = top.addChain("A")
    sgs = []
    for i, resnum in enumerate([5, 14]):
        res = top.addResidue("CYS", chain, str(resnum))
        sg = top.addAtom("SG", Element.getBySymbol("S"), res)
        sgs.append(sg)

    pairs = [
        {
            "residue_a": {"chain_id": "A", "residue_number": 5},
            "residue_b": {"chain_id": "A", "residue_number": 14},
        }
    ]
    added = tp.add_disulfide_bonds(top, pairs)
    assert added == 1
    bonds = list(top.bonds())
    assert len(bonds) == 1
    bond_atoms = {bonds[0][0], bonds[0][1]}
    assert bond_atoms == set(sgs)


def test_add_disulfide_bonds_accepts_prepare_complex_schema():
    """Current disulfide_bonds.json uses cys1/cys2 from prepare_complex."""
    from openmm.app import Element, Topology

    top = Topology()
    chain = top.addChain("A")
    sgs = []
    for resnum in [5, 55]:
        res = top.addResidue("CYX", chain, str(resnum))
        sgs.append(top.addAtom("SG", Element.getBySymbol("S"), res))

    pairs = [
        {
            "cys1": {"chain": "A", "resnum": 5},
            "cys2": {"chain": "A", "resnum": 55},
        }
    ]
    assert tp.add_disulfide_bonds(top, pairs) == 1
    assert tp.add_disulfide_bonds(top, pairs) == 0
    bonds = list(top.bonds())
    assert len(bonds) == 1
    assert {bonds[0][0], bonds[0][1]} == set(sgs)


def test_add_disulfide_bonds_silently_skips_unresolvable_pairs():
    """Non-existent residue numbers must not raise — return 0 added."""
    from openmm.app import Topology

    top = Topology()
    pairs = [
        {
            "residue_a": {"chain_id": "A", "residue_number": 99},
            "residue_b": {"chain_id": "A", "residue_number": 100},
        }
    ]
    assert tp.add_disulfide_bonds(top, pairs) == 0


def test_add_disulfide_bonds_with_empty_input():
    from openmm.app import Topology
    assert tp.add_disulfide_bonds(Topology(), []) == 0
