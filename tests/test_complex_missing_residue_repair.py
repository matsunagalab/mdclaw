"""Complex-wide MODELLER repair: multi-chain alignment rows and file plumbing.

The single-chain repair was already correct chain by chain; what it could not
see was the other chain. These cover the pieces that make one MODELLER pass over
the whole complex possible.
"""
import importlib
from pathlib import Path

import pytest

from mdclaw.structure.clean_protein import (
    _combine_chains_for_repair,
    _split_repaired_complex,
    _template_alignment_row,
    _write_repair_alignment,
)

# mdclaw.structure re-exports the clean_protein *function*, which shadows the
# module of the same name; reach the module explicitly so monkeypatch can see it.
cp = importlib.import_module("mdclaw.structure.clean_protein")


class _Res:
    pass


class _Chain:
    def __init__(self, chain_id, n_residues):
        self.id = chain_id
        self._residues = [_Res() for _ in range(n_residues)]

    def residues(self):
        return list(self._residues)


def _atom(chain, resnum, name="CA", resname="ALA", serial=1):
    return (
        f"ATOM  {serial:>5} {name:<4}{resname:>4}{chain:>2}{resnum:>4}"
        f"    {0.0:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           C"
    )


# --- template row ----------------------------------------------------------

def test_single_chain_row_has_no_separator():
    chains = [_Chain("A", 4)]
    row = _template_alignment_row(["ABCDE"], {(0, 2): ["GLY"]}, chains)
    assert row == "AB-DE"
    assert "/" not in row


def test_two_chains_are_joined_with_a_chain_break():
    chains = [_Chain("A", 3), _Chain("B", 3)]
    row = _template_alignment_row(["ABCD", "EFGH"], {(0, 1): ["X"], (1, 2): ["Y"]}, chains)
    assert row == "A-CD/EF-H"


def test_gap_indices_are_chain_local_not_global():
    """A gap at index 1 of chain B must land in chain B's row, not chain A's."""
    chains = [_Chain("A", 3), _Chain("B", 3)]
    row = _template_alignment_row(["ABC", "DEFG"], {(1, 1): ["X"]}, chains)
    first, second = row.split("/")
    assert first == "ABC"          # untouched
    # D, F, G are the observed residues; E is the one in the gap, so it is the
    # letter the template row drops.
    assert second == "D-FG"


def test_row_and_target_rows_have_equal_length():
    chains = [_Chain("A", 3), _Chain("B", 4)]
    targets = ["ABCDE", "FGHIJ"]
    row = _template_alignment_row(targets, {(0, 0): ["X", "Y"], (1, 4): ["Z"]}, chains)
    assert len(row) == len("/".join(targets))


def test_terminal_gap_at_chain_end_is_emitted():
    chains = [_Chain("A", 2)]
    row = _template_alignment_row(["ABC"], {(0, 2): ["X"]}, chains)
    assert row == "AB-"


# --- alignment file --------------------------------------------------------

def test_structure_line_names_first_and_last_chain(tmp_path):
    path = tmp_path / "aln.ali"
    _write_repair_alignment(
        path,
        target_code="t",
        target_sequence="AB/CD",
        template_code="s",
        template_row="A-/CD",
        first_chain="A",
        last_chain="B",
    )
    text = path.read_text()
    assert "structureX:s:FIRST:A:LAST:B:" in text
    assert "FIRST:@" not in text


def test_structure_line_defaults_to_wildcard_for_one_chain(tmp_path):
    path = tmp_path / "aln.ali"
    _write_repair_alignment(
        path,
        target_code="t",
        target_sequence="ABC",
        template_code="s",
        template_row="A-C",
    )
    assert "structureX:s:FIRST:@:LAST:@:" in path.read_text()


def test_mismatched_row_lengths_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="differ in length"):
        _write_repair_alignment(
            tmp_path / "aln.ali",
            target_code="t",
            target_sequence="ABCD",
            template_code="s",
            template_row="A-C",
        )


# --- combine / split -------------------------------------------------------

