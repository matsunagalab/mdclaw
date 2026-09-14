"""Refusals prepare_complex decides from the split output, before the node begins.

Campaign v2 (2026-09-11) sealed prep nodes after the whole preparation on
failures the split had already determined:

- a declared disulfide named by label chain (028_complex_1dfj, RNase A's
  A:26-A:84 while the pieces carry author chain E);
- a requested range whose end residues are unobserved and that nothing builds
  (008_membrane_6i53, 062_metal_6w9c), or unobserved residues facing another
  joined range of the same chain;
- a protonation state named "ARG" (004_membrane_5zkb, 007_membrane_6gt3).

Each is now refused with the node still pending, or accepted.
"""

import importlib

import pytest

from mdclaw._node import create_node, read_node, update_job_params
from mdclaw.structure.protonation import (
    _apply_protonation_states_with_modeller,
    _normalize_protonation_state_overrides,
)

pc = importlib.import_module("mdclaw.structure.prepare_complex")


def _row(number, observed=True, name="ALA"):
    return {"number": number, "icode": "", "name": name,
            "sequence_position": number, "observed": observed}


def _info(pieces, rows, unresolved=(), chain_id="A", author="A"):
    info = {"file": f"/x/{chain_id}.pdb", "chain_id": chain_id, "author_chain": author,
            "residue_identity": {"evidence": "polymer_scheme", "residues": rows,
                                 "unresolved_endpoints": list(unresolved)}}
    if len(pieces) == 1:
        info["residue_range"] = pieces[0]
    else:
        info["residue_ranges"] = pieces
    return info


def _piece(chain, start, end):
    return {"range": f"{chain}:{start}-{end}", "start": start, "end": end}


# --- unobserved range ends ------------------------------------------------------

def test_an_unobserved_tail_nothing_builds_is_refused_with_the_observed_span():
    rows = [_row(n) for n in range(10, 313)] + [_row(n, observed=False) for n in range(313, 316)]
    split = {"chain_file_info": [_info([_piece("A", 10, 315)], rows)]}
    problems = pc._unbuildable_range_ends(split, terminal_builds=False)
    assert len(problems) == 1
    assert problems[0]["residues"] == "313-315" and problems[0]["end"] == "end"
    assert problems[0]["observed_span"] == "A:10-312"
    # the terminal build (or caps) builds it: nothing to refuse
    assert pc._unbuildable_range_ends(split, terminal_builds=True) == []


def test_unobserved_residues_facing_a_joined_range_are_the_insertion_not_an_end():
    """008_membrane_6i53: A:10-323 + A:384-418 joined, 313-414 unobserved.

    Joined ranges are bonded across the omitted span, so 313-323 and 384-414
    are built as the run between 312 and 415 (numbered from the windows);
    only the true ends of the component can be unbuildable.
    """
    rows = ([_row(n, observed=False) for n in range(8, 10)] + [_row(n) for n in range(10, 313)]
            + [_row(n, observed=False) for n in range(313, 324)]
            + [_row(n, observed=False) for n in range(384, 415)] + [_row(n) for n in range(415, 419)])
    split = {"chain_file_info": [_info([_piece("A", 8, 323), _piece("A", 384, 418)], rows)]}
    assert pc._unbuildable_range_ends(split, terminal_builds=True) == []
    problems = pc._unbuildable_range_ends(split, terminal_builds=False)
    assert [(p["residues"], p["end"]) for p in problems] == [("8-9", "start")]


def test_a_joined_junction_is_numbered_from_the_requested_windows():
    """The MODELLER walk numbers the 42-residue run between 312 and 415."""
    cp = importlib.import_module("mdclaw.structure.clean_protein")

    class _Residue:
        def __init__(self, number):
            self.id, self.insertionCode = str(number), ""

    class _Chain:
        id = "A"

        def residues(self):
            return [_Residue(n) for n in (310, 311, 312, 415, 416)]

    resolved = cp._resolve_target_residue_sites(
        [_Chain()], [["X"] * 47], {(0, 3): ["X"] * 42},
        build_windows_by_chain={"A": [(10, 323), (384, 418)]})
    assert resolved["errors"] == []
    assert resolved["gap_sites"][(0, 3)] == (
        [("A", n, "") for n in range(313, 324)] + [("A", n, "") for n in range(384, 415)])
    assert "joined_range_windows" in resolved["numbering_source"]
    # without windows that account for the run, it still fails closed
    unresolved = cp._resolve_target_residue_sites(
        [_Chain()], [["X"] * 47], {(0, 3): ["X"] * 42},
        build_windows_by_chain={"A": [(10, 330), (384, 418)]})
    assert any("not determined by the flanking" in e for e in unresolved["errors"])


