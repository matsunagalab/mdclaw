"""Numbering is identity, not a prescription to synthesize integer intervals."""

import os

import pytest

gemmi = pytest.importorskip("gemmi")

from mdclaw.structure.residue_identity import (  # noqa: E402
    chain_residues, checked_structure_override, compare_identity, selection_identity,
)
from mdclaw.structure.residue_range import parse_residue_ranges  # noqa: E402


def polymer(numbers=(5, 7, 8), names=("ALA", "HIS", "PHE"), sequence=None):
    structure = gemmi.Structure()
    model = gemmi.Model("1")
    chain = gemmi.Chain("A")
    for i, (number, name) in enumerate(zip(numbers, names), 1):
        residue = gemmi.Residue()
        residue.name = name
        residue.seqid = gemmi.SeqId(str(number))
        residue.label_seq = i
        for atom_name in ("N", "CA", "C", "O"):
            atom = gemmi.Atom()
            atom.name = atom_name
            atom.element = gemmi.Element(atom_name[0])
            residue.add_atom(atom)
        chain.add_residue(residue)
    model.add_chain(chain)
    structure.add_model(model)
    structure.setup_entities()
    if sequence:
        structure.entities[0].full_sequence = sequence
    return structure


def selected(structure, spec="A:5-8", block=None):
    return selection_identity(structure, structure[0][0].subchains()[0],
                              parse_residue_ranges(spec), block)


def test_number_gap_without_sequence_is_not_a_missing_residue():
    result = selected(polymer())
    assert result["evidence"] == "coordinates_only"
    assert [r["number"] for r in result["residues"]] == [5, 7, 8]
    assert not result["unresolved_endpoints"]


def test_unknown_endpoint_is_not_invented():
    result = selected(polymer(), "A:5-9")
    assert len(result["residues"]) == 3
    assert result["unresolved_endpoints"] == ["9"]


def test_insertions_follow_source_order_and_multiple_ranges_stay_separate():
    structure = polymer(("5A", 5, 8))
    assert len(selected(structure, "A:5A-8")["residues"]) == 3
    assert [r["number"] for r in selected(polymer(), "A:5-5,A:8-8")["residues"]] == [5, 8]


def test_scheme_distinguishes_true_unobserved_from_number_gap():
    structure = polymer()
    label = structure[0][0].subchains()[0].subchain_id()
    block = gemmi.cif.Block("test")
    block.set_mmcif_category("_pdbx_poly_seq_scheme.", {
        "asym_id": [label] * 4, "seq_id": ["1", "2", "3", "4"],
        "mon_id": ["ALA", "GLY", "HIS", "PHE"],
        "pdb_seq_num": ["5", "6", "7", "8"], "pdb_ins_code": ["."] * 4,
    })
    expected = selected(structure, block=block)["residues"]
    assert expected[1]["observed"] is False
    audit = compare_identity(expected, chain_residues(structure[0][0]))
    assert [r["number"] for r in audit["missing"]] == [6]


@pytest.mark.parametrize("names,missing,extra", [
    (("ALA", "HIE", "PHE"), 0, 0),
    (("ALA", "HIS", "PHE", "LYS"), 0, 1),
    (("ALA", "HIS"), 1, 0),
    (("ALA", "LYS", "PHE"), 1, 1),
])
def test_prepared_sequence_not_just_count(names, missing, extra):
    expected = selected(polymer())["residues"]
    actual = chain_residues(polymer(range(1, len(names) + 1), names)[0][0])
    audit = compare_identity(expected, actual)
    assert len(audit["missing"]) == missing
    assert len(audit["unexpected"]) == extra
    assert audit["mapping"][0]["source"]["number"] == 5
    assert audit["mapping"][0]["prepared"]["number"] == 1


