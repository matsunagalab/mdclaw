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

    topology, system = _minimal_disulfide_topology_and_system(bonded=True)
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
        amber_variant_restore=None,
        non_authoritative_notes=["Pablo fallback was validated by final artifacts."],
    )

    assert report["status"] == "passed"
    assert report["loader"]["status"] == "fallback_validated"
    assert report["core"]["atom_count_preserved"] is True
    assert report["patches"]["template_internal_bonds_added"] == 2
    assert report["protonation_variants"]["status"] == "not_requested"
    assert report["non_authoritative_notes"] == [
        "Pablo fallback was validated by final artifacts."
    ]
