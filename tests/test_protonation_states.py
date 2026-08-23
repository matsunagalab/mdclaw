"""Tests for site-specific residue protonation overrides."""

import importlib
import textwrap
from pathlib import Path

import pytest

from mdclaw.structure.protonation import (
    _apply_protonation_states_with_modeller,
    _extract_non_default_protonation_states,
    _extract_input_protonation_state_overrides,
    _merge_input_protonation_state_overrides,
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


def test_input_pdb_promotes_states_but_leaves_cyx_to_disulfide_contract(tmp_path):
    pdb = tmp_path / "input.pdb"
    pdb.write_text(
        MIXED_STATES_PDB.replace(
            "TER\n",
            "ATOM      8  SG  CYX C 300       0.000   0.000   0.000  1.00  0.00           S\n"
            "TER\n",
        )
    )

    states = _extract_input_protonation_state_overrides(pdb)

    assert [(s["state"], s["chain"], s["resnum"], s["icode"]) for s in states] == [
        ("ASH", "A", "97", ""),
        ("GLH", "B", "210", ""),
        ("LYN", "B", "211", "A"),
        ("CYM", "B", "212", ""),
    ]
    assert all(s["source"] == "user_override" for s in states)
    assert all(s["input_state_preserved"] is True for s in states)
    assert "CYX" not in {s["state"] for s in states}
    assert not {"HID", "HIE", "HIP"} & {s["state"] for s in states}


def test_input_mmcif_promotes_nondefault_states(tmp_path):
    gemmi = pytest.importorskip("gemmi")
    structure = gemmi.Structure()
    model = gemmi.Model("1")
    chain = gemmi.Chain("Q")
    for num, icode, name in ((12, " ", "ASH"), (13, "B", "LYN"), (14, " ", "CYX")):
        residue = gemmi.Residue()
        residue.name = name
        residue.seqid = gemmi.SeqId(num, icode)
        atom = gemmi.Atom()
        atom.name = "CA"
        atom.element = gemmi.Element("C")
        residue.add_atom(atom)
        chain.add_residue(residue)
    model.add_chain(chain)
    structure.add_model(model)
    cif = tmp_path / "input.cif"
    structure.make_mmcif_document().write_file(str(cif))

    states = _extract_input_protonation_state_overrides(cif)

    assert [(s["state"], s["chain"], s["resnum"], s["icode"]) for s in states] == [
        ("ASH", "Q", "12", ""),
        ("LYN", "Q", "13", "B"),
    ]


def test_explicit_override_wins_over_input_derived_state():
    promoted = [{
        "chain": "A",
        "resnum": "25",
        "icode": "",
        "state": "ASH",
        "input_state_preserved": True,
    }]
    explicit = _normalize_protonation_state_overrides(
        protonation_states={"A:25": "ASP"}
    )

    merged = _merge_input_protonation_state_overrides(promoted, explicit)

    assert merged == [{
        "chain": "A",
        "resnum": "25",
        "icode": "",
        "state": "ASP",
    }]


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


def test_extract_reports_assigned_states_as_auto_detected(tmp_path):
    pdb = tmp_path / "out.pdb"
    pdb.write_text(
        "ATOM      1  N   ASH A  97       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  N   GLH A 210       0.000   0.000   0.000  1.00  0.00           N\n"
        "END\n"
    )

    states = _extract_non_default_protonation_states(pdb)

    assert [(s["resnum"], s["source"]) for s in states] == [
        ("97", "auto_detected"),
        ("210", "auto_detected"),
    ]


@pytest.mark.parametrize(
    ("pathway", "input_format"),
    [
        ("pdb2pqr", "pdb"),
        ("pdb4amber", "pdb"),
        ("pdb2pqr", "mmcif"),
    ],
)
def test_input_state_is_reapplied_in_both_protonation_paths(
    tmp_path,
    monkeypatch,
    pathway,
    input_format,
):
    clean_module = importlib.import_module("mdclaw.structure.clean_protein")
    source = tmp_path / "input_ash.pdb"
    source.write_text(ASP_HEAVY_PDB.replace(" ASP ", " ASH "))
    if input_format == "mmcif":
        gemmi = pytest.importorskip("gemmi")
        structure = gemmi.read_structure(str(source))
        source = tmp_path / "input_ash.cif"
        structure.make_mmcif_document().write_file(str(source))

    def fake_pdb2pqr(args):
        output = Path(args[args.index("--pdb-output") + 1])
        output.write_text(Path(args[0]).read_text())

    def fake_pdb4amber(args):
        output = Path(args[args.index("-o") + 1])
        output.write_text(Path(args[args.index("-i") + 1]).read_text())

    monkeypatch.setattr(
        clean_module.pdb2pqr_wrapper,
        "is_available",
        lambda: pathway == "pdb2pqr",
    )
    monkeypatch.setattr(clean_module.pdb2pqr_wrapper, "run", fake_pdb2pqr)
    monkeypatch.setattr(clean_module.pdb4amber_wrapper, "is_available", lambda: True)
    monkeypatch.setattr(clean_module.pdb4amber_wrapper, "run", fake_pdb4amber)

    result = clean_module.clean_protein(
        str(source),
        add_missing_atoms=False,
    )

    assert result["success"], result["errors"]
    preserved = [
        state
        for state in result["protonation_states"]
        if state.get("input_state_preserved")
    ]
    assert len(preserved) == 1
    assert preserved[0]["state"] == "ASH"
    assert preserved[0]["source"] == "user_override"
    assert preserved[0]["override_origin"] == "input_structure"
    assert result["input_protonation_states_promoted"][0]["state"] == "ASH"
    assert "openmm_modeller_user_states" in result["protonation_method"]


def test_pdb4amber_fallback_reports_detected_nondefault_states(tmp_path, monkeypatch):
    clean_module = importlib.import_module("mdclaw.structure.clean_protein")
    source = tmp_path / "protein.pdb"
    source.write_text(
        "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  CA  ALA A   1       1.450   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  C   ALA A   1       2.000   1.400   0.000  1.00  0.00           C\n"
        "ATOM      4  O   ALA A   1       1.300   2.400   0.000  1.00  0.00           O\n"
        "END\n"
    )

    monkeypatch.setattr(clean_module.pdb2pqr_wrapper, "is_available", lambda: False)
    monkeypatch.setattr(clean_module.pdb4amber_wrapper, "is_available", lambda: True)

    def fake_pdb4amber(args):
        output = args[args.index("-o") + 1]
        with open(output, "w") as handle:
            handle.write(
                "ATOM      1  N   ASH A   1       0.000   0.000   0.000  1.00  0.00           N\n"
                "END\n"
            )

    monkeypatch.setattr(clean_module.pdb4amber_wrapper, "run", fake_pdb4amber)

    result = clean_module.clean_protein(
        str(source),
        add_missing_atoms=False,
        add_hydrogens=False,
    )

    assert result["success"] is True
    assert result["protonation_states"] == [{
        "chain": "A",
        "resnum": "1",
        "icode": "",
        "state": "ASH",
        "default_state": "ASP",
        "source": "auto_detected",
    }]
    op = next(
        item
        for item in result["operations"]
        if item.get("method") == "pdb4amber+reduce"
    )
    assert op["protonation_states"] == result["protonation_states"]


def test_pdb4amber_fallback_scans_after_user_state_rewrite(tmp_path, monkeypatch):
    clean_module = importlib.import_module("mdclaw.structure.clean_protein")
    source = tmp_path / "protein.pdb"
    source.write_text(
        "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  CA  ALA A   1       1.450   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  C   ALA A   1       2.000   1.400   0.000  1.00  0.00           C\n"
        "ATOM      4  O   ALA A   1       1.300   2.400   0.000  1.00  0.00           O\n"
        "END\n"
    )
    monkeypatch.setattr(clean_module.pdb2pqr_wrapper, "is_available", lambda: False)
    monkeypatch.setattr(clean_module.pdb4amber_wrapper, "is_available", lambda: True)

    def fake_pdb4amber(args):
        output = args[args.index("-o") + 1]
        with open(output, "w") as handle:
            handle.write(
                "ATOM      1  N   ASP A   1       0.000   0.000   0.000  1.00  0.00           N\n"
                "END\n"
            )

    def fake_apply(pdb_file, protonation_states, *, ph):
        del protonation_states, ph
        pdb_file.write_text(
            "ATOM      1  N   ASH A   1       0.000   0.000   0.000  1.00  0.00           N\n"
            "END\n"
        )
        return {
            "success": True,
            "errors": [],
            "warnings": [],
            "applied_states": [{
                "chain": "A",
                "resnum": "1",
                "icode": "",
                "state": "ASH",
            }],
        }

    scanned_text = []

    def recording_extract(pdb_file):
        scanned_text.append(pdb_file.read_text())
        return _extract_non_default_protonation_states(pdb_file)

    monkeypatch.setattr(clean_module.pdb4amber_wrapper, "run", fake_pdb4amber)
    monkeypatch.setattr(
        clean_module,
        "_apply_protonation_states_with_modeller",
        fake_apply,
    )
    monkeypatch.setattr(
        clean_module,
        "_extract_non_default_protonation_states",
        recording_extract,
    )

    result = clean_module.clean_protein(
        str(source),
        add_missing_atoms=False,
        add_hydrogens=False,
        protonation_states={"A:1": "ASH"},
    )

    assert result["success"] is True
    assert len(scanned_text) == 1
    assert " ASH A   1" in scanned_text[0]


# --- a variant asked back to its parent ---------------------------------------
# PDBFile's reader normalises a variant onto its parent, so an ASH loads as an
# ASP with HD2 still on it. Comparing the requested state against that name made
# "give me ASP" a no-op on exactly the residues that needed it: the call reported
# success and already_in_requested_state and left the file protonated. Measured
# on 5ZK8, where propka had neutralised two aspartates the reference kept
# charged and the override to put them back did nothing. ASH/GLH/LYN/CYM were
# all affected; the other direction, ASP -> ASH, always worked.

ASH_PDB = textwrap.dedent("""\
ATOM      1  N   ASH A  69       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ASH A  69       1.450   0.000   0.000  1.00  0.00           C
ATOM      3  C   ASH A  69       2.000   1.400   0.000  1.00  0.00           C
ATOM      4  O   ASH A  69       1.300   2.400   0.000  1.00  0.00           O
ATOM      5  CB  ASH A  69       2.000  -0.800  -1.200  1.00  0.00           C
ATOM      6  CG  ASH A  69       3.500  -0.800  -1.200  1.00  0.00           C
ATOM      7  OD1 ASH A  69       4.100   0.200  -1.200  1.00  0.00           O
ATOM      8  OD2 ASH A  69       4.100  -1.900  -1.200  1.00  0.00           O
ATOM      9  HD2 ASH A  69       5.060  -1.900  -1.200  1.00  0.00           H
TER
END
""")


def _names(path):
    return {line[22:26].strip(): line[17:20].strip()
            for line in path.read_text().splitlines() if line.startswith("ATOM")}


def _atom_names(path):
    return {line[12:16].strip() for line in path.read_text().splitlines()
            if line.startswith("ATOM")}


def _atom_list(path):
    return [line[12:16].strip() for line in path.read_text().splitlines()
            if line.startswith("ATOM")]


def test_asking_a_protonated_aspartate_back_to_asp_deprotonates_it(tmp_path):
    pdb = tmp_path / "ash.pdb"
    pdb.write_text(ASH_PDB)
    assert _names(pdb) == {"69": "ASH"} and "HD2" in _atom_names(pdb)

    result = _apply_protonation_states_with_modeller(
        pdb, [{"chain": "A", "resnum": "69", "icode": "", "state": "ASP"}], ph=7.4)

    assert result["success"] and not result["errors"]
    assert _names(pdb) == {"69": "ASP"}
    assert "HD2" not in _atom_names(pdb)
    applied, = result["applied_states"]
    assert not applied.get("already_in_requested_state")


def test_asking_for_the_state_a_residue_already_holds_is_still_a_no_op(tmp_path):
    """The guard the comparison was written for, on the name that is really there."""
    pdb = tmp_path / "asp.pdb"
    pdb.write_text(ASP_HEAVY_PDB)
    before = _atom_names(pdb)

    result = _apply_protonation_states_with_modeller(
        pdb, [{"chain": "A", "resnum": "25", "icode": "", "state": "ASP"}], ph=7.4)

    assert result["success"]
    applied, = result["applied_states"]
    assert applied["already_in_requested_state"] is True
    assert _names(pdb) == {"25": "ASP"}
    # The fault the guard was written for: renaming a residue to its own base
    # and handing it back to addHydrogens re-added the whole set, duplicating
    # H, HA, HB2 and HB3. Hydrogens the input never had may still be built.
    names = _atom_list(pdb)
    assert len(names) == len(set(names)), "no atom duplicated"
    assert before <= set(names), "no heavy atom lost"
