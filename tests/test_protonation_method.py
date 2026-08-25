"""Which protonation path actually ran, asserted on the provenance.

The bug these exist for: the titration flags were spliced into the pdb2pqr
argument list by index and landed between ``--ffout`` and its value. pdb2pqr
then failed, preparation fell back to ``pdb4amber+reduce`` - which ignores pH
entirely - and said nothing, because prepare_complex drops that warning. The
whole 1659-test suite stayed green. Asserting that a wrapper was called would
not have caught it; only the recorded method does.
"""

import pytest
import importlib

from mdclaw.structure.clean_protein import (
    PROTONATION_METHODS,
    _protonation_method_label,
)


def test_the_recorded_label_distinguishes_the_two_paths():
    assert _protonation_method_label(False) == "pdb2pqr+propka"
    assert _protonation_method_label(True) == "pdb2pqr_standard_state"
    assert set(PROTONATION_METHODS) == {"propka", "standard"}


def test_an_unknown_method_is_refused(tmp_path):
    from mdclaw.structure.clean_protein import clean_protein

    structure = tmp_path / "x.pdb"
    structure.write_text("END\n")
    with pytest.raises(ValueError, match="Unsupported protonation_method"):
        clean_protein(str(structure), protonation_method="propka-ish")


def test_requested_method_fails_closed_when_pdb2pqr_is_unavailable(
    tmp_path, monkeypatch,
):
    clean_module = importlib.import_module("mdclaw.structure.clean_protein")
    structure = tmp_path / "x.pdb"
    structure.write_text(
        "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  CA  ALA A   1       1.450   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  C   ALA A   1       2.000   1.400   0.000  1.00  0.00           C\n"
        "ATOM      4  O   ALA A   1       1.300   2.400   0.000  1.00  0.00           O\n"
        "END\n")
    monkeypatch.setattr(clean_module.pdb2pqr_wrapper, "is_available", lambda: False)

    result = clean_module.clean_protein(str(structure), add_missing_atoms=False)

    assert result["success"] is False
    assert result["code"] == "protonation_method_unavailable"
    assert "pdb4amber+reduce" in result["errors"][0]


def test_requested_method_fails_closed_when_pdb2pqr_errors(tmp_path, monkeypatch):
    clean_module = importlib.import_module("mdclaw.structure.clean_protein")
    structure = tmp_path / "x.pdb"
    structure.write_text(
        "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  CA  ALA A   1       1.450   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  C   ALA A   1       2.000   1.400   0.000  1.00  0.00           C\n"
        "ATOM      4  O   ALA A   1       1.300   2.400   0.000  1.00  0.00           O\n"
        "END\n")
    monkeypatch.setattr(clean_module.pdb2pqr_wrapper, "is_available", lambda: True)
    monkeypatch.setattr(
        clean_module.pdb2pqr_wrapper, "run",
        lambda args: (_ for _ in ()).throw(RuntimeError("synthetic pdb2pqr failure")),
    )

    result = clean_module.clean_protein(str(structure), add_missing_atoms=False)

    assert result["success"] is False
    assert result["code"] == "protonation_method_failed"
    assert "synthetic pdb2pqr failure" in result["errors"][0]


def test_requested_method_cannot_succeed_from_stale_pdb2pqr_output(
    tmp_path, monkeypatch,
):
    clean_module = importlib.import_module("mdclaw.structure.clean_protein")
    structure = tmp_path / "x.pdb"
    structure.write_text(
        "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  CA  ALA A   1       1.450   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  C   ALA A   1       2.000   1.400   0.000  1.00  0.00           C\n"
        "ATOM      4  O   ALA A   1       1.300   2.400   0.000  1.00  0.00           O\n"
        "END\n")
    stale = tmp_path / "x.amber.pdb"
    stale.write_text("REMARK stale output from a previous invocation\nEND\n")
    monkeypatch.setattr(clean_module.pdb2pqr_wrapper, "is_available", lambda: True)
    monkeypatch.setattr(clean_module.pdb2pqr_wrapper, "run", lambda args: None)

    result = clean_module.clean_protein(str(structure), add_missing_atoms=False)

    assert result["success"] is False
    assert result["code"] == "protonation_method_failed"
    assert "did not create output" in result["errors"][0]
    assert not stale.exists()


@pytest.mark.parametrize("method,expected_flag", [
    ("propka", True),
    ("standard", False),
])
def test_titration_flags_are_paired_with_their_values(method, expected_flag):
    """`--ffout` must keep its value whichever mode is selected.

    Rebuilds the argument list the same way the code does and checks that every
    option that takes a value is followed by one, which is what the index
    splice broke.
    """
    titration = (["--titration-state-method", "propka", "--with-ph", "7.0"]
                 if method == "propka" else [])
    args = ["in.pdb", "out.pqr", "--ff", "AMBER", "--ffout", "AMBER",
            *titration, "--pdb-output", "amber.pdb", "--keep-chain",
            "--drop-water"]

    assert ("--titration-state-method" in args) is expected_flag
    for option in ("--ff", "--ffout", "--pdb-output"):
        value = args[args.index(option) + 1]
        assert not value.startswith("--"), f"{option} lost its value: {value!r}"
    if expected_flag:
        assert args[args.index("--titration-state-method") + 1] == "propka"
        assert args[args.index("--with-ph") + 1] == "7.0"