def test_separate_components_of_one_chain_are_terminal_ends_not_junctions():
    first = _info([_piece("B", 8, 312)],
                  [_row(n, observed=False) for n in range(8, 21)] + [_row(n) for n in range(21, 313)],
                  chain_id="B", author="B")
    second = _info([_piece("B", 415, 447)], [_row(n) for n in range(415, 448)], chain_id="B", author="B")
    second["file"] = "/x/B2.pdb"
    split = {"chain_file_info": [first, second]}
    assert pc._unbuildable_range_ends(split, terminal_builds=True) == []
    problems = pc._unbuildable_range_ends(split, terminal_builds=False)
    assert [(p["residues"], p["end"]) for p in problems] == [("8-20", "start")]


def test_an_endpoint_the_source_cannot_identify_is_refused():
    split = {"chain_file_info": [_info([_piece("A", 1, 4)], [_row(1)], unresolved=["4"])]}
    problems = pc._unbuildable_range_ends(split, terminal_builds=True)
    assert len(problems) == 1 and problems[0]["residues"] is None
    assert "endpoint 4" in problems[0]["reason"]


def test_components_without_ranges_are_not_checked():
    split = {"chain_file_info": [{"file": "/x/A.pdb", "chain_id": "A", "author_chain": "A"}]}
    assert pc._unbuildable_range_ends(split, terminal_builds=False) == []


# --- declared disulfide sites -----------------------------------------------------

def _pdb(path, chain, residues):
    lines, serial = [], 1
    for number, name in residues:
        for atom in ("N", "CA", "C", "O") + (("CB", "SG") if name == "CYS" else ("CB",)):
            lines.append(f"ATOM  {serial:5d}  {atom:<3s} {name} {chain}{number:4d}    "
                         f"{serial * 0.5:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           {atom[0]}")
            serial += 1
    path.write_text("\n".join(lines) + "\nEND\n")
    return str(path)


@pytest.fixture
def pieces(tmp_path):
    """Author chain E (label A): residues 1-5 and 9-10, cysteines at 3 and 9."""
    residues = [(1, "ALA"), (2, "ALA"), (3, "CYS"), (4, "ALA"), (5, "ALA"), (9, "CYS"), (10, "ALA")]
    path = _pdb(tmp_path / "protein_1.pdb", "E", residues)
    info = {"file": path, "chain_id": "A", "author_chain": "E"}
    return {"protein_files": [path], "chain_file_info": [info]}


def _pair(a, b):
    return {"cys1": {"chain": a[0], "resnum": a[1]}, "cys2": {"chain": b[0], "resnum": b[1]}}


def test_an_unstated_insertion_code_means_the_residue_without_one(tmp_path):
    """1CEB numbers GLU 1A before CYS 1; "A:1" is the cysteine."""
    path = tmp_path / "protein_1.pdb"
    lines = [
        "ATOM      1  N   GLU A   1A      0.000   0.000   0.000  1.00  0.00           N",
        "ATOM      2  CA  GLU A   1A      1.000   0.000   0.000  1.00  0.00           C",
        "ATOM      3  N   CYS A   1       3.000   0.000   0.000  1.00  0.00           N",
        "ATOM      4  SG  CYS A   1       4.000   0.000   0.000  1.00  0.00           S",
        "ATOM      5  N   CYS A  79       6.000   0.000   0.000  1.00  0.00           N",
        "ATOM      6  SG  CYS A  79       7.000   0.000   0.000  1.00  0.00           S",
    ]
    path.write_text("\n".join(lines) + "\nEND\n")
    split = {"protein_files": [str(path)],
             "chain_file_info": [{"file": str(path), "chain_id": "A", "author_chain": "A"}]}
    assert pc._declared_disulfide_site_problems([_pair(("A", 1), ("A", 79))], split,
                                                terminal_builds=False) == []
    explicit = {"cys1": {"chain": "A", "resnum": 1, "icode": "A"}, "cys2": {"chain": "A", "resnum": 79}}
    assert pc._declared_disulfide_site_problems([explicit], split, terminal_builds=False) == [
        "A:1A is GLU, not a cysteine"]


def test_declared_sites_on_selected_cysteines_pass(pieces):
    assert pc._declared_disulfide_site_problems([_pair(("E", 3), ("E", 9))], pieces,
                                                terminal_builds=False) == []


def test_a_pair_written_in_label_chain_ids_names_the_author_chain(pieces):
    problems = pc._declared_disulfide_site_problems([_pair(("A", 3), ("A", 9))], pieces,
                                                    terminal_builds=False)
    assert len(problems) == 2
    assert "label chain A" in problems[0] and "E:3" in problems[0]


def test_a_site_that_is_not_a_cysteine_is_named(pieces):
    problems = pc._declared_disulfide_site_problems([_pair(("E", 3), ("E", 4))], pieces,
                                                    terminal_builds=False)
    assert problems == ["E:4 is ALA, not a cysteine"]


