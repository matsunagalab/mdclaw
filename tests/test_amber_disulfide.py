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


def test_plan_disulfide_topology_bonds_emits_indices_on_cyx(tmp_path):
    """CYX residues at the expected resnums resolve to unit-sequential indices.

    The PDB has only two unique residues, so their 1-based unit indices —
    which is what the openmmforcefields build path passes to
    ``Topology.addBond`` — are 1 and 2 regardless of the PDB resSeq
    values (22, 95).
    """
    from mdclaw.amber.topology_bonds import _plan_disulfide_topology_bonds

    pdb = _write_pdb(tmp_path, "CYX", "CYX")
    plan = _plan_disulfide_topology_bonds(pdb, [_pair("A", 22, "A", 95)])

    assert plan["resolved"][0]["status"] == "emitted"
    assert plan["resolved"][0]["topology_residues"] == [[1, 2]]
    assert plan["warnings"] == []


def test_plan_disulfide_topology_bonds_skips_cys_protonated(tmp_path):
    """Plain CYS residues are skipped to avoid conflicting with HG on SG."""
    from mdclaw.amber.topology_bonds import _plan_disulfide_topology_bonds

    pdb = _write_pdb(tmp_path, "CYS", "CYS")
    plan = _plan_disulfide_topology_bonds(pdb, [_pair("A", 22, "A", 95)])

    assert plan["resolved"][0]["status"] == "skipped_cys_protonated"
    assert plan["resolved"][0]["topology_residues"] is None
    assert any("CYS (protonated)" in w for w in plan["warnings"])


def test_plan_disulfide_topology_bonds_unresolved_when_resnum_missing(tmp_path):
    """Pair pointing at a resnum not in the PDB is marked unresolved."""
    from mdclaw.amber.topology_bonds import _plan_disulfide_topology_bonds

    pdb = _write_pdb(tmp_path, "CYX", "CYX")
    plan = _plan_disulfide_topology_bonds(pdb, [_pair("A", 22, "A", 999)])

    assert plan["resolved"][0]["status"] == "unresolved"
    assert plan["resolved"][0]["topology_residues"] is None


def test_plan_disulfide_topology_bonds_ignores_chain_label(tmp_path):
    """Chain label from the pair is advisory — per-chain scan of merged PDB wins.

    prepare_complex records chains from the original PDB but merge_structures
    renames them, so the pair's chain field is unreliable. The scanner looks
    for any chain in the merged PDB that carries both resnums as CYX.
    """
    from mdclaw.amber.topology_bonds import _plan_disulfide_topology_bonds

    pdb = _write_pdb(tmp_path, "CYX", "CYX")
    # Pair declares chain B, but PDB only has chain A.
    plan = _plan_disulfide_topology_bonds(pdb, [_pair("B", 22, "B", 95)])

    assert plan["resolved"][0]["status"] == "emitted"
    assert plan["resolved"][0]["topology_residues"] == [[1, 2]]


def test_plan_disulfide_topology_bonds_homodimer_emits_per_chain(tmp_path):
    """Homodimers with the same resSeq in two chains emit one bond per chain.

    The old resnum-only lookup tripped ``len(matches) != 1`` and dropped
    every pair on the floor. The per-chain scanner assigns distinct unit
    indices (chain A → 1, 2; chain B → 3, 4) and emits the correct two
    bonds, with global de-dup so the second disulfide_bonds.json entry
    (which resolves to the same two chains) is recorded as a duplicate
    rather than double-bonding.
    """
    import textwrap as _textwrap
    from mdclaw.amber.topology_bonds import _plan_disulfide_topology_bonds

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

    plan = _plan_disulfide_topology_bonds(
        pdb,
        [_pair("A", 22, "A", 95), _pair("B", 22, "B", 95)],
    )

    # First pair emits both chains; second pair finds the same two chains
    # already covered and is recorded as a duplicate.
    assert plan["resolved"][0]["status"] == "emitted"
    assert plan["resolved"][0]["topology_residues"] == [[1, 2], [3, 4]]
    assert plan["resolved"][1]["status"] == "emitted_duplicate"
    assert plan["warnings"] == []


def test_plan_disulfide_topology_bonds_empty_input(tmp_path):
    """No pairs → empty plan, no warnings."""
    from mdclaw.amber.topology_bonds import _plan_disulfide_topology_bonds

    pdb = _write_pdb(tmp_path, "CYX", "CYX")
    plan = _plan_disulfide_topology_bonds(pdb, [])
    assert plan == {"resolved": [], "warnings": []}


