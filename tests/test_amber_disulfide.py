"""Unit tests for amber_server disulfide bond integration."""
from __future__ import annotations

import textwrap
from pathlib import Path


def _write_pdb(tmp_path: Path, cys22_resname: str, cys95_resname: str) -> Path:
    """Minimal PDB with two CYS/CYX residues at resSeq 22 and 95 (chain A)."""
    pdb = textwrap.dedent(f"""\
        ATOM      1  N   {cys22_resname} A  22       0.000   0.000   0.000  1.00  0.00           N
        ATOM      2  CA  {cys22_resname} A  22       1.458   0.000   0.000  1.00  0.00           C
        ATOM      3  CB  {cys22_resname} A  22       2.000  -1.000   0.000  1.00  0.00           C
        ATOM      4  SG  {cys22_resname} A  22       3.500  -1.500   0.000  1.00  0.00           S
        ATOM      5  C   {cys22_resname} A  22       2.000   1.000   0.000  1.00  0.00           C
        ATOM      6  O   {cys22_resname} A  22       1.300   2.000   0.000  1.00  0.00           O
        ATOM      7  N   {cys95_resname} A  95       5.000   0.000   0.000  1.00  0.00           N
        ATOM      8  CA  {cys95_resname} A  95       4.000   0.000   0.000  1.00  0.00           C
        ATOM      9  CB  {cys95_resname} A  95       4.500  -1.000   0.000  1.00  0.00           C
        ATOM     10  SG  {cys95_resname} A  95       5.540  -1.500   0.000  1.00  0.00           S
        ATOM     11  C   {cys95_resname} A  95       3.000   1.000   0.000  1.00  0.00           C
        ATOM     12  O   {cys95_resname} A  95       3.500   2.000   0.000  1.00  0.00           O
        TER
        END
        """)
    p = tmp_path / f"pair_{cys22_resname}_{cys95_resname}.pdb"
    p.write_text(pdb)
    return p


def _pair(c1_chain: str, c1_resnum: int, c2_chain: str, c2_resnum: int,
          source: str = "pdb_ssbond") -> dict:
    return {
        "cys1": {"chain": c1_chain, "resnum": c1_resnum, "resname": "CYS"},
        "cys2": {"chain": c2_chain, "resnum": c2_resnum, "resname": "CYS"},
        "distance_angstrom": 2.04,
        "confidence": "high",
        "recommendation": "form_bond",
        "source": source,
    }


def test_plan_disulfide_tleap_bonds_emits_bond_on_cyx(tmp_path):
    """CYX residues at the expected resnums produce a tleap bond line.

    The PDB has only two unique residues, so their 1-based unit indices —
    which is what tleap's ``mol.N.SG`` refers to after ``loadpdb`` — are
    1 and 2 regardless of the PDB resSeq values (22, 95).
    """
    from mdclaw.amber_server import _plan_disulfide_tleap_bonds

    pdb = _write_pdb(tmp_path, "CYX", "CYX")
    plan = _plan_disulfide_tleap_bonds(pdb, [_pair("A", 22, "A", 95)])

    assert plan["bond_lines"] == ["bond mol.1.SG mol.2.SG"]
    assert plan["resolved"][0]["status"] == "emitted"
    assert plan["resolved"][0]["tleap_residues"] == [[1, 2]]
    assert plan["warnings"] == []


def test_plan_disulfide_tleap_bonds_skips_cys_protonated(tmp_path):
    """Plain CYS residues are skipped to avoid conflicting with HG on SG."""
    from mdclaw.amber_server import _plan_disulfide_tleap_bonds

    pdb = _write_pdb(tmp_path, "CYS", "CYS")
    plan = _plan_disulfide_tleap_bonds(pdb, [_pair("A", 22, "A", 95)])

    assert plan["bond_lines"] == []
    assert plan["resolved"][0]["status"] == "skipped_cys_protonated"
    assert any("CYS (protonated)" in w for w in plan["warnings"])


def test_plan_disulfide_tleap_bonds_unresolved_when_resnum_missing(tmp_path):
    """Pair pointing at a resnum not in the PDB is marked unresolved."""
    from mdclaw.amber_server import _plan_disulfide_tleap_bonds

    pdb = _write_pdb(tmp_path, "CYX", "CYX")
    plan = _plan_disulfide_tleap_bonds(pdb, [_pair("A", 22, "A", 999)])

    assert plan["bond_lines"] == []
    assert plan["resolved"][0]["status"] == "unresolved"


