"""Caps have to survive the trip through pdb2pqr -- and so does everything else.

PDBFixer inserts ACE/NME as heavy atoms only, and pdb2pqr has no topology for
either residue: it charges the atoms it recognises, finds the cap charge
non-integral, and gives up on the whole structure. A capped chain therefore
never reached the protonation baseline at all. These cover the two halves of
the fix - completing the cap hydrogens without protonating anything else, and
writing the cap atoms under the names pdb2pqr's AMBER.DAT actually uses.

They also pin the two things that made the first version of this fix wrong:
completing the hydrogens means loading the structure into OpenMM, which renames
Amber residue variants on the way in, and handing pdb2pqr the original file when
completion fails only reproduces the abort under a less informative code.
"""

import pytest

from mdclaw.structure.terminal_caps import (
    _pdb_atom_name_field,
    _prepare_terminal_caps_for_pdb2pqr,
    _rewrite_cap_atom_names_for_pdb2pqr,
)

# ACE + one residue + NME, heavy atoms only, exactly as PDBFixer leaves them.
CAPPED = """\
HETATM    1  C   ACE A  25      10.000  10.000  10.000  1.00  0.00           C
HETATM    2  O   ACE A  25      10.000  11.230  10.000  1.00  0.00           O
HETATM    3  CH3 ACE A  25       8.500  10.000  10.000  1.00  0.00           C
ATOM      4  N   ALA A  26      10.700   8.850  10.000  1.00  0.00           N
ATOM      5  CA  ALA A  26      12.150   8.700  10.000  1.00  0.00           C
ATOM      6  CB  ALA A  26      12.550   7.230  10.000  1.00  0.00           C
ATOM      7  C   ALA A  26      12.700   9.400  11.240  1.00  0.00           C
ATOM      8  O   ALA A  26      13.900   9.640  11.310  1.00  0.00           O
HETATM    9  N   NME A  27      11.850   9.740  12.220  1.00  0.00           N
HETATM   10  C   NME A  27      12.280  10.410  13.440  1.00  0.00           C
END
"""