def test_override_restores_sequence_and_rejects_changed_construct(tmp_path):
    source = tmp_path / "source.cif"
    override = tmp_path / "override.pdb"
    output = tmp_path / "checked.cif"
    polymer(sequence=["ALA", "HIS", "PHE"]).make_mmcif_document().write_file(str(source))
    polymer().write_pdb(str(override))
    checked_structure_override(source, override, output)
    result = gemmi.read_structure(str(output))
    assert list(result.entities[0].full_sequence) == ["ALA", "HIS", "PHE"]
    polymer((5, 7, 8, 9), ("ALA", "HIS", "PHE", "LYS")).write_pdb(str(override))
    with pytest.raises(ValueError, match="new source node"):
        checked_structure_override(source, override, output)


def test_override_scheme_entity_ids_match_the_new_atom_labels(tmp_path):
    structure = polymer(sequence=["ALA", "HIS", "PHE"])
    structure.entities[0].name = "1"
    document = structure.make_mmcif_document()
    label = structure[0][0].subchains()[0].subchain_id()
    document.sole_block().set_mmcif_category("_pdbx_poly_seq_scheme.", {
        "asym_id": [label] * 3, "entity_id": ["1"] * 3,
        "seq_id": ["1", "2", "3"], "mon_id": ["ALA", "HIS", "PHE"],
        "pdb_seq_num": ["5", "7", "8"], "pdb_ins_code": ["."] * 3,
    })
    source, override, output = (tmp_path / name for name in ("source.cif", "override.pdb", "checked.cif"))
    document.write_file(str(source))
    polymer().write_pdb(str(override))
    checked_structure_override(source, override, output)
    block = gemmi.cif.read(str(output)).sole_block()
    scheme = block.get_mmcif_category("_pdbx_poly_seq_scheme.")
    atoms = block.get_mmcif_category("_atom_site.")
    assert set(scheme["entity_id"]) == set(atoms["label_entity_id"])
    assert set(scheme["asym_id"]) == set(atoms["label_asym_id"])


def test_explicit_prep_reinput_cannot_bypass_source_resolution(tmp_path, monkeypatch):
    from mdclaw.structure.prepare_complex import _resolve_prepare_node_structure_file

    source, override = tmp_path / "source.pdb", tmp_path / "extra.pdb"
    polymer().write_pdb(str(source))
    polymer((5, 7, 8, 9), ("ALA", "HIS", "PHE", "LYS")).write_pdb(str(override))
    monkeypatch.setattr("mdclaw._node.resolve_node_inputs", lambda *args: {
        "source_bundle_file": "bundle.json"})
    monkeypatch.setattr("mdclaw.source_bundle.materialize_source_selection", lambda **kwargs: {
        "structure_file": str(source), "source_bundle_file": "bundle.json"})
    result = _resolve_prepare_node_structure_file(str(tmp_path), "prep_002", str(override))
    assert result["structure_file"] is None
    assert "new source node" in result["input_resolution_error"]


def test_model_selection_keeps_sequence_and_author_scheme(tmp_path):
    from mdclaw.source_bundle import _write_single_model

    structure = polymer(sequence=["ALA", "GLY", "HIS", "PHE"])
    second = gemmi.Model("2")
    second.add_chain(structure[0][0].clone())
    structure.add_model(second)
    document = structure.make_mmcif_document()
    scheme = {"asym_id": [structure[0][0].subchains()[0].subchain_id()] * 4,
              "seq_id": ["1", "2", "3", "4"], "mon_id": ["ALA", "GLY", "HIS", "PHE"],
              "pdb_seq_num": ["5", "6", "7", "8"], "pdb_ins_code": ["."] * 4}
    document.sole_block().set_mmcif_category("_pdbx_poly_seq_scheme.", scheme)
    source, output = tmp_path / "models.cif", tmp_path / "selected.cif"
    document.write_file(str(source))
    _write_single_model(source, 1, output)
    result = gemmi.read_structure(str(output))
    assert len(result) == 1
    assert list(result.entities[0].full_sequence) == ["ALA", "GLY", "HIS", "PHE"]
    assert gemmi.cif.read(str(output)).sole_block().get_mmcif_category("_pdbx_poly_seq_scheme.") == scheme