def _write_chain(tmp_path, name, chain, resnums):
    path = tmp_path / name
    lines = [f"SEQRES   1 {chain} {len(resnums):>4}  ALA"]
    lines += [_atom(chain, n, serial=i + 1) for i, n in enumerate(resnums)]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_combine_keeps_every_seqres_and_marks_chain_breaks(tmp_path):
    a = _write_chain(tmp_path, "p1.pdb", "A", [1, 2])
    b = _write_chain(tmp_path, "p2.pdb", "B", [1, 2, 3])
    out = tmp_path / "fused.pdb"
    res = _combine_chains_for_repair([a, b], out)
    assert res["success"], res["errors"]
    assert res["chain_ids"] == ["A", "B"]
    text = out.read_text()
    assert text.count("SEQRES") == 2
    assert text.count("\nTER") == 2
    assert len([ln for ln in text.splitlines() if ln.startswith("ATOM")]) == 5


def test_combine_refuses_duplicate_chain_ids(tmp_path):
    a = _write_chain(tmp_path, "p1.pdb", "A", [1, 2])
    b = _write_chain(tmp_path, "p2.pdb", "A", [3, 4])
    res = _combine_chains_for_repair([a, b], tmp_path / "fused.pdb")
    assert not res["success"]
    assert any("more than one input file" in e for e in res["errors"])


def test_combine_refuses_a_file_holding_two_chains(tmp_path):
    path = tmp_path / "p1.pdb"
    path.write_text("\n".join([_atom("A", 1, serial=1), _atom("B", 1, serial=2)]) + "\n")
    res = _combine_chains_for_repair([path], tmp_path / "fused.pdb")
    assert not res["success"]
    assert any("expected 1" in e for e in res["errors"])


def test_split_returns_one_file_per_chain_keeping_source_seqres(tmp_path):
    a = _write_chain(tmp_path, "p1.pdb", "A", [1, 2])
    b = _write_chain(tmp_path, "p2.pdb", "B", [1, 2, 3])
    model = tmp_path / "model.pdb"
    model.write_text("\n".join(
        [_atom("A", n, serial=i + 1) for i, n in enumerate([1, 2, 3])]
        + [_atom("B", n, serial=i + 10) for i, n in enumerate([1, 2, 3, 4])]
    ) + "\n")
    res = _split_repaired_complex(model, {"A": str(a), "B": str(b)})
    assert res["success"], res["errors"]
    text_a = Path(res["outputs"]["A"]).read_text()
    text_b = Path(res["outputs"]["B"]).read_text()
    assert text_a.count("SEQRES") == 1 and "SEQRES   1 A" in text_a
    assert text_b.count("SEQRES") == 1 and "SEQRES   1 B" in text_b
    assert len([ln for ln in text_a.splitlines() if ln.startswith("ATOM")]) == 3
    assert len([ln for ln in text_b.splitlines() if ln.startswith("ATOM")]) == 4
    assert all(ln[21] == "A" for ln in text_a.splitlines() if ln.startswith("ATOM"))


def test_split_fails_when_a_chain_vanished_from_the_model(tmp_path):
    a = _write_chain(tmp_path, "p1.pdb", "A", [1, 2])
    b = _write_chain(tmp_path, "p2.pdb", "B", [1, 2])
    model = tmp_path / "model.pdb"
    model.write_text(_atom("A", 1, serial=1) + "\n")
    res = _split_repaired_complex(model, {"A": str(a), "B": str(b)})
    assert not res["success"]
    assert any("no atoms for chain 'B'" in e for e in res["errors"])


# --- entry point: when the complex pass runs, and when it steps aside --------

def test_single_protein_file_is_left_to_the_per_chain_path(tmp_path):
    repair_complex_missing_residues = cp.repair_complex_missing_residues
    a = _write_chain(tmp_path, "p1.pdb", "A", [1, 2])
    outcome = repair_complex_missing_residues([a])
    assert outcome["applied"] is False
    assert outcome["success"] is True


def test_complex_pass_is_skipped_when_no_chain_needs_modeller(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cp, "_resolve_missing_residue_method",
        lambda method, path: {"method": "pdbfixer", "escalated": False,
                              "out_of_scope": False, "summary": None, "usability": None},
    )
    called = []
    monkeypatch.setattr(
        cp, "_repair_missing_residues_with_modeller",
        lambda *a, **k: called.append(1),
    )
    outcome = cp.repair_complex_missing_residues(
        [_write_chain(tmp_path, "p1.pdb", "A", [1]), _write_chain(tmp_path, "p2.pdb", "B", [1])]
    )
    assert outcome["applied"] is False
    assert not called