def test_plan_disulfide_topology_bonds_waters_do_not_clobber_protein(tmp_path):
    """Waters sharing chain + resnum with a protein CYX must not shadow it.

    Solvated PDBs have waters with PDB resSeq wrapping at 9999, and
    packmol-memgen often puts them under the same chain letter as the
    protein. A naive ``by_chain[chain][resnum] = resname`` map would let
    a later ``WAT`` entry overwrite the earlier ``CYX``. The scanner
    filters to CYS/CYX residues at insertion time so the protein entry
    wins and the bond is emitted. unit_index keeps counting every
    residue (including waters) so it continues to match the unit
    sequential index used by the openmmforcefields topology builder.
    """
    import textwrap as _textwrap
    from mdclaw.amber.topology_bonds import _plan_disulfide_topology_bonds

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

    plan = _plan_disulfide_topology_bonds(pdb, [_pair("B", 22, "B", 95)])

    assert plan["resolved"][0]["status"] == "emitted"
    assert plan["resolved"][0]["topology_residues"] == [[1, 2]]
    assert plan["warnings"] == []


def _minimal_disulfide_topology_and_system(*, bonded: bool):
    import pytest

    pytest.importorskip("openmm")
    from openmm import HarmonicBondForce, System, unit
    from openmm.app import Element, Topology

    topology = Topology()
    chain = topology.addChain("A")
    sg_atoms = []
    for resnum in (5, 55):
        residue = topology.addResidue("CYX", chain, str(resnum))
        sg_atoms.append(topology.addAtom("SG", Element.getBySymbol("S"), residue))
    system = System()
    for _atom in topology.atoms():
        system.addParticle(32.06 * unit.dalton)
    if bonded:
        topology.addBond(sg_atoms[0], sg_atoms[1])
        bonds = HarmonicBondForce()
        bonds.addBond(
            sg_atoms[0].index,
            sg_atoms[1].index,
            0.204 * unit.nanometer,
            1000.0 * unit.kilojoule_per_mole / unit.nanometer**2,
        )
        system.addForce(bonds)
    return topology, system


def test_final_disulfide_validation_trusts_artifacts_over_manual_add_count():
    """A 0/N manual add count is not authoritative if final artifacts pass."""
    from mdclaw.amber.topology_validation import _validate_final_disulfides

    topology, system = _minimal_disulfide_topology_and_system(bonded=True)
    report = _validate_final_disulfides(
        topology=topology,
        system=system,
        disulfide_bonds=[_pair("A", 5, "A", 55)],
        manual_added_count=0,
    )

    assert report["status"] == "passed"
    assert report["expected_count"] == 1
    assert report["observed_topology_sg_sg_bond_count"] == 1
    assert report["observed_system_harmonic_sg_sg_bond_count"] == 1
    assert report["non_authoritative_notes"]


def test_final_disulfide_validation_fails_missing_artifact_bond():
    from mdclaw.amber.topology_validation import _validate_final_disulfides

    topology, system = _minimal_disulfide_topology_and_system(bonded=False)
    report = _validate_final_disulfides(
        topology=topology,
        system=system,
        disulfide_bonds=[_pair("A", 5, "A", 55)],
        manual_added_count=0,
    )

    assert report["status"] == "failed"
    assert report["observed_topology_sg_sg_bond_count"] == 0
    assert report["observed_system_harmonic_sg_sg_bond_count"] == 0


def test_topology_validation_records_loader_and_patch_notes_as_non_authoritative():
    from mdclaw.amber.topology_validation import _build_topology_validation_report

    topology, system = _minimal_disulfide_topology_and_system(bonded=False)
    report = _build_topology_validation_report(
        topology=topology,
        system=system,
        position_count=2,
        minimization={
            "energy_is_finite": True,
            "positions_are_finite": True,
        },
        box_dimensions=None,
        canon_implicit=None,
        pablo_used=False,
        pablo_guardrail_codes=["pablo_topology_fallback"],
        patch_summary={
            "ligand_molecule_internal_bonds_added": 0,
            "template_internal_bonds_added": 2,
            "external_bonds_added": 0,
            "unpaired_external_atom_count": 0,
            "unpaired_lipid21_external_atom_count": 0,
            "nln_renamed_to_asn_count": 0,
            "orphan_glycam_residues_dropped_count": 0,
            "add_extra_particles_completed": True,
        },
        disulfide_bonds=None,
        manual_disulfide_added_count=0,
        non_authoritative_notes=["Pablo fallback was validated by final artifacts."],
    )

    assert report["status"] == "passed"
    assert report["loader"]["status"] == "fallback_validated"
    assert report["core"]["atom_count_preserved"] is True
    assert report["patches"]["template_internal_bonds_added"] == 2
    assert report["non_authoritative_notes"] == [
        "Pablo fallback was validated by final artifacts."
    ]
