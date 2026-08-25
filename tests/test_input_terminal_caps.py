"""Terminal caps that arrive with the structure, rather than ones we add.

A deposit can be capped already - 1DFJ chain I carries an N-terminal ACE - and
ACE/NME are classified as protein, so preparation keeps them and counts them as
residues. These cover the two things that were missing: seeing that the input
was capped, and being able to simulate the chain without it.
"""

from mdclaw.structure.terminal_caps import (
    detect_input_terminal_caps,
    strip_input_terminal_caps,
)

CAPPED = """\
ATOM      1  CH3 ACE I   1      10.000  10.000  10.000  1.00  0.00           C
ATOM      2  C   ACE I   1      11.000  10.000  10.000  1.00  0.00           C
ATOM      3  N   MET I   1      12.000  10.000  10.000  1.00  0.00           N
ATOM      4  CA  MET I   1      13.000  10.000  10.000  1.00  0.00           C
ATOM      5  N   ALA I   2      14.000  10.000  10.000  1.00  0.00           N
ATOM      6  CA  ALA I   2      15.000  10.000  10.000  1.00  0.00           C
ATOM      7  N   GLY I   3      16.000  10.000  10.000  1.00  0.00           N
ATOM      8  CA  GLY I   3      17.000  10.000  10.000  1.00  0.00           C
END
"""


def write(tmp_path, text, name="capped.pdb"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_an_n_terminal_cap_on_the_input_is_reported(tmp_path):
    caps = detect_input_terminal_caps(write(tmp_path, CAPPED))
    assert [(c["chain"], c["resname"], c["terminus"]) for c in caps] == [
        ("I", "ACE", "n")
    ]


def test_an_uncapped_input_reports_nothing(tmp_path):
    uncapped = "\n".join(
        line for line in CAPPED.splitlines() if "ACE" not in line
    ) + "\n"
    assert detect_input_terminal_caps(write(tmp_path, uncapped, "plain.pdb")) == []


def test_stripping_removes_the_cap_and_nothing_else(tmp_path):
    """The cap shares residue number 1 with the MET it caps, as in 1DFJ.

    Matching on chain and number alone deletes the first real residue too, so
    this is the case that matters.
    """
    out = tmp_path / "uncapped.pdb"
    result = strip_input_terminal_caps(write(tmp_path, CAPPED), out)
    assert [r["resname"] for r in result["removed"]] == ["ACE"]

    kept = [line[17:20].strip() for line in out.read_text().splitlines()
            if line.startswith("ATOM")]
    assert "ACE" not in kept
    assert kept.count("MET") == 2      # the residue the cap sat on survives
    assert {"MET", "ALA", "GLY"} <= set(kept)


def test_stripping_an_uncapped_structure_is_a_no_op(tmp_path):
    uncapped = "\n".join(
        line for line in CAPPED.splitlines() if "ACE" not in line
    ) + "\n"
    source = write(tmp_path, uncapped, "plain.pdb")
    result = strip_input_terminal_caps(source, tmp_path / "out.pdb")
    assert result["removed"] == []
    assert result["output_file"] == str(source)
