"""Label or author chain ids, when the two id sets are permutations of each other.

1KX5 (nucleosome): the mmCIF label ids A..J are a rotation of the author ids
(author D is label F, author I is label A). Read as labels, the task's
author-numbered ranges landed on other chains and every endpoint came back
"unresolved" (051_nucleic_1kx5, campaign v2: three cli_sif timeouts).

Label ids are the documented convention, though, and the first fix -- read as
author whenever the names allow it -- broke every call that followed it:
1IV6 "A:1-13" is label A's DNA strand while author A is the protein 378-434
(and 1A66, 1J46, 1ZGW, 2HDC alike). The residue numbers decide: author ids
only when the ranges fit the author chains strictly better.
"""

import gemmi

from mdclaw.structure.split import split_molecules


def _two_chain_cif(path, swap_labels, b_offset=0):
    """Author A (ALA 1-4) and author B (GLY 1-4, shifted by ``b_offset``)."""
    st = gemmi.Structure()
    model = gemmi.Model("1")
    for auth, resname, label in (("A", "ALA", "B" if swap_labels else "A"), ("B", "GLY", "A" if swap_labels else "B")):
        chain = gemmi.Chain(auth)
        for i in range(1, 5):
            res = gemmi.Residue()
            res.name = resname
            res.seqid = gemmi.SeqId(str(i + (b_offset if auth == "B" else 0)))
            res.subchain = label
            res.label_seq = i
            res.entity_type = gemmi.EntityType.Polymer
            for j, name in enumerate(("N", "CA", "C", "O")):
                atom = gemmi.Atom()
                atom.name = name
                atom.element = gemmi.Element(name[0])
                atom.pos = gemmi.Position(i * 3.8 + j * 0.5, 0.0 if auth == "A" else 20.0, j * 0.7)
                res.add_atom(atom)
            chain.add_residue(res)
        model.add_chain(chain)
    st.add_model(model)
    st.setup_entities()
    st.make_mmcif_document().write_file(str(path))
    return path


def _residue_names(pdb_file):
    return {line[17:20].strip() for line in open(pdb_file) if line.startswith("ATOM")}


def test_permuted_ids_are_read_as_author_when_only_the_author_numbering_fits(tmp_path):
    # label A is author B, numbered 101-104; "A:1-4" only fits author A
    cif = _two_chain_cif(tmp_path / "swapped.cif", swap_labels=True, b_offset=100)
    split = split_molecules(str(cif), output_dir=str(tmp_path / "split"),
                            select_chains=["A"], include_types=["protein"], residue_ranges=["A:1-4"])
    assert split["success"], split["errors"]
    assert [a["code"] for a in split["selection_adjustments"]] == ["chain_ids_read_as_author"]
    assert _residue_names(split["protein_files"][0]) == {"ALA"}      # author A, not label A (= author B)


def test_permuted_ids_stay_label_ids_when_the_label_numbering_fits(tmp_path):
    """1IV6: "A:1-13" names label A (DNA 1-13); author A is the protein."""
    cif = _two_chain_cif(tmp_path / "swapped.cif", swap_labels=True, b_offset=100)
    split = split_molecules(str(cif), output_dir=str(tmp_path / "split"),
                            select_chains=["A"], include_types=["protein"], residue_ranges=["A:101-104"])
    assert split["success"], split["errors"]
    assert not any(a["code"] == "chain_ids_read_as_author" for a in split["selection_adjustments"])
    assert _residue_names(split["protein_files"][0]) == {"GLY"}      # label A = author B


def test_permuted_ids_with_no_numbering_evidence_stay_label_ids(tmp_path):
    """Equal numbering (or no ranges): the documented label reading holds."""
    cif = _two_chain_cif(tmp_path / "swapped.cif", swap_labels=True)
    for ranges in (["A:1-4"], None):
        split = split_molecules(str(cif), output_dir=str(tmp_path / f"split{bool(ranges)}"),
                                select_chains=["A"], include_types=["protein"], residue_ranges=ranges)
        assert split["success"], split["errors"]
        assert not any(a["code"] == "chain_ids_read_as_author" for a in split["selection_adjustments"])
        assert _residue_names(split["protein_files"][0]) == {"GLY"}  # label A = author B


def test_matching_ids_are_untouched(tmp_path):
    cif = _two_chain_cif(tmp_path / "plain.cif", swap_labels=False)
    split = split_molecules(str(cif), output_dir=str(tmp_path / "split"),
                            select_chains=["A"], include_types=["protein"], residue_ranges=["A:1-4"])
    assert split["success"], split["errors"]
    assert not any(a["code"] == "chain_ids_read_as_author" for a in split["selection_adjustments"])
    assert _residue_names(split["protein_files"][0]) == {"ALA"}
