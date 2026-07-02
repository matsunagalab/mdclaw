"""Tests for site-specific residue protonation overrides."""

import textwrap

from mdclaw.structure.protonation import (
    _apply_protonation_states_with_modeller,
    _normalize_protonation_state_overrides,
)


ASP_HEAVY_PDB = textwrap.dedent("""\
ATOM      1  N   ASP A  25       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ASP A  25       1.450   0.000   0.000  1.00  0.00           C
ATOM      3  C   ASP A  25       2.000   1.400   0.000  1.00  0.00           C
ATOM      4  O   ASP A  25       1.300   2.400   0.000  1.00  0.00           O
ATOM      5  CB  ASP A  25       2.000  -0.800  -1.200  1.00  0.00           C
ATOM      6  CG  ASP A  25       3.500  -0.800  -1.200  1.00  0.00           C
ATOM      7  OD1 ASP A  25       4.100   0.200  -1.200  1.00  0.00           O
ATOM      8  OD2 ASP A  25       4.100  -1.900  -1.200  1.00  0.00           O
TER
END
""")


def test_normalize_protonation_state_dict_and_legacy_histidine():
    records = _normalize_protonation_state_overrides(
        protonation_states={"A:25": "ash"},
        histidine_states={"A:57": "HSP"},
    )

    assert records == [
        {"chain": "A", "resnum": "25", "icode": "", "state": "ASH"},
        {"chain": "A", "resnum": "57", "icode": "", "state": "HIP"},
    ]


def test_modeller_rebuilds_ash_and_stamps_residue_name(tmp_path):
    pdb = tmp_path / "asp.pdb"
    pdb.write_text(ASP_HEAVY_PDB)

    result = _apply_protonation_states_with_modeller(
        pdb,
        [{"chain": "A", "resnum": "25", "state": "ASH"}],
        ph=7.4,
    )

    assert result["success"], result["errors"]
    assert result["applied_states"] == [
        {
            "chain": "A",
            "resnum": "25",
            "icode": "",
            "state": "ASH",
            "modeller_variant": "ASH",
        }
    ]
    text = pdb.read_text()
    assert " ASH A  25" in text
    assert " HD2 ASH A  25" in text
    assert "HETATM" not in text