@pytest.mark.parametrize("join", [False, True])
def test_split_and_merge_preserve_component_identity(tmp_path, join):
    from mdclaw.structure.split import split_molecules
    from mdclaw.structure.merge import merge_structures
    from mdclaw.structure.prepare_complex import _residue_range_coverage
    from mdclaw.structure.residue_identity import audit_merged_identity

    source = tmp_path / "source.pdb"
    polymer().write_pdb(str(source))
    split = split_molecules(str(source), output_dir=str(tmp_path / "split"),
                            select_chains=["A"], include_types=["protein"],
                            residue_ranges=["A:5-5", "A:8-8"], join_range_pieces=join)
    assert split["success"], split["errors"]
    proteins = [{"chain_id": c["chain_id"], "input_file": c["file"],
                 "output_file": c["file"], "success": True}
                for c in split["chain_file_info"]]
    coverage = _residue_range_coverage(split, proteins)
    assert coverage["Axp"]["requested"] == 2
    merged = merge_structures(split["protein_files"], output_dir=str(tmp_path / "merge"))
    assert merged["success"], merged["errors"]
    assert not audit_merged_identity(coverage, merged["chain_identity_map"], merged["output_file"])
    components = merged["chain_identity_map"]["components"]
    assert len(components) == (1 if join else 2)
    assert [r["source"]["number"] for c in components
            for r in c["residue_identity"]["mapping"]] == [5, 8]


def test_same_numbered_chains_are_audited_independently(tmp_path):
    from mdclaw.structure.split import split_molecules
    from mdclaw.structure.prepare_complex import _residue_range_coverage

    structure = polymer()
    second = structure[0][0].clone()
    second.name = "B"
    for residue in second:
        residue.subchain = "Bxp"
    structure[0].add_chain(second)
    source = tmp_path / "dimer.pdb"
    structure.write_pdb(str(source))
    split = split_molecules(str(source), output_dir=str(tmp_path / "split"),
                            select_chains=["A", "B"], include_types=["protein"],
                            residue_ranges=["A:5-8", "B:5-8"])
    assert split["success"], split["errors"]
    proteins = [{"chain_id": c["chain_id"], "input_file": c["file"],
                 "output_file": c["file"], "success": True}
                for c in split["chain_file_info"]]
    mutated = gemmi.read_pdb(proteins[0]["output_file"])
    mutated[0][0][0].name = "GLY"
    mutated.write_pdb(proteins[0]["output_file"])
    coverage = _residue_range_coverage(split, proteins)
    assert len(coverage[proteins[0]["chain_id"]]["missing"]) == 1
    assert len(coverage[proteins[0]["chain_id"]]["unexpected"]) == 1
    assert coverage[proteins[1]["chain_id"]]["delivered"] == 3
    assert not coverage[proteins[1]["chain_id"]]["missing"]


@pytest.mark.integration
def test_real_12ca_preparation_keeps_255_and_author_phe260(tmp_path):
    source = os.environ.get("MDCLAW_12CA_CIF")
    if not source:
        pytest.skip("Set MDCLAW_12CA_CIF to a local deposit; no reference is needed")
    from mdclaw.structure.prepare_complex import prepare_complex

    result = prepare_complex(
        structure_file=source, output_dir=str(tmp_path / "prep"),
        select_chains=["A"], residue_ranges=["A:5-260"],
        include_types=["protein", "ion"], process_ligands=False,
    )
    assert result["success"], result.get("errors")
    coverage = result["residue_range_coverage"]["A"]
    assert coverage["requested"] == coverage["delivered"] == 255
    assert not coverage["missing"] and not coverage["unexpected"]
    components = result["chain_identity_map"]["components"]
    audit = next(c["residue_identity"] for c in components if "residue_identity" in c)
    assert not audit["broken_peptide_bonds"]
    last = audit["mapping"][-1]
    assert last["source"]["number"] == last["prepared"]["number"] == 260
    assert last["prepared"]["name"] == "PHE"