def test_one_chain_needing_modeller_pulls_the_whole_complex_in(tmp_path, monkeypatch):
    """The point of the pass: chain B's gap is repaired with chain A present."""
    a = _write_chain(tmp_path, "p1.pdb", "A", [1, 2])
    b = _write_chain(tmp_path, "p2.pdb", "B", [1, 2])
    model = tmp_path / "model.pdb"
    model.write_text("\n".join(
        [_atom("A", n, serial=i + 1) for i, n in enumerate([1, 2])]
        + [_atom("B", n, serial=i + 5) for i, n in enumerate([1, 2, 3])]
    ) + "\n")

    decisions = iter([
        {"method": "pdbfixer", "escalated": False, "out_of_scope": False,
         "summary": None, "usability": None},
        {"method": "modeller", "escalated": True, "out_of_scope": True,
         "summary": {"total_residues": 1}, "usability": {"usable": True}},
    ])
    monkeypatch.setattr(cp, "_resolve_missing_residue_method",
                        lambda method, path: next(decisions))
    seen = {}

    def fake_repair(path, *args, **kwargs):
        seen["input"] = Path(path)
        return {"applied": True, "success": True, "model_file": str(model),
                "summary": {"total_residues": 1}, "operation": {}, "validation": {},
                "errors": [], "warnings": [], "code": None}

    monkeypatch.setattr(cp, "_repair_missing_residues_with_modeller", fake_repair)
    outcome = cp.repair_complex_missing_residues([a, b], work_dir=tmp_path)

    assert outcome["applied"] is True
    # MODELLER saw one fused file holding both chains, not chain B alone.
    fused = seen["input"].read_text()
    assert {line[21] for line in fused.splitlines() if line.startswith("ATOM")} == {"A", "B"}
    assert set(outcome["outputs_by_source"]) == {str(a), str(b)}


def test_unreadable_chain_defers_instead_of_aborting(tmp_path, monkeypatch):
    """A structure the probe cannot read must not take the whole run down."""
    def boom(method, path):
        raise ValueError("could not convert string to float")

    monkeypatch.setattr(cp, "_resolve_missing_residue_method", boom)
    outcome = cp.repair_complex_missing_residues(
        [_write_chain(tmp_path, "p1.pdb", "A", [1]), _write_chain(tmp_path, "p2.pdb", "B", [1])]
    )
    assert outcome["applied"] is False
    assert outcome["success"] is True
    assert any("falling back to per-chain repair" in w for w in outcome["warnings"])


def test_duplicate_chain_ids_defer_with_a_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cp, "_resolve_missing_residue_method",
        lambda method, path: {"method": "modeller", "escalated": True,
                              "out_of_scope": True, "summary": None,
                              "usability": {"usable": True}},
    )
    outcome = cp.repair_complex_missing_residues(
        [_write_chain(tmp_path, "p1.pdb", "A", [1]), _write_chain(tmp_path, "p2.pdb", "A", [2])],
        work_dir=tmp_path,
    )
    assert outcome["applied"] is False
    assert outcome["success"] is True
    assert any("falling back to per-chain repair" in w for w in outcome["warnings"])


def test_modeller_failure_is_a_hard_error_not_a_fallback(tmp_path, monkeypatch):
    """A repair that ran and failed must not quietly become a per-chain repair."""
    monkeypatch.setattr(
        cp, "_resolve_missing_residue_method",
        lambda method, path: {"method": "modeller", "escalated": True,
                              "out_of_scope": True, "summary": None,
                              "usability": {"usable": True}},
    )
    monkeypatch.setattr(
        cp, "_repair_missing_residues_with_modeller",
        lambda *a, **k: {"applied": False, "success": False, "model_file": None,
                         "summary": None, "errors": ["modeller blew up"],
                         "warnings": [], "code": "modeller_execution_failed"},
    )
    outcome = cp.repair_complex_missing_residues(
        [_write_chain(tmp_path, "p1.pdb", "A", [1]), _write_chain(tmp_path, "p2.pdb", "B", [1])],
        work_dir=tmp_path,
    )
    assert outcome["success"] is False
    assert outcome["code"] == "modeller_execution_failed"


# --- how the rebuild is reported --------------------------------------------