def test_plan_disulfide_tleap_bonds_ignores_chain_label(tmp_path):
    """Chain label from the pair is advisory — per-chain scan of merged PDB wins.

    prepare_complex records chains from the original PDB but merge_structures
    renames them, so the pair's chain field is unreliable. The scanner looks
    for any chain in the merged PDB that carries both resnums as CYX.
    """
    from mdclaw.amber_server import _plan_disulfide_tleap_bonds

    pdb = _write_pdb(tmp_path, "CYX", "CYX")
    # Pair declares chain B, but PDB only has chain A.
    plan = _plan_disulfide_tleap_bonds(pdb, [_pair("B", 22, "B", 95)])

    assert plan["bond_lines"] == ["bond mol.1.SG mol.2.SG"]
    assert plan["resolved"][0]["status"] == "emitted"


def test_plan_disulfide_tleap_bonds_homodimer_emits_per_chain(tmp_path):
    """Homodimers with the same resSeq in two chains emit one bond per chain.

    The old resnum-only lookup tripped ``len(matches) != 1`` and dropped
    every pair on the floor. The per-chain scanner assigns distinct unit
    indices (chain A → 1, 2; chain B → 3, 4) and emits the correct two
    bonds, with global de-dup so the second disulfide_bonds.json entry
    (which resolves to the same two chains) is recorded as a duplicate
    rather than double-bonding.
    """
    import textwrap as _textwrap
    from mdclaw.amber_server import _plan_disulfide_tleap_bonds

    pdb_text = _textwrap.dedent("""\
        ATOM      1  N   CYX A  22       0.000   0.000   0.000  1.00  0.00           N
        ATOM      2  CA  CYX A  22       1.458   0.000   0.000  1.00  0.00           C
        ATOM      3  CB  CYX A  22       2.000  -1.000   0.000  1.00  0.00           C
        ATOM      4  SG  CYX A  22       3.500  -1.500   0.000  1.00  0.00           S
        ATOM      5  C   CYX A  22       2.000   1.000   0.000  1.00  0.00           C
        ATOM      6  O   CYX A  22       1.300   2.000   0.000  1.00  0.00           O
        ATOM      7  N   CYX A  95       5.000   0.000   0.000  1.00  0.00           N
        ATOM      8  CA  CYX A  95       4.000   0.000   0.000  1.00  0.00           C
        ATOM      9  CB  CYX A  95       4.500  -1.000   0.000  1.00  0.00           C
        ATOM     10  SG  CYX A  95       5.540  -1.500   0.000  1.00  0.00           S
        ATOM     11  C   CYX A  95       3.000   1.000   0.000  1.00  0.00           C
        ATOM     12  O   CYX A  95       3.500   2.000   0.000  1.00  0.00           O
        TER
        ATOM     13  N   CYX B  22      10.000   0.000   0.000  1.00  0.00           N
        ATOM     14  CA  CYX B  22      11.458   0.000   0.000  1.00  0.00           C
        ATOM     15  CB  CYX B  22      12.000  -1.000   0.000  1.00  0.00           C
        ATOM     16  SG  CYX B  22      13.500  -1.500   0.000  1.00  0.00           S
        ATOM     17  C   CYX B  22      12.000   1.000   0.000  1.00  0.00           C
        ATOM     18  O   CYX B  22      11.300   2.000   0.000  1.00  0.00           O
        ATOM     19  N   CYX B  95      15.000   0.000   0.000  1.00  0.00           N
        ATOM     20  CA  CYX B  95      14.000   0.000   0.000  1.00  0.00           C
        ATOM     21  CB  CYX B  95      14.500  -1.000   0.000  1.00  0.00           C
        ATOM     22  SG  CYX B  95      15.540  -1.500   0.000  1.00  0.00           S
        ATOM     23  C   CYX B  95      13.000   1.000   0.000  1.00  0.00           C
        ATOM     24  O   CYX B  95      13.500   2.000   0.000  1.00  0.00           O
        TER
        END
        """)
    pdb = tmp_path / "homodimer.pdb"
    pdb.write_text(pdb_text)

    plan = _plan_disulfide_tleap_bonds(
        pdb,
        [_pair("A", 22, "A", 95), _pair("B", 22, "B", 95)],
    )

    assert plan["bond_lines"] == [
        "bond mol.1.SG mol.2.SG",
        "bond mol.3.SG mol.4.SG",
    ]
    # First pair emits both chains; second pair finds the same two chains
    # already covered and is recorded as a duplicate.
    assert plan["resolved"][0]["status"] == "emitted"
    assert plan["resolved"][0]["tleap_residues"] == [[1, 2], [3, 4]]
    assert plan["resolved"][1]["status"] == "emitted_duplicate"
    assert plan["warnings"] == []


