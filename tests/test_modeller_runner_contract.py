"""The generated MODELLER runner must keep the two contracts that were broken.

Both were invisible to the rest of the suite. `select_atoms()` decides which
atoms MODELLER may move; without it, filling a few loops re-optimises the whole
structure (measured on 9UT9: observed heavy atoms moved a median 0.28 A and
15.4 A next to a gap). And a disulfide patch index is positional, so passing it
as a string turns `residues[337]` into a lookup for a residue *named* 337.

Text checks, deliberately: they cost nothing and they fail the moment either
line is deleted. The real geometry is covered by the integration smoke test.
"""
import importlib

import pytest

gm = importlib.import_module("mdclaw.genesis.modeller")


@pytest.fixture(scope="module")
def runner_source(tmp_path_factory):
    path = tmp_path_factory.mktemp("runner") / "run_modeller.py"
    gm._write_modeller_runner(path)
    return path.read_text()


def test_the_base_model_is_restricted_too(runner_source):
    """Restricting only loop refinement leaves the whole-structure rebuild.

    By the time loops are refined, the base comparative model has already been
    optimised over every atom.
    """
    assert "def select_atoms(self):" in runner_source


def test_loop_refinement_is_restricted(runner_source):
    assert "def select_loop_atoms(self):" in runner_source


def test_both_hooks_return_the_same_selection(runner_source):
    """Two different ranges would leave part of the structure free to move."""
    body = runner_source.split("def select_atoms(self):", 1)[1]
    after_select = body.split("def select_loop_atoms(self):", 1)
    assert "self._mdclaw_gap_selection()" in after_select[0]
    assert "self._mdclaw_gap_selection()" in after_select[1]


def test_the_selection_is_the_gap_plus_two_residues(runner_source):
    """MODELLER's own anchor: enough to close the loop, few enough to preserve."""
    selection = runner_source.split("def _mdclaw_gap_selection(self):", 1)[1]
    assert "insertion_ext=2" in selection.split("def ", 1)[0]


def test_patch_indices_stay_integers_in_the_runner(runner_source):
    """`residues["337"]` is a residue identifier, not position 337."""
    assert "(int(a), int(b))" in runner_source
    assert "(str(a), str(b))" not in runner_source


def test_the_runner_declares_the_disulfides_and_restrains_them(runner_source):
    """The patch alone was not enough: 9UT9's CYS59-CYS102 opened to 3.53 A."""
    assert "def special_patches(self, aln):" in runner_source
    assert "def special_restraints(self, aln):" in runner_source
    assert 'residue_type="DISU"' in runner_source


def test_config_carries_patch_indices_as_json_numbers(tmp_path, monkeypatch):
    """A string here would survive JSON and reach the runner as an identifier."""
    import json

    class _Stopped(Exception):
        pass

    def fake_run(*args, **kwargs):
        raise _Stopped                       # the config is written by now

    monkeypatch.setattr(gm.subprocess, "run", fake_run)
    # The licence gate runs before the config is written; this test is about the
    # config's shape, not about having MODELLER.
    monkeypatch.setenv("KEY_MODELLER10v8", "test-only")
    alignment = tmp_path / "a.ali"
    alignment.write_text(">P1;t\nsequence:t:::::::: :\nAC*\n>P1;s\n"
                         "structureX:s:FIRST:A:LAST:A::::\nAC*\n")
    # A real two-residue template: the coordinate file is parsed before the
    # config is written.
    template = tmp_path / "s.pdb"
    rows = [(1, "ALA", "N", "N"), (1, "ALA", "CA", "C"), (1, "ALA", "C", "C"),
            (1, "ALA", "O", "O"), (1, "ALA", "CB", "C"),
            (2, "CYS", "N", "N"), (2, "CYS", "CA", "C"), (2, "CYS", "C", "C"),
            (2, "CYS", "O", "O"), (2, "CYS", "CB", "C"), (2, "CYS", "SG", "S")]
    template.write_text("\n".join(
        "ATOM  " + f"{i:>5}" + " " + f"{name:<4}" + " " + res
        + " " + "A" + f"{num:>4}" + " " + "   "
        + f"{1.5 * i:8.3f}{0.0:8.3f}{0.0:8.3f}" + "  1.00  0.00          "
        + f"{el:>2}"
        for i, (num, res, name, el) in enumerate(rows, start=1)
    ) + "\nTER\nEND\n")
    out_dir = tmp_path / "out"
    try:
        gm.modeller_from_alignment(
            template_pdb=str(template), alignment_file=str(alignment),
            template_code="s", target_code="t",
            disulfide_patches=[(8, 11)], output_dir=str(out_dir),
        )
    except _Stopped:
        pass

    config_path = next(out_dir.rglob("modeller_config.json"))
    patches = json.loads(config_path.read_text())["disulfide_patches"]
    assert patches == [[8, 11]]
    assert all(type(value) is int for pair in patches for value in pair)
