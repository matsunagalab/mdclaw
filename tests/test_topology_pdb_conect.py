"""Final topology PDBs must be readable without trusting PDB CONECT."""

from pathlib import Path

import pytest

from mdclaw.structure.pdb_utils import strip_conect_records_from_pdb_text


def test_strip_conect_records_preserves_every_other_record_and_line_ending():
    pdb_text = (
        "HEADER    TEST\r\n"
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000\r\n"
        "CONECT    1    2\r\n"
        "CONECTA003BA003C\r\n"
        "END"
    )

    assert strip_conect_records_from_pdb_text(pdb_text) == (
        "HEADER    TEST\r\n"
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000\r\n"
        "END"
    )


@pytest.mark.parametrize(
    "builder",
    ["amber/openmm_build.py", "openmm_system/build.py"],
)
def test_both_topology_contract_builders_strip_conect(builder):
    source = Path(__file__).resolve().parent.parent / "mdclaw" / builder
    text = source.read_text()

    assert "topology_pdb_text = strip_conect_records_from_pdb_text(" in text
    assert "strip_conect_records_from_pdb_text(\n            preserve_long_resnames" in text
