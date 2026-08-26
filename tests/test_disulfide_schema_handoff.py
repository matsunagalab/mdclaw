"""The schema prepare_complex writes must be the schema the repair reads.

These two ends disagreed once: the public argument is nested (`cys1`/`cys2`),
`prepare_complex` flattens it, and the resolver read only the nested form. Every
declared disulfide was dropped in silence -- `disulfide_patches: []` in the
generated config -- and 51 tests passed. A hand-written flat record in a test
would not have caught it either, so the conversion itself is what gets exercised
here.
"""
import importlib

import pytest

pc = importlib.import_module("mdclaw.structure.prepare_complex")
cp = importlib.import_module("mdclaw.structure.clean_protein")


def _nested(chain_a, num_a, chain_b, num_b, icode_a=None, icode_b=None):
    one = {"chain": chain_a, "resnum": num_a}
    two = {"chain": chain_b, "resnum": num_b}
    if icode_a is not None:
        one["icode"] = icode_a
    if icode_b is not None:
        two["icode"] = icode_b
    return {"cys1": one, "cys2": two}


class _Residue:
    def __init__(self, resnum, icode=""):
        self.id = str(resnum)
        self.insertionCode = icode


class _Chain:
    def __init__(self, chain_id, numbers):
        self.id = chain_id
        self._residues = [_Residue(*n) if isinstance(n, tuple) else _Residue(n)
                          for n in numbers]

    def residues(self):
        return list(self._residues)


def test_what_prepare_writes_is_what_the_resolver_reads():
    """The end-to-end link, through the real conversion rather than a fixture."""
    nested = [_nested("A", 59, "A", 102)]
    flat = pc.flatten_disulfide_pairs(nested)
    assert cp._disulfide_pair_sites(flat[0]) == cp._disulfide_pair_sites(nested[0])


def test_positions_come_out_the_same_from_either_shape():
    chains = [_Chain("A", list(range(26, 31)))]
    spans = [["X"] * 5]
    nested = [_nested("A", 26, "A", 30)]
    from_nested = cp._disulfide_patch_positions(chains, spans, {}, nested)
    from_flat = cp._disulfide_patch_positions(
        chains, spans, {}, pc.flatten_disulfide_pairs(nested))
    assert from_nested == from_flat
    assert from_nested["positions"] == [(0, 4)]


def test_the_flat_shape_carries_the_insertion_code():
    """Dropping it here would make the resolver's icode support unreachable."""
    flat = pc.flatten_disulfide_pairs([_nested("A", 52, "A", 53, icode_a="A")])
    assert flat[0]["icode1"] == "A"
    sites = cp._disulfide_pair_sites(flat[0])
    assert sites[0] == ("A", 52, "A")


def test_an_unstated_code_stays_unstated_through_the_conversion():
    """None and "" mean different things; collapsing them loses the distinction."""
    flat = pc.flatten_disulfide_pairs([_nested("A", 52, "A", 53)])
    assert flat[0]["icode1"] is None
    assert cp._disulfide_pair_sites(flat[0])[0] == ("A", 52, None)

    blank = pc.flatten_disulfide_pairs([_nested("A", 52, "A", 53, icode_a="")])
    assert blank[0]["icode1"] == ""
    assert cp._disulfide_pair_sites(blank[0])[0] == ("A", 52, "")


def test_form_bond_false_survives_the_conversion():
    pair = _nested("A", 59, "A", 102)
    pair["form_bond"] = False
    flat = pc.flatten_disulfide_pairs([pair])
    assert flat[0]["form_bond"] is False
    assert cp._disulfide_pair_sites(flat[0]) is None