def write(tmp_path, text, name="capped.pdb"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_a_four_character_name_takes_the_whole_atom_field():
    # HH31 has no room for the leading space that shorter names keep.
    assert _pdb_atom_name_field("HH31") == "HH31"
    assert _pdb_atom_name_field("C") == " C  "
    assert _pdb_atom_name_field("CH3") == " CH3"


def test_cap_atoms_are_renamed_to_the_names_pdb2pqr_charges(tmp_path):
    path = write(tmp_path, CAPPED)
    renamed = _rewrite_cap_atom_names_for_pdb2pqr(path)

    assert renamed == 1  # only NME's C -> CH3; the rest are hydrogens
    lines = path.read_text().splitlines()
    nme = [line for line in lines if line[17:20] == "NME"]
    assert [line[12:16].strip() for line in nme] == ["N", "CH3"]


def test_a_non_cap_residue_is_left_alone(tmp_path):
    path = write(tmp_path, CAPPED)
    _rewrite_cap_atom_names_for_pdb2pqr(path)

    ala = [line for line in path.read_text().splitlines() if line[17:20] == "ALA"]
    assert [line[12:16].strip() for line in ala] == ["N", "CA", "CB", "C", "O"]


def test_only_the_caps_come_back_protonated(tmp_path):
    pytest.importorskip("openmm.app")
    path = write(tmp_path, CAPPED)

    result = _prepare_terminal_caps_for_pdb2pqr(path, forcefield_name="ff19SB")

    assert result["success"], result["warnings"]
    # ACE gains three methyl hydrogens; NME gains its amide H and three more.
    assert result["cap_hydrogens_added"] == 7
    prepared = [
        line
        for line in open(result["output_file"]).read().splitlines()
        if line.startswith(("ATOM", "HETATM"))
    ]
    assert len(prepared) == 17
    # pdb2pqr still owns the protonation baseline, so nothing else gained a
    # hydrogen on the way in.
    assert not [
        line
        for line in prepared
        if line[76:78].strip() == "H" and line[17:20] not in ("ACE", "NME")
    ]


def test_an_uncapped_structure_is_passed_straight_through(tmp_path):
    uncapped = "\n".join(
        line for line in CAPPED.splitlines() if line[17:20] not in ("ACE", "NME")
    )
    result = _prepare_terminal_caps_for_pdb2pqr(write(tmp_path, uncapped + "\nEND\n"))

    assert result["success"] and result["skipped"]
    assert result["output_file"] is None


# A disulfide-bonded cysteine, named CYX by prep, between two caps.
CAPPED_CYX = """\
HETATM    1  C   ACE A  25      10.000  10.000  10.000  1.00  0.00           C
HETATM    2  O   ACE A  25      10.000  11.230  10.000  1.00  0.00           O
HETATM    3  CH3 ACE A  25       8.500  10.000  10.000  1.00  0.00           C
ATOM      4  N   CYX A  26      10.700   8.850  10.000  1.00  0.00           N
ATOM      5  CA  CYX A  26      12.150   8.700  10.000  1.00  0.00           C
ATOM      6  CB  CYX A  26      12.550   7.230  10.000  1.00  0.00           C
ATOM      7  SG  CYX A  26      14.330   7.010  10.000  1.00  0.00           S
ATOM      8  C   CYX A  26      12.700   9.400  11.240  1.00  0.00           C
ATOM      9  O   CYX A  26      13.900   9.640  11.310  1.00  0.00           O
HETATM   10  N   NME A  27      11.850   9.740  12.220  1.00  0.00           N
HETATM   11  C   NME A  27      12.280  10.410  13.440  1.00  0.00           C
END
"""


def test_completion_does_not_rename_the_disulfide_cysteine(tmp_path):
    # OpenMM's loader normalises CYX to CYS. Writing that back out would hand
    # pdb2pqr a structure whose cysteines are no longer the ones prep bonded,
    # and pdb2pqr would silently re-derive them from geometry instead.
    pytest.importorskip("openmm.app")
    path = write(tmp_path, CAPPED_CYX, "capped_cyx.pdb")

    result = _prepare_terminal_caps_for_pdb2pqr(path, forcefield_name="ff19SB")

    assert result["success"], result["errors"]
    assert result["resnames_restored"]
    prepared = open(result["output_file"]).read().splitlines()
    resnames = {line[17:20] for line in prepared if line.startswith(("ATOM", "HETATM"))}
    assert "CYX" in resnames
    assert "CYS" not in resnames
    # and the caps were still completed, without protonating the cysteine
    assert result["cap_hydrogens_added"] == 7
    assert not [
        line for line in prepared
        if line[76:78].strip() == "H" and line[17:20] not in ("ACE", "NME")
    ]


def test_an_unresolvable_forcefield_fails_instead_of_passing_the_file_on(tmp_path):
    result = _prepare_terminal_caps_for_pdb2pqr(
        write(tmp_path, CAPPED), forcefield_name="no-such-forcefield")

    assert not result["success"]
    assert result["code"] == "terminal_cap_hydrogen_completion_unavailable"
    assert result["output_file"] is None
    assert result["errors"]


def test_a_failed_completion_fails_instead_of_passing_the_file_on(
        tmp_path, monkeypatch):
    pytest.importorskip("openmm.app")
    import openmm.app as app

    def explode(self, *args, **kwargs):
        raise RuntimeError("no hydrogens for you")

    monkeypatch.setattr(app.Modeller, "addHydrogens", explode)
    result = _prepare_terminal_caps_for_pdb2pqr(
        write(tmp_path, CAPPED), forcefield_name="ff19SB")

    assert not result["success"]
    assert result["code"] == "terminal_cap_hydrogen_completion_failed"
    assert result["output_file"] is None


def test_a_failed_resname_restore_fails_rather_than_renaming_residues(
        tmp_path, monkeypatch):
    # If the restore cannot be applied, the alternative is handing pdb2pqr a
    # structure with silently renamed residues. Refuse instead.
    pytest.importorskip("openmm.app")
    monkeypatch.setattr(
        "mdclaw.structure.terminal_caps.restore_resnames_by_residue_key",
        lambda *a, **k: None)
    result = _prepare_terminal_caps_for_pdb2pqr(
        write(tmp_path, CAPPED), forcefield_name="ff19SB")

    assert not result["success"]
    assert result["code"] == "terminal_cap_hydrogen_completion_failed"
    assert any("restore residue names" in e for e in result["errors"])


@pytest.mark.parametrize("code", [
    "terminal_cap_hydrogen_completion_unavailable",
    "terminal_cap_hydrogen_completion_failed",
])
def test_clean_protein_stops_instead_of_running_pdb2pqr_on_the_original(
        tmp_path, monkeypatch, code):
    # The call site is the half of the fix the helper tests cannot reach:
    # before, a failed preparation fell through and pdb2pqr ran on the
    # uncorrected file, reporting the cap problem as protonation_method_failed.
    pytest.importorskip("openmm.app")
    # mdclaw.structure re-exports the clean_protein *function* under the same
    # name as its module, so plain import binds the function. Ask importlib for
    # the module itself.
    import importlib
    cp = importlib.import_module("mdclaw.structure.clean_protein")

    monkeypatch.setattr(
        cp, "_prepare_terminal_caps_for_pdb2pqr",
        lambda *a, **k: {"success": False, "code": code,
                         "errors": ["forced failure"], "warnings": [],
                         "output_file": None})

    def refuse(*args, **kwargs):
        raise AssertionError("pdb2pqr must not run after cap preparation failed")

    monkeypatch.setattr(cp.pdb2pqr_wrapper, "run", refuse)
    monkeypatch.setattr(cp.pdb2pqr_wrapper, "is_available", lambda: True)

    result = cp.clean_protein(
        str(write(tmp_path, CAPPED_CYX, "capped_cyx.pdb")),
        cap_termini=True,
    )

    assert result["code"] == code
    assert any("forced failure" in e for e in result["errors"])
    assert any(op.get("step") == "terminal_cap_pdb2pqr_preparation"
               and op.get("status") == "error"
               for op in result["operations"])


# A cap can arrive with the structure rather than be added by us, and it can
# arrive complete, half-finished, or on one terminus only. Those all reach the
# same helper, because it triggers on caps being present.
def _pdb(atoms):
    out = []
    for i, (name, resname, resseq, x, y, z, element) in enumerate(atoms, start=1):
        field = name.ljust(4)[:4] if len(name) >= 4 else f" {name}".ljust(4)
        record = "HETATM" if resname in ("ACE", "NME") else "ATOM  "
        out.append(f"{record}{i:5d} {field} {resname:>3} A{resseq:4d}    "
                   f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {element:>2}")
    return "\n".join(out) + "\nEND\n"


_ALA = [("N", "ALA", 26, 10.700, 8.850, 10.000, "N"),
        ("CA", "ALA", 26, 12.150, 8.700, 10.000, "C"),
        ("CB", "ALA", 26, 12.550, 7.230, 10.000, "C"),
        ("C", "ALA", 26, 12.700, 9.400, 11.240, "C"),
        ("O", "ALA", 26, 13.900, 9.640, 11.310, "O")]
_ACE_COMPLETE = [("C", "ACE", 25, 10.000, 10.000, 10.000, "C"),
                 ("O", "ACE", 25, 10.000, 11.230, 10.000, "O"),
                 ("CH3", "ACE", 25, 8.500, 10.000, 10.000, "C"),
                 ("H1", "ACE", 25, 8.170, 9.000, 9.720, "H"),
                 ("H2", "ACE", 25, 8.130, 10.730, 9.280, "H"),
                 ("H3", "ACE", 25, 8.100, 10.250, 10.990, "H")]
_ACE_COMPLETE_AMBER = [("C", "ACE", 25, 10.000, 10.000, 10.000, "C"),
                       ("O", "ACE", 25, 10.000, 11.230, 10.000, "O"),
                       ("CH3", "ACE", 25, 8.500, 10.000, 10.000, "C"),
                       ("HH31", "ACE", 25, 8.170, 9.000, 9.720, "H"),
                       ("HH32", "ACE", 25, 8.130, 10.730, 9.280, "H"),
                       ("HH33", "ACE", 25, 8.100, 10.250, 10.990, "H")]
_NME_COMPLETE = [("N", "NME", 27, 11.850, 9.740, 12.220, "N"),
                 ("H", "NME", 27, 10.900, 9.520, 12.020, "H"),
                 ("C", "NME", 27, 12.280, 10.410, 13.440, "C"),
                 ("H1", "NME", 27, 11.500, 10.500, 14.190, "H"),
                 ("H2", "NME", 27, 13.130, 9.890, 13.870, "H"),
                 ("H3", "NME", 27, 12.580, 11.400, 13.140, "H")]
_NME_HEAVY = [("N", "NME", 27, 11.850, 9.740, 12.220, "N"),
              ("C", "NME", 27, 12.280, 10.410, 13.440, "C")]
_OXT = [("OXT", "ALA", 26, 12.100, 9.900, 12.200, "O")]


def _prepare(tmp_path, text, name):
    pytest.importorskip("openmm.app")
    return _prepare_terminal_caps_for_pdb2pqr(
        write(tmp_path, text, name), forcefield_name="ff19SB")


def _residue_atom_keys(path):
    return [(line[17:20], line[22:26], line[12:16])
            for line in open(path).read().splitlines()
            if line.startswith(("ATOM", "HETATM"))]


def test_a_cap_that_arrives_complete_gains_no_hydrogens(tmp_path):
    result = _prepare(tmp_path, _pdb(_ACE_COMPLETE + _ALA + _NME_COMPLETE),
                      "complete.pdb")

    assert result["success"], result["errors"]
    assert result["cap_hydrogens_added"] == 0
    keys = _residue_atom_keys(result["output_file"])
    assert len(keys) == len(set(keys)), "an arriving cap was duplicated"


def test_a_cap_that_arrives_already_amber_named_is_not_duplicated(tmp_path):
    # The rename maps OpenMM names onto AMBER ones. A cap deposited under the
    # AMBER names must not end up with both spellings.
    result = _prepare(tmp_path, _pdb(_ACE_COMPLETE_AMBER + _ALA + _NME_COMPLETE),
                      "amber_named.pdb")

    assert result["success"], result["errors"]
    keys = _residue_atom_keys(result["output_file"])
    assert len(keys) == len(set(keys))
    ace = [k[2].strip() for k in keys if k[0] == "ACE"]
    assert sorted(ace) == ["C", "CH3", "HH31", "HH32", "HH33", "O"]


def test_a_half_finished_cap_is_completed(tmp_path):
    # ACE arrives complete, NME arrives heavy-only: only the second is finished.
    result = _prepare(tmp_path, _pdb(_ACE_COMPLETE + _ALA + _NME_HEAVY),
                      "half.pdb")

    assert result["success"], result["errors"]
    assert result["cap_hydrogens_added"] == 4  # NME's H plus three methyl H


def test_a_cap_on_one_terminus_only_is_handled(tmp_path):
    # An N-terminal cap with a genuine free C-terminus (OXT present).
    result = _prepare(tmp_path, _pdb(_ACE_COMPLETE + _ALA + _OXT), "ace_only.pdb")

    assert result["success"], result["errors"]
    assert {k[0] for k in _residue_atom_keys(result["output_file"])} == {"ACE", "ALA"}


def test_stripping_the_caps_leaves_nothing_for_the_helper_to_do(tmp_path):
    # strip_input_caps runs before this helper, so the helper should see an
    # uncapped structure and skip, leaving pdb2pqr on the original file.
    from mdclaw.structure.terminal_caps import strip_input_terminal_caps

    capped = write(tmp_path, _pdb(_ACE_COMPLETE + _ALA + _NME_COMPLETE), "capped.pdb")
    stripped = tmp_path / "stripped.pdb"
    removal = strip_input_terminal_caps(str(capped), str(stripped))

    assert {c["resname"] for c in removal["removed"]} == {"ACE", "NME"}
    result = _prepare_terminal_caps_for_pdb2pqr(stripped, forcefield_name="ff19SB")
    assert result["success"] and result["skipped"]
    assert result["output_file"] is None