def _chain_count_from_records(modelled):
    """The counting rule prepare_complex uses for the 'across N chain(s)' warning."""
    repaired = set()
    for repair in modelled:
        ids = repair.get("chain_ids")
        if ids:
            repaired.update(str(i) for i in ids)
        elif repair.get("chain_id") is not None:
            repaired.add(str(repair["chain_id"]))
        else:
            repaired.add(f"_record_{id(repair)}")
    return len(repaired)


def test_complex_record_counts_its_chains_not_itself():
    """One record covering two chains is two chains, not one.

    The complex pass reports a single repair, and counting records said
    "across 1 chain(s)" for a rebuild that spanned both.
    """
    assert _chain_count_from_records([
        {"method": "modeller", "chain_ids": ["A", "B"], "total_residues": 58},
    ]) == 2


def test_per_chain_records_still_count_one_each():
    assert _chain_count_from_records([
        {"method": "modeller", "chain_id": "A", "total_residues": 38},
        {"method": "modeller", "chain_id": "B", "total_residues": 20},
    ]) == 2


def test_records_with_no_chain_identity_are_not_collapsed():
    """Two anonymous records must not merge into one chain."""
    assert _chain_count_from_records([
        {"method": "modeller", "total_residues": 3},
        {"method": "modeller", "total_residues": 4},
    ]) == 2


# --- disulfides across a rebuilt gap ----------------------------------------

def _ss(chain_a, num_a, chain_b, num_b, icode_a=None, icode_b=None):
    """A pair. An omitted icode means "not stated", which is not the same as ""."""
    one = {"chain": chain_a, "resnum": num_a}
    two = {"chain": chain_b, "resnum": num_b}
    if icode_a is not None:
        one["icode"] = icode_a
    if icode_b is not None:
        two["icode"] = icode_b
    return {"cys1": one, "cys2": two}


def _flat(chain_a, num_a, chain_b, num_b):
    """The shape prepare_complex flattens pairs into before passing them on."""
    return {"chain1": chain_a, "resnum1": num_a, "icode1": "",
            "chain2": chain_b, "resnum2": num_b, "icode2": "", "form_bond": True}


class _Residue:
    def __init__(self, resnum, icode=""):
        self.id = str(resnum)
        self.insertionCode = icode


class _ObservedChain:
    """A topology chain carrying the deposit's own residue numbering."""

    def __init__(self, chain_id, numbers):
        self.id = chain_id
        self._residues = [_Residue(*n) if isinstance(n, tuple) else _Residue(n)
                          for n in numbers]

    def residues(self):
        return list(self._residues)


def _positions(chains, spans, internal, pairs):
    return cp._disulfide_patch_positions(chains, spans, internal, pairs)


def test_positions_are_model_indices_not_author_numbers():
    """While modelling, residues are numbered over the target sequence.

    Chain A runs 26..40 observed with a 3-residue gap after 30, so author 35 is
    the 10th residue of the model: index 9.
    """
    chains = [_ObservedChain("A", list(range(26, 31)) + list(range(34, 41)))]
    spans = [["X"] * 15]                       # 26..40 once the gap is filled
    internal = {(0, 5): ["GLY", "GLY", "GLY"]}  # 31, 32, 33
    got = _positions(chains, spans, internal, [_ss("A", 26, "A", 35)])
    assert got["errors"] == []
    assert got["positions"] == [(0, 9)]


def test_positions_offset_by_earlier_chains():
    chains = [_ObservedChain("A", list(range(26, 31))),
              _ObservedChain("B", list(range(23, 28)))]
    spans = [["X"] * 5, ["X"] * 5]
    got = _positions(chains, spans, {}, [_ss("B", 24, "B", 27)])
    assert got["errors"] == []
    assert got["positions"] == [(6, 9)]        # chain B starts at 5


def test_a_bond_inside_a_gap_resolves_from_its_anchors():
    """The case the whole thing exists for: 9UT9's A363-A366.

    Both are inside the 342-366 gap, so the template cannot show them. The
    flanking anchors number the run uniquely, so they resolve anyway.
    """
    chains = [_ObservedChain("A", [340, 341, 367, 368])]
    spans = [["X"] * 29]                       # 340..368
    internal = {(0, 2): ["X"] * 25}            # 342..366
    got = _positions(chains, spans, internal, [_ss("A", 363, "A", 366)])
    assert got["errors"] == []
    assert got["positions"] == [(23, 26)]


