"""The residue numbering an agent needs is in the default output.

015_antibody_1ahw r1 of campaign v4 wanted each chain's first and last
residue and its gaps; ``chains`` is cut from the default output of a
six-chain entry, so it wrote its own parser, which looped for ever. 036,
028 and 024 delivered a chain one residue off the task's range with nothing
in the prep result saying so.
"""
from pathlib import Path

import pytest

from mdclaw._envelope import brief_result, next_step
from mdclaw._node import create_node, read_node
from mdclaw.research.inspection import inspect_molecules
from mdclaw.structure.prepare_complex import _range_delivery_warnings
from mdclaw.structure.split import split_molecules


def _atom(serial, name, resname, chain, resnum, icode, x):
    padded = name if len(name) == 4 else f" {name:<3s}"
    return ("ATOM  %5d %4s %3s %1s%4d%1s   %8.3f%8.3f%8.3f%6.2f%6.2f          %2s"
            % (serial, padded, resname, chain, resnum, icode, x, 0.0, 0.0, 1.0, 0.0, name[0]))


def _toy_pdb_text():
    """Chain A: 1, 1A, 2, 3, 6, 7 (residues 4-5 missing); chain B: 10-12."""
    residues = [("A", 1, " ", "MET"), ("A", 1, "A", "GLY"), ("A", 2, " ", "ALA"),
                ("A", 3, " ", "SER"), ("A", 6, " ", "LYS"), ("A", 7, " ", "GLU"),
                ("B", 10, " ", "ALA"), ("B", 11, " ", "GLY"), ("B", 12, " ", "VAL")]
    lines, serial = [], 1
    for index, (chain, num, icode, name) in enumerate(residues):
        for atom, dx in (("N", 0.0), ("CA", 1.5), ("C", 2.5), ("O", 3.0)):
            lines.append(_atom(serial, atom, name, chain, num, icode, index * 4.0 + dx))
            serial += 1
        if chain == "A" and num == 7 or chain == "B" and num == 12:
            lines.append("TER")
    lines.append("END")
    return "\n".join(lines) + "\n"


@pytest.fixture
def pdb(tmp_path):
    path = tmp_path / "toy.pdb"
    path.write_text(_toy_pdb_text())
    return path


def test_inspect_reports_chain_ranges_in_the_default_output(pdb):
    result = inspect_molecules(structure_file=str(pdb))
    assert result["success"], result["errors"]
    by_chain = {c["author_chain"]: c for c in result["chain_ranges"]}
    a = by_chain["A"]
    assert (a["first"], a["last"], a["count"], a["span"]) == ("1 MET", "7 GLU", 6, "1-7")
    assert a["gaps"] == ["4-5"] and a["missing_count"] == 2
    assert a["insertion_codes"] == ["1A"] and a["chain_type"] == "protein"
    assert by_chain["B"]["gaps"] == [] and by_chain["B"]["count"] == 3
    # everything large is cut from the brief output; this is kept
    brief = brief_result(result, limit=10)
    assert brief["chain_ranges"] == result["chain_ranges"]
    assert brief["chains"].get("_omitted") is True


def test_split_reports_what_each_chain_kept(pdb, tmp_path):
    out = split_molecules(str(pdb), output_dir=str(tmp_path / "split"), residue_ranges=["A:1-6"])
    assert out["success"], out["errors"]
    kept = {e["author_chain"]: e for e in out["delivered_chain_ranges"]}
    assert kept["A"]["requested"] == ["A:1-6"]
    assert (kept["A"]["first"], kept["A"]["last"], kept["A"]["count"]) == ("1 MET", "6 LYS", 5)
    assert kept["A"]["gaps"] == ["4-5"] and kept["A"]["insertion_codes"] == ["1A"]
    assert kept["B"]["requested"] is None and kept["B"]["span"] == "10-12"
    info = next(i for i in out["chain_file_info"] if i["author_chain"] == "A")
    assert info["delivered_range"]["count"] == 5 and Path(kept["A"]["file"]).exists()
    # a range that reaches past the deposited residues, and one that spans a gap, are said so
    beyond = split_molecules(str(pdb), output_dir=str(tmp_path / "beyond"), residue_ranges=["A:1-9"])
    warnings = _range_delivery_warnings(beyond)
    assert any("reaches beyond the deposited residues" in w for w in warnings)
    assert any("absent from the deposited structure (4-5; 2 missing)" in w for w in warnings)
    assert _range_delivery_warnings(split_molecules(
        str(pdb), output_dir=str(tmp_path / "plain"), residue_ranges=["B:10-12"])) == []


def test_source_node_carries_chain_ranges_and_next_points_at_the_inspection(pdb, tmp_path):
    from mdclaw.research.source_node import register_local_structure

    jd = tmp_path / "job"
    jd.mkdir()
    src = create_node(str(jd), "source")["node_id"]
    result = register_local_structure(str(pdb), job_dir=str(jd), node_id=src)
    assert result["success"], result["errors"]
    assert [c["author_chain"] for c in result["chain_ranges"]] == ["A", "B"]
    node = read_node(str(jd), src)
    assert node["status"] == "completed"
    assert node["metadata"]["chain_ranges"] == result["chain_ranges"]
    step = next_step(str(jd), src, {})
    assert step["action"] == "create" and step["node_type"] == "prep"
    assert "inspect_molecules" in step["inspect_command"]
    assert step["chain_ranges"] == result["chain_ranges"]