def test_sites_in_an_internal_gap_or_the_range_contract_pass(pieces):
    # 7 lies in the unobserved 6-8 gap the repair builds
    assert pc._declared_disulfide_site_problems([_pair(("E", 3), ("E", 7))], pieces,
                                                terminal_builds=False) == []
    # 12 is beyond the piece, but the range contract requests it
    pieces["chain_file_info"][0]["residue_identity"] = {
        "residues": [_row(12, observed=False, name="CYS")]}
    assert pc._declared_disulfide_site_problems([_pair(("E", 3), ("E", 12))], pieces,
                                                terminal_builds=False) == []


def test_sites_outside_the_selection_or_on_unselected_chains_are_refused(pieces):
    outside = pc._declared_disulfide_site_problems([_pair(("E", 3), ("E", 40))], pieces,
                                                   terminal_builds=False)
    assert outside and "outside the selected residues of chain E" in outside[0]
    # near the chain end, a terminal build may still make it
    assert pc._declared_disulfide_site_problems([_pair(("E", 3), ("E", 15))], pieces,
                                                terminal_builds=True) == []
    other = pc._declared_disulfide_site_problems([_pair(("E", 3), ("Z", 4))], pieces,
                                                 terminal_builds=False)
    assert other and "chain Z is not a selected protein chain" in other[0]


# --- ARG is an accepted protonation state -------------------------------------

ARG_PDB = """\
ATOM      1  N   ARG A  52       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ARG A  52       1.450   0.000   0.000  1.00  0.00           C
ATOM      3  C   ARG A  52       2.000   1.400   0.000  1.00  0.00           C
ATOM      4  O   ARG A  52       1.300   2.400   0.000  1.00  0.00           O
ATOM      5  CB  ARG A  52       2.000  -0.800  -1.200  1.00  0.00           C
ATOM      6  CG  ARG A  52       3.500  -0.800  -1.200  1.00  0.00           C
ATOM      7  CD  ARG A  52       4.000  -2.200  -1.200  1.00  0.00           C
ATOM      8  NE  ARG A  52       5.450  -2.200  -1.200  1.00  0.00           N
ATOM      9  CZ  ARG A  52       6.200  -3.300  -1.200  1.00  0.00           C
ATOM     10  NH1 ARG A  52       5.700  -4.500  -1.200  1.00  0.00           N
ATOM     11  NH2 ARG A  52       7.500  -3.200  -1.200  1.00  0.00           N
TER
END
"""


def test_arg_is_a_supported_state_and_a_no_op_on_an_arginine(tmp_path):
    records = _normalize_protonation_state_overrides({"A:52": "ARG"})
    assert records == [{"chain": "A", "resnum": "52", "icode": "", "state": "ARG"}]
    pdb = tmp_path / "arg.pdb"
    pdb.write_text(ARG_PDB)
    result = _apply_protonation_states_with_modeller(pdb, records, ph=7.0)
    assert result["success"], result["errors"]
    assert result["applied_states"][0]["already_in_requested_state"] is True


def test_arg_on_a_residue_that_is_not_an_arginine_is_still_refused(tmp_path):
    pdb = tmp_path / "lys.pdb"
    pdb.write_text(ARG_PDB.replace("ARG A  52", "LYS A  52"))
    result = _apply_protonation_states_with_modeller(
        pdb, [{"chain": "A", "resnum": "52", "icode": "", "state": "ARG"}], ph=7.0)
    assert not result["success"]
    assert "incompatible" in result["errors"][0]


# --- in node mode, through prepare_complex --------------------------------------

_CIF = """\
data_TEST
#
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_alt_id
_atom_site.label_comp_id
_atom_site.label_asym_id
_atom_site.label_entity_id
_atom_site.label_seq_id
_atom_site.pdbx_PDB_ins_code
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
_atom_site.occupancy
_atom_site.B_iso_or_equiv
_atom_site.pdbx_formal_charge
_atom_site.auth_seq_id
_atom_site.auth_comp_id
_atom_site.auth_asym_id
_atom_site.auth_atom_id
_atom_site.pdbx_PDB_model_num
ATOM   1 N N   . ALA A 1 1   ? 0.0 0.0 0.0 1.0 0.0 ? 1   ALA A N   1
ATOM   2 C CA  . ALA A 1 1   ? 1.5 0.0 0.0 1.0 0.0 ? 1   ALA A CA  1
ATOM   3 C C   . ALA A 1 1   ? 2.0 1.5 0.0 1.0 0.0 ? 1   ALA A C   1
ATOM   4 O O   . ALA A 1 1   ? 1.5 2.5 0.0 1.0 0.0 ? 1   ALA A O   1
#
"""