def test_a_gap_whose_numbering_does_not_match_its_length_is_refused():
    """Guessing here would patch the wrong cysteine -- a covalent bond."""
    chains = [_ObservedChain("A", [340, 341, 367, 368])]
    spans = [["X"] * 24]
    internal = {(0, 2): ["X"] * 20}            # 20 residues for a 25-wide gap
    got = _positions(chains, spans, internal, [_ss("A", 363, "A", 366)])
    assert got["positions"] == []
    assert any("not determined by the flanking" in e for e in got["errors"])


def test_a_jump_in_observed_numbering_still_resolves_by_alignment_order():
    """Observed residues are exact keys, so a numbering jump is not a problem."""
    chains = [_ObservedChain("A", [10, 11, 90, 91])]
    spans = [["X"] * 4]
    got = _positions(chains, spans, {}, [_ss("A", 11, "A", 90)])
    assert got["errors"] == []
    assert got["positions"] == [(1, 2)]


def test_an_insertion_code_resolves_only_when_named():
    chains = [_ObservedChain("A", [(52, ""), (52, "A"), (53, "")])]
    spans = [["X"] * 3]
    named = _positions(chains, spans, {}, [_ss("A", 52, "A", 53, icode_a="A")])
    assert named["errors"] == [] and named["positions"] == [(1, 2)]

    # Not stated at all, where two residues answer to 52: ambiguous.
    bare = _positions(chains, spans, {}, [_ss("A", 52, "A", 53)])
    assert bare["positions"] == []
    assert any("more than one residue" in e for e in bare["errors"])


def test_an_explicit_blank_insertion_code_is_not_ambiguous():
    """"" names the residue without a code; omitting it names nothing."""
    chains = [_ObservedChain("A", [(52, ""), (52, "A"), (53, "")])]
    got = _positions(chains, [["X"] * 3], {},
                     [_ss("A", 52, "A", 53, icode_a="", icode_b="")])
    assert got["errors"] == [] and got["positions"] == [(0, 2)]


def test_a_missing_chain_is_refused_not_read_as_chain_N():
    """str(None)[:1] is "N", and chain N exists in plenty of structures."""
    chains = [_ObservedChain("N", [1, 2, 3])]
    got = _positions(chains, [["X"] * 3], {},
                     [{"cys1": {"chain": None, "resnum": 1},
                       "cys2": {"chain": "N", "resnum": 3}}])
    assert got["positions"] == []


def test_a_residue_outside_the_span_is_refused_not_skipped():
    chains = [_ObservedChain("A", list(range(26, 31)))]
    spans = [["X"] * 5]
    got = _positions(chains, spans, {}, [_ss("A", 26, "A", 900)])
    assert got["positions"] == []
    assert any("not a residue of the modelled span" in e for e in got["errors"])


def test_a_bond_to_a_chain_this_run_does_not_hold_is_deferred():
    """Per-chain runs legitimately see only part of a complex's bonds."""
    chains = [_ObservedChain("A", list(range(26, 31)))]
    spans = [["X"] * 5]
    got = _positions(chains, spans, {}, [_ss("A", 26, "B", 30)])
    assert got["positions"] == [] and got["errors"] == []


def test_both_disulfide_pair_shapes_are_understood():
    """Reading only cys1/cys2 matched nothing when the flat records arrived."""
    chains = [_ObservedChain("A", list(range(26, 31)))]
    spans = [["X"] * 5]
    nested = _positions(chains, spans, {}, [_ss("A", 26, "A", 30)])
    flat = _positions(chains, spans, {}, [_flat("A", 26, "A", 30)])
    assert nested == flat
    assert nested["positions"] == [(0, 4)]


def test_a_pair_marked_not_to_form_is_not_patched():
    chains = [_ObservedChain("A", list(range(26, 31)))]
    pair = _flat("A", 26, "A", 30)
    pair["form_bond"] = False
    assert _positions(chains, [["X"] * 5], {}, [pair])["positions"] == []


