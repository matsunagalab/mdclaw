"""Tests for site-specific residue protonation overrides."""

import textwrap

from mdclaw.structure.protonation import (
    _apply_protonation_states_with_modeller,
    _extract_non_default_protonation_states,
    _merge_protonation_states,
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


# ── reporting non-default protonation ─────────────────────────────────────


MIXED_STATES_PDB = textwrap.dedent("""\
ATOM      1  N   ASH A  97       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ASH A  97       1.450   0.000   0.000  1.00  0.00           C
ATOM      3  N   HID A  77       0.000   0.000   0.000  1.00  0.00           N
ATOM      4  N   GLH B 210       0.000   0.000   0.000  1.00  0.00           N
ATOM      5  N   LYN B 211A      0.000   0.000   0.000  1.00  0.00           N
ATOM      6  N   CYM B 212       0.000   0.000   0.000  1.00  0.00           N
ATOM      7  N   ASP A  98       0.000   0.000   0.000  1.00  0.00           N
TER
END
""")


def test_extract_reports_non_default_states_but_not_histidine(tmp_path):
    # Histidine is reported separately by _extract_histidine_states: every HIS
    # carries a tautomer, so it says nothing about a charge change.
    pdb = tmp_path / "mixed.pdb"
    pdb.write_text(MIXED_STATES_PDB)

    states = _extract_non_default_protonation_states(pdb)

    assert [(s["chain"], s["resnum"], s["state"]) for s in states] == [
        ("A", "97", "ASH"),
        ("B", "210", "GLH"),
        ("B", "211", "LYN"),
        ("B", "212", "CYM"),
    ]
    assert all(s["source"] == "auto_detected" for s in states)
    assert states[0]["default_state"] == "ASP"
    assert states[2]["icode"] == "A"


def test_extract_reports_each_residue_once(tmp_path):
    pdb = tmp_path / "repeat.pdb"
    pdb.write_text(
        "ATOM      1  N   ASH A  97       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  CA  ASH A  97       1.450   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )

    assert len(_extract_non_default_protonation_states(pdb)) == 1


def test_extract_survives_an_unreadable_file(tmp_path):
    assert _extract_non_default_protonation_states(tmp_path / "absent.pdb") == []


def test_merge_prefers_the_caller_choice_over_what_was_detected():
    detected = [
        {"chain": "A", "resnum": "97", "icode": "", "state": "ASH",
         "default_state": "ASP", "source": "auto_detected"},
        {"chain": "A", "resnum": "112", "icode": "", "state": "ASH",
         "default_state": "ASP", "source": "auto_detected"},
    ]
    requested = [{"chain": "A", "resnum": "97", "icode": "", "state": "ASP"}]

    merged = _merge_protonation_states(detected, requested)

    # A:97 appears once, as the caller's decision; A:112 stays auto-detected.
    assert [(m["resnum"], m["state"], m["source"]) for m in merged] == [
        ("97", "ASP", "user_override"),
        ("112", "ASH", "auto_detected"),
    ]


def test_merge_handles_either_side_being_empty():
    detected = [{"chain": "A", "resnum": "97", "icode": "", "state": "ASH",
                 "default_state": "ASP", "source": "auto_detected"}]

    assert _merge_protonation_states(detected, []) == detected
    assert _merge_protonation_states([], []) == []
    assert _merge_protonation_states(
        [], [{"chain": "A", "resnum": "5", "state": "ASP"}]
    )[0]["source"] == "user_override"


def test_states_already_in_the_input_are_marked_as_such(tmp_path):
    # A state that arrived with the structure will not move when the caller
    # changes --ph, so it is reported differently from one pdb2pqr assigned.
    pdb = tmp_path / "out.pdb"
    pdb.write_text(
        "ATOM      1  N   ASH A  97       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  N   GLH A 210       0.000   0.000   0.000  1.00  0.00           N\n"
        "END\n"
    )

    states = _extract_non_default_protonation_states(
        pdb, preexisting={("A", "97", "")}
    )

    assert [(s["resnum"], s["source"]) for s in states] == [
        ("97", "from_input_structure"),
        ("210", "auto_detected"),
    ]


def test_no_preexisting_set_means_everything_is_newly_assigned(tmp_path):
    pdb = tmp_path / "out.pdb"
    pdb.write_text(
        "ATOM      1  N   ASH A  97       0.000   0.000   0.000  1.00  0.00           N\nEND\n"
    )

    assert _extract_non_default_protonation_states(pdb)[0]["source"] == "auto_detected"
    assert _extract_non_default_protonation_states(
        pdb, preexisting=set()
    )[0]["source"] == "auto_detected"