def _prep(tmp_path):
    from mdclaw.research.source_node import register_local_structure

    deposit = tmp_path / "one_alanine.cif"
    deposit.write_text(_CIF, encoding="utf-8")
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    update_job_params(str(job_dir), {"solvent_regime": "explicit"})
    source = create_node(str(job_dir), "source")["node_id"]
    assert register_local_structure(str(deposit), str(job_dir), source)["success"]
    return job_dir, create_node(str(job_dir), "prep")["node_id"]


def test_an_unidentifiable_range_end_refuses_with_the_node_pending(tmp_path):
    job_dir, prep = _prep(tmp_path)
    result = pc.prepare_complex(job_dir=str(job_dir), node_id=prep, select_chains=["A"],
                                include_types=["protein"], residue_ranges=["A:1-4"])
    assert result["code"] == "residue_range_endpoint_unobserved", result.get("errors")
    assert result["node_status"] == "pending"
    assert read_node(str(job_dir), prep)["status"] == "pending"


def test_a_disulfide_on_an_unselected_chain_refuses_with_the_node_pending(tmp_path):
    job_dir, prep = _prep(tmp_path)
    result = pc.prepare_complex(job_dir=str(job_dir), node_id=prep, select_chains=["A"],
                                include_types=["protein"],
                                disulfide_pairs=[_pair(("A", 1), ("B", 2))])
    assert result["code"] == "disulfide_site_not_selected", result.get("errors")
    assert read_node(str(job_dir), prep)["status"] == "pending"


def test_an_unknown_protonation_state_refuses_before_the_split(tmp_path):
    job_dir, prep = _prep(tmp_path)
    result = pc.prepare_complex(job_dir=str(job_dir), node_id=prep, select_chains=["A"],
                                include_types=["protein"], protonation_states={"A:1": "XYZ"})
    assert result["code"] == "invalid_protonation_state"
    assert not list((job_dir / "nodes" / prep / "artifacts").glob("split*")), "refused before the split"
    assert read_node(str(job_dir), prep)["status"] == "pending"


def test_the_callers_disulfide_pairs_are_not_rewritten(tmp_path):
    """The receipt's "requested" pairs are the caller's own dicts.

    028_complex_1dfj passed E:26-E:84; the merged-frame remap rewrote those
    dicts to A:26-A:84 with original_chain E, and the receipt showed that.
    """
    import copy

    job_dir, prep = _prep(tmp_path)
    pairs = [_pair(("A", 1), ("B", 2))]
    before = copy.deepcopy(pairs)
    pc.prepare_complex(job_dir=str(job_dir), node_id=prep, select_chains=["A"],
                       include_types=["protein"], disulfide_pairs=pairs)
    assert pairs == before


def test_a_list_of_one_site_protonation_mappings_is_accepted():
    """016_antibody_1ay7 wrote [{"A:7": "CYS"}, ...] and was refused."""
    records = _normalize_protonation_state_overrides(
        [{"A:7": "CYS"}, {"B:40": "CYS"}, {"chain": "B", "resnum": 82, "state": "CYS"}])
    assert [(r["chain"], r["resnum"], r["state"]) for r in records] == [
        ("A", "7", "CYS"), ("B", "40", "CYS"), ("B", "82", "CYS")]


def test_a_declared_pair_the_deposit_holds_apart_is_refused_before_the_node_begins(tmp_path, monkeypatch):
    """035_nanobody_6gwn: B22-B96 is 3.49 A apart in the deposit; nothing rebuilds it."""
    job_dir, prep = _prep(tmp_path)
    import mdclaw.structure.disulfide as ss

    monkeypatch.setattr(ss, "measure_disulfide_pairs", lambda path, pairs: [
        {"chain1": "A", "resnum1": 1, "icode1": "", "chain2": "A", "resnum2": 1, "icode2": "",
         "sg_sg_angstrom": 3.485, "geometry": "not_formed"}])
    result = pc.prepare_complex(job_dir=str(job_dir), node_id=prep, select_chains=["A"],
                                include_types=["protein"],
                                disulfide_pairs=[_pair(("A", 1), ("A", 2))])
    assert result["code"] == "declared_disulfide_unbonded_in_source"
    assert "3.48 A apart" in result["errors"][0] or "3.49 A apart" in result["errors"][0]
    assert read_node(str(job_dir), prep)["status"] == "pending"


def test_a_protonation_state_outside_the_selection_refuses_with_the_node_pending(tmp_path):
    """021_antibody_3eoa: "B:6" on a selection without chain B sealed a prep node."""
    job_dir, prep = _prep(tmp_path)
    result = pc.prepare_complex(job_dir=str(job_dir), node_id=prep, select_chains=["A"],
                                include_types=["protein"], protonation_states={"B:6": "GLU"})
    assert result["code"] == "invalid_protonation_state", result.get("errors")
    assert "B:6" in result["errors"][0] and "components are A" in result["errors"][0]
    assert read_node(str(job_dir), prep)["status"] == "pending"
