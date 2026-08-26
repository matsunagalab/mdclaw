"""A disulfide endpoint has to resolve to one cysteine, insertion code included.

MODELLER is told which residues to patch by position, and the naming and topology
steps downstream find them by number. If those disagree -- and a bare
(chain, resnum) does disagree as soon as a deposit uses insertion codes -- the
bond is restrained on one cysteine and labelled CYX on another.

Artifacts written before insertion codes were carried have no icode field, so an
unstated code has to keep resolving wherever only one residue answers to the
number; only a genuinely ambiguous number fails.
"""
import importlib

pu = importlib.import_module("mdclaw.structure.pdb_utils")
ds = importlib.import_module("mdclaw.structure.disulfide")

CANDIDATES = {("A", 52, ""): 1, ("A", 52, "A"): 2, ("A", 53, ""): 3}
UNIQUE = {("A", 52, ""): 1, ("A", 53, ""): 2}


def test_a_named_blank_code_picks_the_uncoded_residue():
    site, error = pu.resolve_residue_site(CANDIDATES, "A", 52, "")
    assert error is None and site == ("A", 52, "")


def test_a_named_code_picks_that_residue():
    site, error = pu.resolve_residue_site(CANDIDATES, "A", 52, "A")
    assert error is None and site == ("A", 52, "A")


def test_an_unstated_code_is_ambiguous_when_several_share_the_number():
    site, error = pu.resolve_residue_site(CANDIDATES, "A", 52, None)
    assert site is None
    assert "name the insertion code" in error


def test_an_unstated_code_still_resolves_when_only_one_matches():
    """Every artifact written before icodes were carried looks like this."""
    site, error = pu.resolve_residue_site(UNIQUE, "A", 52, None)
    assert error is None and site == ("A", 52, "")


def test_a_residue_that_is_not_there_is_an_error_not_a_guess():
    site, error = pu.resolve_residue_site(UNIQUE, "A", 99, None)
    assert site is None and "not present" in error


# --- the pair identity used to reject one sulfur holding two bonds ----------

def _pair(num_a, icode_a, num_b, icode_b):
    one = {"chain": "A", "resnum": num_a}
    two = {"chain": "A", "resnum": num_b}
    if icode_a is not None:
        one["icode"] = icode_a
    if icode_b is not None:
        two["icode"] = icode_b
    return {"cys1": one, "cys2": two}


def test_insertion_coded_neighbours_are_different_ends():
    """A270 and A270A are two cysteines, so these are two distinct bonds."""
    assert ds._pair_key(_pair(270, "", 300, "")) != ds._pair_key(_pair(270, "A", 300, ""))


def test_the_same_pair_is_the_same_key_either_way_round():
    assert ds._pair_key(_pair(270, "", 300, "")) == ds._pair_key(_pair(300, "", 270, ""))


# --- CYX reconciliation ------------------------------------------------------

def _two_cys_pdb(path):
    """Chain A with CYS 52, CYS 52A and CYS 53, all written as CYS."""
    rows = [(52, " "), (52, "A"), (53, " ")]
    lines = []
    serial = 1
    for resnum, icode in rows:
        for name, element in (("N", "N"), ("CA", "C"), ("CB", "C"), ("SG", "S"),
                              ("C", "C"), ("O", "O")):
            lines.append(
                "ATOM  " + f"{serial:>5}" + " " + f"{name:<4}" + " " + "CYS"
                + " " + "A" + f"{resnum:>4}" + icode + "   "
                + f"{serial * 1.5:8.3f}{0.0:8.3f}{0.0:8.3f}"
                + "  1.00  0.00          " + f"{element:>2}"
            )
            serial += 1
    path.write_text("\n".join(lines) + "\nEND\n")
    return path


def _resname_at(path, resnum, icode):
    for line in path.read_text().splitlines():
        if line.startswith("ATOM") and int(line[22:26]) == resnum and line[26] == icode:
            return line[17:20].strip()
    return None


def test_a_named_code_renames_only_that_cysteine(tmp_path):
    path = _two_cys_pdb(tmp_path / "c.pdb")
    ds._reconcile_cyx_cys_in_pdb(str(path), [_pair(52, "A", 53, "")])
    assert _resname_at(path, 52, "A") == "CYX"
    assert _resname_at(path, 52, " ") == "CYS"      # the neighbour is untouched


def test_a_named_blank_renames_the_uncoded_one(tmp_path):
    path = _two_cys_pdb(tmp_path / "c.pdb")
    ds._reconcile_cyx_cys_in_pdb(str(path), [_pair(52, "", 53, "")])
    assert _resname_at(path, 52, " ") == "CYX"
    assert _resname_at(path, 52, "A") == "CYS"


def test_an_ambiguous_number_renames_neither(tmp_path):
    """Refusing beats renaming one of two at random."""
    path = _two_cys_pdb(tmp_path / "c.pdb")
    ds._reconcile_cyx_cys_in_pdb(str(path), [_pair(52, None, 53, None)])
    assert _resname_at(path, 52, " ") == "CYS"
    assert _resname_at(path, 52, "A") == "CYS"