def test_plan_disulfide_tleap_bonds_empty_input(tmp_path):
    """No pairs → empty plan, no warnings."""
    from mdclaw.amber_server import _plan_disulfide_tleap_bonds

    pdb = _write_pdb(tmp_path, "CYX", "CYX")
    plan = _plan_disulfide_tleap_bonds(pdb, [])
    assert plan == {"bond_lines": [], "resolved": [], "warnings": []}


def test_plan_disulfide_tleap_bonds_waters_do_not_clobber_protein(tmp_path):
    """Waters sharing chain + resnum with a protein CYX must not shadow it.

    Solvated PDBs have waters with PDB resSeq wrapping at 9999, and
    packmol-memgen often puts them under the same chain letter as the
    protein. A naive ``by_chain[chain][resnum] = resname`` map would let
    a later ``WAT`` entry overwrite the earlier ``CYX``. The scanner
    filters to CYS/CYX residues at insertion time so the protein entry
    wins and the bond is emitted. unit_index keeps counting every
    residue (including waters) so it continues to match what tleap
    assigns after ``loadpdb``.
    """
    import textwrap as _textwrap
    from mdclaw.amber_server import _plan_disulfide_tleap_bonds

    # Protein CYX at (A, 22) and (A, 95), followed by WAT residues that
    # reuse resSeq 22 and 95 on the same chain A — the real pattern seen
    # in packmol-memgen output for 4M3J.
    pdb_text = _textwrap.dedent("""\
        ATOM      1  N   CYX A  22       0.000   0.000   0.000  1.00  0.00           N
        ATOM      2  CA  CYX A  22       1.458   0.000   0.000  1.00  0.00           C
        ATOM      3  CB  CYX A  22       2.000  -1.000   0.000  1.00  0.00           C
        ATOM      4  SG  CYX A  22       3.500  -1.500   0.000  1.00  0.00           S
        ATOM      5  C   CYX A  22       2.000   1.000   0.000  1.00  0.00           C
        ATOM      6  O   CYX A  22       1.300   2.000   0.000  1.00  0.00           O
        ATOM      7  N   CYX A  95       5.000   0.000   0.000  1.00  0.00           N
        ATOM      8  CA  CYX A  95       4.000   0.000   0.000  1.00  0.00           C
        ATOM      9  CB  CYX A  95       4.500  -1.000   0.000  1.00  0.00           C
        ATOM     10  SG  CYX A  95       5.540  -1.500   0.000  1.00  0.00           S
        ATOM     11  C   CYX A  95       3.000   1.000   0.000  1.00  0.00           C
        ATOM     12  O   CYX A  95       3.500   2.000   0.000  1.00  0.00           O
        ATOM     13  O   WAT A  22      50.000  50.000  50.000  1.00  0.00           O
        ATOM     14  H1  WAT A  22      50.800  50.400  50.000  1.00  0.00           H
        ATOM     15  H2  WAT A  22      49.200  50.400  50.000  1.00  0.00           H
        ATOM     16  O   WAT A  95      60.000  60.000  60.000  1.00  0.00           O
        ATOM     17  H1  WAT A  95      60.800  60.400  60.000  1.00  0.00           H
        ATOM     18  H2  WAT A  95      59.200  60.400  60.000  1.00  0.00           H
        TER
        END
        """)
    pdb = tmp_path / "protein_with_colliding_waters.pdb"
    pdb.write_text(pdb_text)

    plan = _plan_disulfide_tleap_bonds(pdb, [_pair("B", 22, "B", 95)])

    assert plan["bond_lines"] == ["bond mol.1.SG mol.2.SG"]
    assert plan["resolved"][0]["status"] == "emitted"
    assert plan["resolved"][0]["tleap_residues"] == [[1, 2]]
    assert plan["warnings"] == []
