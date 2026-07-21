from pathlib import Path

from mdclaw.amber.openmm_build import (
    _pdb_residue_names_4char,
    _select_lipid21_xml_key_for_pdb,
)


def _atom_line(serial: int, atom: str, resname: str, chain: str, resseq: int) -> str:
    return (
        f"{'HETATM':<6}{serial:>5} {atom:<4}{'':1}{resname[:4]:<4}{chain:1}"
        f"{resseq:>4}{'':1}   "
        f"{0.0:>8.3f}{0.0:>8.3f}{0.0:>8.3f}{1.0:>6.2f}{0.0:>6.2f}"
    )


def _write_pdb(path: Path, residue_names: list[str]) -> None:
    lines = [
        _atom_line(index, "C1", resname, "A", index)
        for index, resname in enumerate(residue_names, start=1)
    ]
    path.write_text("\n".join(lines) + "\nEND\n")


def test_lipid21_xml_selection_detects_full_residue_lipid_names(tmp_path: Path):
    pdb = tmp_path / "full_lipids.pdb"
    _write_pdb(pdb, ["POPC", "POPE", "CHL1"])

    assert {"POPC", "POPE", "CHL1"} <= _pdb_residue_names_4char(pdb)
    assert _select_lipid21_xml_key_for_pdb(pdb) == "lipid21_full"


def test_lipid21_xml_selection_covers_all_full_residue_families(tmp_path: Path):
    pdb = tmp_path / "full_lipid_families.pdb"
    _write_pdb(pdb, ["DOPC", "DOPG", "POPS", "DAPA", "SDPS"])

    assert _select_lipid21_xml_key_for_pdb(pdb) == "lipid21_full"


def test_lipid21_xml_selection_keeps_modular_lipid21_default(tmp_path: Path):
    pdb = tmp_path / "modular_lipids.pdb"
    _write_pdb(pdb, ["PA", "PC", "PE", "OL", "CHL"])

    assert {"PA", "PC", "PE", "OL", "CHL"} <= _pdb_residue_names_4char(pdb)
    assert _select_lipid21_xml_key_for_pdb(pdb) == "lipid21"