def test_no_pairs_means_no_patches():
    chains = [_ObservedChain("A", list(range(26, 31)))]
    assert _positions(chains, [["X"] * 5], {}, None)["positions"] == []
    assert _positions(chains, [["X"] * 5], {}, [])["positions"] == []


def test_a_span_the_walk_cannot_fill_is_an_error():
    chains = [_ObservedChain("A", list(range(26, 31)))]
    got = _positions(chains, [["X"] * 9], {}, [_ss("A", 26, "A", 30)])
    assert got["positions"] == []
    assert any("do not fill the" in e for e in got["errors"])


# --- the declared bonds actually formed --------------------------------------

def _sg_pdb(path, coords):
    """A PDB holding only SG atoms, at the coordinates given per residue key."""
    lines = []
    for i, ((chain, resnum, icode), (x, y, z)) in enumerate(coords.items(), start=1):
        lines.append(
            "ATOM  " + f"{i:>5}" + " " + f"{'SG':<4}" + " " + "CYX"
            + " " + chain + f"{resnum:>4}" + (icode or " ") + "   "
            + f"{x:8.3f}{y:8.3f}{z:8.3f}" + "  1.00  0.00           S"
        )
    path.write_text("\n".join(lines) + "\nEND\n")
    return path


def test_a_bond_at_bonding_distance_passes(tmp_path):
    path = _sg_pdb(tmp_path / "m.pdb", {("A", 59, ""): (0, 0, 0), ("A", 102, ""): (2.04, 0, 0)})
    got = cp._validate_declared_disulfides(path, [_ss("A", 59, "A", 102)], {"A"})
    assert got["success"] and got["checked"] == 1
    assert got["distances"][0]["sg_sg_angstrom"] == 2.04


def test_a_bond_pulled_open_by_refinement_fails(tmp_path):
    """The measured 9UT9 regression: declared, patched, and still 3.53 A."""
    path = _sg_pdb(tmp_path / "m.pdb", {("A", 59, ""): (0, 0, 0), ("A", 102, ""): (3.53, 0, 0)})
    got = cp._validate_declared_disulfides(path, [_ss("A", 59, "A", 102)], {"A"})
    assert not got["success"]
    assert any("3.53" in e for e in got["errors"])


def test_a_bond_never_built_fails(tmp_path):
    """A363-A366 as it came out without its restraint."""
    path = _sg_pdb(tmp_path / "m.pdb", {("A", 363, ""): (0, 0, 0), ("A", 366, ""): (11.65, 0, 0)})
    got = cp._validate_declared_disulfides(path, [_ss("A", 363, "A", 366)], {"A"})
    assert not got["success"]


def test_the_window_edges(tmp_path):
    for distance, ok in ((2.30, True), (2.31, False), (1.80, True), (1.79, False)):
        path = _sg_pdb(tmp_path / f"m{distance}.pdb",
                       {("A", 1, ""): (0, 0, 0), ("A", 2, ""): (distance, 0, 0)})
        got = cp._validate_declared_disulfides(path, [_ss("A", 1, "A", 2)], {"A"})
        assert got["success"] is ok, f"{distance} A"


def test_a_missing_sg_fails(tmp_path):
    path = _sg_pdb(tmp_path / "m.pdb", {("A", 59, ""): (0, 0, 0)})
    got = cp._validate_declared_disulfides(path, [_ss("A", 59, "A", 102)], {"A"})
    assert not got["success"]
    assert any("no SG atom" in e for e in got["errors"])


def test_a_pair_far_from_any_gap_is_still_checked(tmp_path):
    """Not limited to gap neighbourhoods: that is how A59-A102 went unnoticed."""
    path = _sg_pdb(tmp_path / "m.pdb", {("A", 495, ""): (0, 0, 0), ("A", 514, ""): (5.0, 0, 0)})
    got = cp._validate_declared_disulfides(path, [_ss("A", 495, "A", 514)], {"A"})
    assert got["checked"] == 1 and not got["success"]


def test_a_pair_from_another_chain_is_not_counted(tmp_path):
    path = _sg_pdb(tmp_path / "m.pdb", {("A", 59, ""): (0, 0, 0), ("A", 102, ""): (2.04, 0, 0)})
    got = cp._validate_declared_disulfides(
        path, [_ss("A", 59, "A", 102), _ss("B", 1, "B", 2)], {"A"})
    assert got["success"] and got["checked"] == 1