def _gapped_pdb(path):
    """20 residues with 9-12 missing, CYS at 9 and 12, SEQRES for all 20."""
    seq = ["ALA"] * 8 + ["CYS", "ALA", "ALA", "CYS"] + ["ALA"] * 8
    lines = []
    for start in range(0, 20, 13):
        chunk = seq[start:start + 13]
        lines.append(f"SEQRES  {start // 13 + 1:>2} A   20  " + "  ".join(chunk))
    serial = 1
    for index, resname in enumerate(seq, start=1):
        if 9 <= index <= 12:
            continue
        atoms = [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O"), ("CB", "C")]
        if resname == "CYS":
            atoms.append(("SG", "S"))
        for name, element in atoms:
            lines.append(
                "ATOM  " + f"{serial:>5}" + " " + f"{name:<4}" + " " + resname
                + " " + "A" + f"{index:>4}" + " " + "   "
                + f"{3.4 * index + serial * 0.1:8.3f}{0.0:8.3f}{0.0:8.3f}"
                + "  1.00  0.00          " + f"{element:>2}")
            serial += 1
    path.write_text("\n".join(lines) + "\nTER\nEND\n")
    return path


@pytest.mark.parametrize("shape", ["nested", "flat"])
def test_a_declared_bond_reaches_modeller_as_a_patch(tmp_path, monkeypatch, shape):
    """Through the real repair, with only MODELLER's own runner stubbed.

    The assertion the schema defect failed: `disulfide_patches` must not arrive
    empty. Everything between the caller's pair and that argument -- the flat
    conversion, `_disulfide_pair_sites`, the alignment walk -- runs for real.
    """
    import mdclaw.genesis.modeller as genesis

    captured = {}

    def fake_modeller(**kwargs):
        captured.update(kwargs)
        return {"success": False, "errors": ["stubbed"], "warnings": [],
                "code": "stubbed"}

    monkeypatch.setattr(genesis, "modeller_from_alignment", fake_modeller)

    nested = [_nested("A", 9, "A", 12)]
    pairs = nested if shape == "nested" else pc.flatten_disulfide_pairs(nested)
    cp._repair_missing_residues_with_modeller(
        _gapped_pdb(tmp_path / "gapped.pdb"), disulfide_pairs=pairs)

    assert captured, "modeller_from_alignment was never reached"
    patches = captured.get("disulfide_patches")
    assert patches == [(8, 11)], f"a declared bond vanished: {patches}"


def test_the_repair_refuses_a_bond_it_cannot_place(tmp_path, monkeypatch):
    """A patch that cannot be placed is a covalent bond that would go missing."""
    import mdclaw.genesis.modeller as genesis

    called = []
    monkeypatch.setattr(genesis, "modeller_from_alignment",
                        lambda **kw: called.append(kw))

    outcome = cp._repair_missing_residues_with_modeller(
        _gapped_pdb(tmp_path / "gapped.pdb"),
        disulfide_pairs=[_nested("A", 9, "A", 900)])

    assert outcome["success"] is False
    assert outcome["code"] == "modeller_disulfide_position_unresolvable"
    assert not called, "MODELLER must not run once an endpoint is unplaceable"


def test_the_single_chain_path_forwards_the_pairs_too(tmp_path, monkeypatch):
    """`clean_protein`'s own MODELLER call, not just the complex pre-pass.

    Not only genuinely single-chain inputs land here: a complex whose pre-pass
    cannot probe or fuse falls back to this path chain by chain, and the declared
    bonds have to come with it.
    """
    import mdclaw.genesis.modeller as genesis

    captured = {}

    def fake_modeller(**kwargs):
        captured.update(kwargs)
        return {"success": False, "errors": ["stubbed"], "warnings": [],
                "code": "stubbed"}

    monkeypatch.setattr(genesis, "modeller_from_alignment", fake_modeller)

    source = _gapped_pdb(tmp_path / "gapped.pdb")
    cp.clean_protein(
        pdb_file=str(source),
        cap_termini=False,
        missing_residue_method="modeller",
        disulfide_pairs=pc.flatten_disulfide_pairs([_nested("A", 9, "A", 12)]),
        protonation_method="standard",
    )

    assert captured, "clean_protein never reached MODELLER"
    assert captured.get("disulfide_patches") == [(8, 11)], (
        f"the single-chain path dropped the declared bond: "
        f"{captured.get('disulfide_patches')}"
    )
