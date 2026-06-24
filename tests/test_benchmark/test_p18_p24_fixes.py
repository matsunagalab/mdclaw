"""Regression tests for the P18 (membrane) and P24 (biological assembly) fixes.

These cover the scorer/task changes that let a scientifically correct membrane
or assembly submission score full marks:

- the minimization-report gate accepts a huge *initial* (pre-minimization)
  energy while still bounding the *final* energy,
- lipid component counts tolerate truncated CHARMM lipid names and ignore small
  water/ion residues whose names can collide with truncated lipid aliases,
- the assembly chain count ignores cofactor/ligand chains via ``molecule_type``,
- the assembly PDB fallback counts polymer chains, not ligand-only chains,
- MDClaw's PDB writer preserves 4-character residue names across a round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path

from mdclaw.benchmark import scoring
from mdclaw.benchmark.models import DeterministicCheck


# ---------------------------------------------------------------------------
# Minimization-report energy gate


def _write_min_report(sub: Path, minimization: dict) -> dict:
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "minimization_report.json").write_text(
        json.dumps({"minimization": minimization})
    )
    manifest = {
        "outputs": {"minimization_report": "minimization_report.json"},
    }
    return manifest


_OK_FLAGS = {
    "attempted": True,
    "completed": True,
    "energy_is_finite": True,
    "positions_are_finite": True,
    "atom_count_preserved": True,
}


def test_min_report_accepts_huge_initial_energy(tmp_path: Path):
    # A freshly built membrane legitimately has an enormous pre-minimization
    # energy from packing clashes; only finiteness should be required there.
    manifest = _write_min_report(
        tmp_path,
        {**_OK_FLAGS,
         "energy_initial_kj_mol": 2.86e22,
         "energy_final_kj_mol": -827453.5},
    )
    check = DeterministicCheck(
        check_id="m", check_type="minimization_report_check",
    )
    passed, score, msg = scoring._check_minimization_report(
        check, tmp_path, manifest, {},
    )
    assert passed and score == 1.0, msg


def test_min_report_rejects_nonfinite_initial_energy(tmp_path: Path):
    manifest = _write_min_report(
        tmp_path,
        {**_OK_FLAGS,
         "energy_initial_kj_mol": float("inf"),
         "energy_final_kj_mol": -10.0},
    )
    check = DeterministicCheck(
        check_id="m", check_type="minimization_report_check",
    )
    passed, score, msg = scoring._check_minimization_report(
        check, tmp_path, manifest, {},
    )
    assert not passed and score == 0.0, msg


def test_min_report_still_bounds_final_energy(tmp_path: Path):
    # An implausibly large *final* energy is still rejected.
    manifest = _write_min_report(
        tmp_path,
        {**_OK_FLAGS,
         "energy_initial_kj_mol": -100.0,
         "energy_final_kj_mol": 2.86e22,
         "atom_count": 100},
    )
    check = DeterministicCheck(
        check_id="m", check_type="minimization_report_check",
    )
    passed, score, msg = scoring._check_minimization_report(
        check, tmp_path, manifest, {},
    )
    assert not passed and score == 0.0, msg


# ---------------------------------------------------------------------------
# Lipid component counting


def _atom_line(serial: int, atom: str, resname: str, chain: str,
               resseq: int) -> str:
    # 4-char residue names spill into column 21 (index 17:21), matching the
    # MDClaw long-resname PDB writer.
    rec = "HETATM"
    line = (
        f"{rec:<6}{serial:>5} {atom:<4}{'':1}{resname[:4]:<4}{chain:1}"
        f"{resseq:>4}{'':1}   "
        f"{0.0:>8.3f}{0.0:>8.3f}{0.0:>8.3f}{1.0:>6.2f}{0.0:>6.2f}"
    )
    return line


def _write_structure(path: Path, residues: list[tuple[str, str, int, int]]):
    """Write a PDB. ``residues`` = list of (resname, chain, resseq, n_atoms)."""
    lines = []
    serial = 1
    for resname, chain, resseq, n_atoms in residues:
        for a in range(n_atoms):
            lines.append(
                _atom_line(serial, f"C{a}", resname, chain, resseq)
            )
            serial += 1
    path.write_text("\n".join(lines) + "\nEND\n")


def test_lipid_counts_match_truncated_names_and_ignore_small_residues(
    tmp_path: Path,
):
    structure = tmp_path / "topology.pdb"
    # Big lipid residues written with the agent's last-3 truncation, plus small
    # water residues named "OPC" that must NOT be counted as POPC.
    residues = [
        ("OPC", "A", 1, 50), ("OPC", "A", 2, 50),  # 2 POPC lipids
        ("OPE", "B", 1, 50),                          # 1 POPE lipid
        ("HL1", "C", 1, 50),                          # 1 CHL1 lipid
        ("OPC", "W", 1, 3), ("OPC", "W", 2, 3),       # OPC water (small)
        ("OPC", "W", 3, 3), ("OPC", "W", 4, 3),
    ]
    _write_structure(structure, residues)
    check = DeterministicCheck(
        check_id="lip", check_type="structure_component_rescan",
        min_residue_atom_count=20,
        min_residue_counts={"POPC": 2, "POPE": 1, "CHL1": 1},
        residue_aliases={
            "POPC": ["PC", "OPC"],
            "POPE": ["PE", "OPE"],
            "CHL1": ["CHL", "CHOL", "HL1"],
        },
    )
    passed, score, msg = scoring._check_component_counts_for_structure(
        check, structure,
    )
    assert passed and score == 1.0, msg


def test_lipid_counts_reject_when_only_small_residues_present(tmp_path: Path):
    # Only small "OPC" water residues -> no lipids should be counted.
    structure = tmp_path / "topology.pdb"
    _write_structure(
        structure,
        [("OPC", "W", i, 3) for i in range(1, 6)],
    )
    check = DeterministicCheck(
        check_id="lip", check_type="structure_component_rescan",
        min_residue_atom_count=20,
        min_residue_counts={"POPC": 2},
        residue_aliases={"POPC": ["PC", "OPC"]},
    )
    passed, score, msg = scoring._check_component_counts_for_structure(
        check, structure,
    )
    assert not passed and score == 0.0, msg


def test_canonical_four_char_lipid_names_match(tmp_path: Path):
    structure = tmp_path / "topology.pdb"
    _write_structure(
        structure,
        [("POPC", "A", 1, 50), ("POPC", "A", 2, 50),
         ("POPE", "B", 1, 50), ("CHL1", "C", 1, 50)],
    )
    check = DeterministicCheck(
        check_id="lip", check_type="structure_component_rescan",
        min_residue_atom_count=20,
        min_residue_counts={"POPC": 2, "POPE": 1, "CHL1": 1},
    )
    passed, score, msg = scoring._check_component_counts_for_structure(
        check, structure,
    )
    assert passed and score == 1.0, msg


# ---------------------------------------------------------------------------
# Assembly polymer-chain counting


def _assembly_setup(tmp_path: Path, mapping: list[dict]) -> tuple[Path, dict, dict]:
    sub = tmp_path
    chains = sorted({m["output_chain_id"] for m in mapping})
    _write_structure(
        sub / "prepared_structure.pdb",
        [("ALA", ch, 1, 5) for ch in chains],
    )
    manifest = {"outputs": {"prepared_structure": "prepared_structure.pdb"}}
    metrics = {"preparation": {"assembly_id": "1",
                               "assembly_chain_identity_map": mapping}}
    return sub, manifest, metrics


def _assembly_check() -> DeterministicCheck:
    return DeterministicCheck(
        check_id="asm", check_type="assembly_identity_check",
        assembly_id_json_path="preparation.assembly_id",
        chain_identity_json_path="preparation.assembly_chain_identity_map",
        exact_chain_count=4,
        min_distinct_output_chains=4,
        min_mapping_entries=4,
        required_assembly_id="1",
        required_operator_ids=["1", "2", "3", "4"],
        require_output_chains_in_structure=True,
    )


def _map_entry(chain: str, op: str, mol_type: str | None) -> dict:
    entry = {
        "output_chain_id": chain,
        "source_pdb_id": "1STP",
        "assembly_id": "1",
        "source_auth_asym_id": "A",
        "source_label_asym_id": "A",
        "operator_id": op,
        "naming_policy": "short",
    }
    if mol_type is not None:
        entry["molecule_type"] = mol_type
    return entry


def test_assembly_ignores_ligand_chains_when_tagged(tmp_path: Path):
    mapping = [
        _map_entry("A", "1", "protein"), _map_entry("B", "2", "protein"),
        _map_entry("C", "3", "protein"), _map_entry("D", "4", "protein"),
        _map_entry("E", "1", "ligand"), _map_entry("F", "2", "ligand"),
        _map_entry("G", "3", "ligand"), _map_entry("H", "4", "ligand"),
    ]
    sub, manifest, metrics = _assembly_setup(tmp_path, mapping)
    passed, score, msg = scoring._check_assembly_identity(
        _assembly_check(), sub, manifest, metrics,
    )
    assert passed and score == 1.0, msg


def test_assembly_counts_all_chains_without_molecule_type(tmp_path: Path):
    # No molecule_type tags -> every mapped chain counts, so 8 != 4 fails.
    mapping = [
        _map_entry(ch, op, None)
        for ch, op in zip("ABCDEFGH", ["1", "2", "3", "4"] * 2)
    ]
    sub, manifest, metrics = _assembly_setup(tmp_path, mapping)
    passed, score, msg = scoring._check_assembly_identity(
        _assembly_check(), sub, manifest, metrics,
    )
    assert not passed and score == 0.0, msg


def test_assembly_fallback_counts_polymer_chains_not_ligand_chains(
    tmp_path: Path,
):
    _write_structure(
        tmp_path / "prepared_structure.pdb",
        [
            ("ALA", "A", 1, 5), ("ALA", "B", 1, 5),
            ("ALA", "C", 1, 5), ("ALA", "D", 1, 5),
            ("BTN", "E", 300, 32), ("BTN", "F", 300, 32),
            ("BTN", "G", 300, 32), ("BTN", "H", 300, 32),
        ],
    )
    manifest = {"outputs": {"prepared_structure": "prepared_structure.pdb"}}
    check = DeterministicCheck(
        check_id="asm",
        check_type="assembly_identity_check",
        exact_chain_count=4,
    )
    passed, score, msg = scoring._check_assembly_identity(
        check, tmp_path, manifest, {},
    )
    assert passed and score == 1.0, msg


# ---------------------------------------------------------------------------
# PDB writer preserves 4-character residue names


def test_preserve_long_resnames_round_trips_through_openmm(tmp_path: Path):
    import io

    from openmm import Vec3
    from openmm.app import Element, PDBFile, Topology
    from openmm.unit import nanometer

    from mdclaw.structure.pdb_utils import preserve_long_resnames_in_pdb_text

    topology = Topology()
    chain = topology.addChain("A")
    for name in ("POPC", "POPE", "CHL1", "ALA"):
        res = topology.addResidue(name, chain)
        topology.addAtom("N", Element.getBySymbol("N"), res)
    positions = [Vec3(i * 0.1, 0.0, 0.0) for i in range(4)] * nanometer

    buf = io.StringIO()
    PDBFile.writeFile(topology, positions, buf, keepIds=True)
    # OpenMM truncates 4-char names to their first three characters.
    assert "POP" in buf.getvalue() and "POPC" not in buf.getvalue()

    patched = preserve_long_resnames_in_pdb_text(buf.getvalue(), topology)
    reloaded = PDBFile(io.StringIO(patched))
    names = [r.name for r in reloaded.topology.residues()]
    assert names == ["POPC", "POPE", "CHL1", "ALA"], names
