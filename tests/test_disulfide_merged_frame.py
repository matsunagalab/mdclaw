"""Declared disulfides follow the piece that carries them into the merged frame.

Campaign kimi-k3-3cond-full-v2, 004_membrane_5zkb cli_sif r2: the deposit's
CYS413-CYS416 bond, declared with ``--residue-ranges A:17-217 A:377-456``, was
looked up at A:413 after the merge had renamed the second piece to chain B,
and the prep was sealed with "has no SG atom". The mapping keyed by chain id
alone cannot tell the two pieces of one author chain apart.
"""

from mdclaw.structure.prepare_complex import _disulfide_pairs_in_merged_frame


def _piece(path, chain, resnums):
    lines = []
    serial = 1
    for resnum in resnums:
        for name in ("N", "CA", "CB", "SG"):
            lines.append(f"ATOM  {serial:5d}  {name:<3s} CYS {chain}{resnum:4d}    "
                         f"{1.0:8.3f}{2.0:8.3f}{3.0:8.3f}  1.00  0.00           {name[0]}")
            serial += 1
    path.write_text("\n".join(lines) + "\nEND\n")
    return str(path)


def _pair(a, b, chain="A"):
    return {"cys1": {"chain": chain, "resnum": a}, "cys2": {"chain": chain, "resnum": b}}


def test_two_pieces_of_one_chain_map_by_residue(tmp_path):
    first = _piece(tmp_path / "protein_1.clean.pdb", "A", [96, 176])
    second = _piece(tmp_path / "protein_2.clean.pdb", "A", [413, 416])
    entries = [
        {"source_file": first, "source_chain_id": "A", "md_chain_id": "A"},
        {"source_file": second, "source_chain_id": "A", "md_chain_id": "B"},
    ]
    pairs, unmapped = _disulfide_pairs_in_merged_frame([_pair(96, 176), _pair(413, 416)], entries)
    assert unmapped == []
    assert [(p["cys1"]["chain"], p["cys1"]["resnum"], p["cys2"]["chain"], p["cys2"]["resnum"]) for p in pairs] == [
        ("A", 96, "A", 176), ("B", 413, "B", 416)]


def test_a_residue_in_no_piece_is_unmapped_not_guessed(tmp_path):
    first = _piece(tmp_path / "protein_1.clean.pdb", "A", [96, 176])
    second = _piece(tmp_path / "protein_2.clean.pdb", "A", [413, 416])
    entries = [
        {"source_file": first, "source_chain_id": "A", "md_chain_id": "A"},
        {"source_file": second, "source_chain_id": "A", "md_chain_id": "B"},
    ]
    pairs, unmapped = _disulfide_pairs_in_merged_frame([_pair(300, 305)], entries)
    assert pairs == [] and unmapped == ["A:300-A:305"]


def test_a_chain_that_became_one_merged_chain_maps_by_name():
    entries = [{"source_chain_id": "A", "md_chain_id": "C"}, {"source_chain_id": "B", "md_chain_id": "D"}]
    pairs, unmapped = _disulfide_pairs_in_merged_frame([_pair(5, 9), _pair(5, 9, chain="B")], entries)
    assert unmapped == []
    assert [p["cys1"]["chain"] for p in pairs] == ["C", "D"]


def test_without_any_renaming_chains_are_kept():
    pairs, unmapped = _disulfide_pairs_in_merged_frame([_pair(5, 9)], [])
    assert unmapped == [] and pairs[0]["cys1"]["chain"] == "A"


def test_an_ambiguous_chain_without_files_is_not_guessed():
    entries = [{"source_chain_id": "A", "md_chain_id": "A"}, {"source_chain_id": "A", "md_chain_id": "B"}]
    pairs, unmapped = _disulfide_pairs_in_merged_frame([_pair(413, 416)], entries)
    assert pairs == [] and unmapped == ["A:413-A:416"]
