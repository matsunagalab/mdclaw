"""Preparation guardrails from MDDataBench campaign v5 (prep-side defects).

- The disulfide/metal chain filter read a request differently from the split:
  2DD8 "H L S" kept the heavy chain in the split while its disulfides were
  filtered out (label H is the water of author S), 13 attempts across 2DD8,
  1DFJ and 3RVW. One resolution rule now serves every reader.
- A disulfide the deposit shows can no longer be lost silently: a declared
  list that omits it is refused, the prep checks the merged file, and the
  topology refuses two sulfurs at bonding distance without a bond.
- Exclusions that match nothing, ranges without a chain selection, the
  associated-ligand refusal's next step, terminal caps on a native end, and
  ligand protonation chosen by Dimorphite-DL's output order.
"""
from __future__ import annotations

import importlib
import textwrap
from pathlib import Path

import pytest

from mdclaw._node import create_node, read_node, update_job_params
from mdclaw.structure.split import (
    _inspect_molecules_impl,
    resolve_chain_selection,
    split_molecules,
)

pc = importlib.import_module("mdclaw.structure.prepare_complex")
lc = importlib.import_module("mdclaw.structure.ligand_chemistry")


# --------------------------------------------------------------------------
# A 2DD8-like deposit: three protein chains whose author ids H/L/S collide
# with the label ids of the waters (label H is the water of author S).
# --------------------------------------------------------------------------

_ATOM_SITE_HEADER = """\
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
"""


def _cys_atoms(sg_x, mirrored):
    """CYS backbone and side chain with SG at (sg_x, 0, 0), the rest pointing away."""
    sign = 1.0 if mirrored else -1.0
    return [("N", "N", sg_x + sign * 3.6, 1.2), ("CA", "C", sg_x + sign * 2.8, 0.4),
            ("C", "C", sg_x + sign * 3.4, -0.9), ("O", "O", sg_x + sign * 4.5, -1.1),
            ("CB", "C", sg_x + sign * 1.5, 0.9), ("SG", "S", sg_x, 0.0)]


def _collision_cif(tmp_path: Path) -> Path:
    rows = []
    serial = 1
    chains = [("A", "H", 1, (22, 96), 0.0), ("B", "L", 2, (23, 88), 20.0),
              ("C", "S", 3, (323, 348), 40.0)]
    for label, author, entity, numbers, x0 in chains:
        for seq, (number, sg_x, mirrored) in enumerate(
                ((numbers[0], x0, False), (numbers[1], x0 + 2.03, True)), start=1):
            for name, element, x, y in _cys_atoms(sg_x, mirrored):
                rows.append(f"ATOM {serial} {element} {name} . CYS {label} {entity} {seq} ? "
                            f"{x:.3f} {y:.3f} 0.000 1.00 0.00 ? {number} CYS {author} {name} 1")
                serial += 1
    for label, author, number, x in (("F", "H", 301, 0.0), ("G", "L", 301, 20.0),
                                     ("H", "S", 601, 40.0)):
        rows.append(f"HETATM {serial} O O . HOH {label} 4 . ? {x:.3f} 9.000 0.000 1.00 0.00 ? "
                    f"{number} HOH {author} O 1")
        serial += 1
    path = tmp_path / "collision.cif"
    path.write_text("data_COLLIDE\n" + _ATOM_SITE_HEADER + "\n".join(rows) + "\n#\n")
    return path


def test_author_names_that_collide_with_water_labels_resolve_to_the_proteins(tmp_path):
    cif = _collision_cif(tmp_path)
    selection = resolve_chain_selection(cif, ["H", "L", "S"])
    assert selection["author_chains"] == ["H", "L", "S"]
    assert {"A", "B", "C"} <= set(selection["label_ids"])
    # one comma token is the same request
    assert resolve_chain_selection(cif, ["H,L,S"])["author_chains"] == ["H", "L", "S"]
    ranged = resolve_chain_selection(
        cif, ["H", "L", "S"], residue_ranges=["H:22-96", "L:23-88", "S:323-348"])
    assert ranged["read_as_author"] is True
    assert ranged["author_chains"] == ["H", "L", "S"]
    assert ranged["range_windows_by_author"] == {"H": [(22, 96)], "L": [(23, 88)], "S": [(323, 348)]}


def test_split_delivers_every_requested_protein_through_a_water_label(tmp_path):
    cif = _collision_cif(tmp_path)
    out = split_molecules(str(cif), output_dir=str(tmp_path / "split"),
                          select_chains=["H", "L", "S"], include_types=["protein"])
    assert out["success"], out["errors"]
    assert sorted(i["author_chain"] for i in out["chain_file_info"]) == ["H", "L", "S"]
    assert any(a["code"] == "chain_id_read_as_author_over_water_label"
               for a in out["selection_adjustments"])


def _stub_after_detection(monkeypatch):
    """Stop prepare_complex after detection: an empty split, inert cleaning."""
    def fake_split(*args, **kwargs):
        return {"success": True, "output_dir": str(kwargs.get("output_dir", ".")),
                "protein_files": [], "ligand_files": [], "ion_files": [], "water_files": [],
                "chain_file_info": [], "all_chains": [], "errors": []}

    monkeypatch.setattr(pc, "split_molecules", fake_split)
    monkeypatch.setattr(pc, "clean_protein", lambda *a, **k: {"success": True})
    monkeypatch.setattr(pc, "merge_structures", lambda *a, **k: {"success": True, "output_file": None})


@pytest.mark.parametrize("select_chains,ranges", [
    (["H", "L", "S"], None),
    (["H,L,S"], None),
    (["H", "L", "S"], ["H:22-96", "L:23-88", "S:323-348"]),
])
def test_detected_disulfides_cover_every_selected_chain(tmp_path, monkeypatch, select_chains, ranges):
    """2DD8 (campaign v5): the filter read [S, L, S] and dropped H's disulfides."""
    _stub_after_detection(monkeypatch)
    result = pc.prepare_complex(structure_file=str(_collision_cif(tmp_path)),
                                output_dir=str(tmp_path / "out"), select_chains=select_chains,
                                include_types=["protein"], residue_ranges=ranges)
    chains = sorted(bond["cys1"]["chain"] for bond in result["disulfide_bonds"])
    assert chains == ["H", "L", "S"], result.get("errors")


def test_declared_list_leaving_out_a_bonded_pair_is_refused_with_both_fixes(tmp_path, monkeypatch):
    _stub_after_detection(monkeypatch)
    declared = [{"cys1": {"chain": "L", "resnum": 23}, "cys2": {"chain": "L", "resnum": 88}},
                {"cys1": {"chain": "S", "resnum": 323}, "cys2": {"chain": "S", "resnum": 348}}]
    result = pc.prepare_complex(structure_file=str(_collision_cif(tmp_path)),
                                output_dir=str(tmp_path / "out"), select_chains=["H", "L", "S"],
                                include_types=["protein"], disulfide_pairs=declared)
    assert result["code"] == "detected_disulfides_suppressed"
    assert result["context"]["suppressed_disulfides"] == ["H:22-H:96"]
    assert '"form_bond": false' in result["next_action"]
    # the same pair named with form_bond false is a decision, not an omission
    kept = declared + [{"cys1": {"chain": "H", "resnum": 22}, "cys2": {"chain": "H", "resnum": 96},
                        "form_bond": False}]
    again = pc.prepare_complex(structure_file=str(_collision_cif(tmp_path)),
                               output_dir=str(tmp_path / "out2"), select_chains=["H", "L", "S"],
                               include_types=["protein"], disulfide_pairs=kept)
    assert again.get("code") != "detected_disulfides_suppressed"


def test_a_range_that_cuts_a_cysteine_does_not_count_as_an_omission(tmp_path, monkeypatch):
    _stub_after_detection(monkeypatch)
    result = pc.prepare_complex(structure_file=str(_collision_cif(tmp_path)),
                                output_dir=str(tmp_path / "out"), select_chains=["H"],
                                include_types=["protein"], residue_ranges=["H:22-50"],
                                disulfide_pairs=[])
    assert result.get("code") != "detected_disulfides_suppressed"


def test_empty_chain_filter_selects_no_disulfide():
    from mdclaw.structure.disulfide import _merge_disulfide_pairs

    pair = {"cys1": {"chain": "A", "resnum": 1}, "cys2": {"chain": "A", "resnum": 2},
            "source": "distance", "distance_angstrom": 2.03}
    assert _merge_disulfide_pairs([], [pair], select_chains=[]) == []
    assert len(_merge_disulfide_pairs([], [pair], select_chains=None)) == 1


# --------------------------------------------------------------------------
# Invariants on the merged file and on the final topology
# --------------------------------------------------------------------------

def _pdb_line(serial, name, resname, chain, resnum, x, y=0.0, element=None, record="ATOM  "):
    padded = name if len(name) == 4 else f" {name:<3s}"
    element = element or name[0]
    return (f"{record}{serial:5d} {padded} {resname:>3s} {chain}{resnum:4d}    "
            f"{x:8.3f}{y:8.3f}{0.0:8.3f}  1.00  0.00          {element:>2s}")


def _two_cys_pdb(path, resname="CYS", distance=2.03, metal_at=None):
    lines, serial = [], 1
    for index, number in enumerate((22, 96)):
        sg_x = 0.0 if index == 0 else distance
        for name, element, x, y in _cys_atoms(sg_x, index == 1):
            lines.append(_pdb_line(serial, name, resname, "A", number, x, y, element))
            serial += 1
    if metal_at is not None:
        lines.append(_pdb_line(serial, "ZN", "ZN", "B", 301, metal_at[0], metal_at[1], "ZN", "HETATM"))
    path.write_text("\n".join(lines) + "\nEND\n")
    return path


def _bond(form_bond=None):
    pair = {"cys1": {"chain": "A", "resnum": 22}, "cys2": {"chain": "A", "resnum": 96}}
    if form_bond is not None:
        pair["form_bond"] = form_bond
    return pair


def test_merged_file_with_sulfurs_at_bond_distance_and_no_bond_is_flagged(tmp_path):
    merged = _two_cys_pdb(tmp_path / "merged.pdb")
    problems = pc._unformed_disulfides_in_merged(merged, [])
    assert [p["merged"] for p in problems] == ["A:22-A:96"]
    assert problems[0]["reason"] == "sulfurs_within_bond_distance"
    # formed: both CYX and named
    formed = _two_cys_pdb(tmp_path / "formed.pdb", resname="CYX")
    assert pc._unformed_disulfides_in_merged(formed, [_bond()]) == []
    # named but left CYS is not formed
    assert pc._unformed_disulfides_in_merged(merged, [_bond()])
    # kept reduced on purpose
    assert pc._unformed_disulfides_in_merged(merged, [_bond(form_bond=False)]) == []


def test_metal_bridged_sulfurs_are_not_a_disulfide(tmp_path):
    zinc = _two_cys_pdb(tmp_path / "zn.pdb", distance=2.2, metal_at=(1.1, 1.9))
    assert pc._unformed_disulfides_in_merged(zinc, []) == []


def test_deposit_record_mapped_onto_unformed_cysteines_is_flagged(tmp_path):
    merged = _two_cys_pdb(tmp_path / "merged.pdb", distance=3.6)   # SG rebuilt apart
    record = {"cys1": {"chain": "A", "resnum": 22}, "cys2": {"chain": "A", "resnum": 96},
              "source": "pdb_ssbond", "distance_angstrom": None}
    problems = pc._unformed_disulfides_in_merged(merged, [], deposit_pairs=[record])
    assert problems and problems[0]["reason"] == "recorded_in_deposit"
    assert pc._unformed_disulfides_in_merged(merged, [], deposit_pairs=[]) == []


def _cys_topology(distance=2.03, bonded=False, zinc=False):
    from openmm import Vec3, unit
    from openmm.app import Element, Topology

    topology = Topology()
    chain = topology.addChain("A")
    positions, sulfurs = [], []
    for number, x in ((22, 0.0), (96, distance)):
        residue = topology.addResidue("CYX" if bonded else "CYS", chain, id=str(number))
        sulfurs.append(topology.addAtom("SG", Element.getBySymbol("S"), residue))
        positions.append(Vec3(x / 10.0, 0.0, 0.0))
    if bonded:
        topology.addBond(*sulfurs)
    if zinc:
        ion_chain = topology.addChain("B")
        residue = topology.addResidue("ZN", ion_chain, id="301")
        topology.addAtom("ZN", Element.getBySymbol("Zn"), residue)
        positions.append(Vec3(distance / 20.0, 0.19, 0.0))
    return topology, unit.Quantity(positions, unit.nanometer)


def test_topology_refuses_unbonded_sulfurs_at_bonding_distance():
    pytest.importorskip("openmm")
    from mdclaw.amber.disulfide_contract import unbonded_disulfide_geometry_errors

    topology, positions = _cys_topology()
    errors = unbonded_disulfide_geometry_errors(topology, positions, [])
    assert len(errors) == 1 and "SG-SG 2.03 A" in errors[0]
    bonded, bonded_positions = _cys_topology(bonded=True)
    assert unbonded_disulfide_geometry_errors(bonded, bonded_positions, []) == []
    zinc, zinc_positions = _cys_topology(distance=2.3, zinc=True)
    assert unbonded_disulfide_geometry_errors(zinc, zinc_positions, []) == []
    assert unbonded_disulfide_geometry_errors(topology, positions, [_bond(form_bond=False)]) == []
    far, far_positions = _cys_topology(distance=3.8)
    assert unbonded_disulfide_geometry_errors(far, far_positions, []) == []


def test_final_topology_validation_fails_on_unbonded_sulfurs():
    pytest.importorskip("openmm")
    import openmm

    from mdclaw.amber.topology_validation import _validate_final_disulfides

    topology, positions = _cys_topology()
    system = openmm.System()
    for _ in range(topology.getNumAtoms()):
        system.addParticle(32.0)
    report = _validate_final_disulfides(topology=topology, system=system, disulfide_bonds=[],
                                        manual_added_count=0, positions=positions)
    assert report["status"] == "failed"
    assert len(report["unbonded_disulfide_geometry"]) == 1
    without_positions = _validate_final_disulfides(topology=topology, system=system,
                                                   disulfide_bonds=[], manual_added_count=0)
    assert without_positions["unbonded_disulfide_geometry"] == []


def test_reconciliation_keeps_a_form_bond_false_pair_reduced(tmp_path):
    from mdclaw.structure.disulfide import _reconcile_cyx_cys_in_pdb

    merged = _two_cys_pdb(tmp_path / "merged.pdb")
    report = _reconcile_cyx_cys_in_pdb(str(merged), [_bond(form_bond=False)])
    assert report["renamed_to_cyx"] == 0
    assert "CYX" not in merged.read_text()


# --------------------------------------------------------------------------
# Exclusions, range-only selections and the associated-ligand refusal
# --------------------------------------------------------------------------

_PROTEIN_ION_GLYCAN_PDB = textwrap.dedent("""\
ATOM      1  N   ASN A   1       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ASN A   1       1.400   0.000   0.000  1.00  0.00           C
ATOM      3  C   ASN A   1       2.000   1.300   0.000  1.00  0.00           C
ATOM      4  O   ASN A   1       1.500   2.300   0.000  1.00  0.00           O
ATOM      5  CB  ASN A   1       1.900  -0.800   1.200  1.00  0.00           C
ATOM      6  CG  ASN A   1       3.300  -0.800   1.200  1.00  0.00           C
ATOM      7  OD1 ASN A   1       3.900  -1.800   1.200  1.00  0.00           O
ATOM      8  ND2 ASN A   1       3.900   0.300   1.200  1.00  0.00           N
TER
HETATM    9  C1  NAG B   2       5.200   0.300   1.200  1.00  0.00           C
HETATM   10  O1  NAG B   2       5.800   1.300   1.200  1.00  0.00           O
HETATM   11  C2  NAG B   2       5.700  -1.000   1.200  1.00  0.00           C
HETATM   12  O5  NAG B   2       4.000   0.300   1.200  1.00  0.00           O
TER
HETATM   13 ZN    ZN C 301      10.000  10.000  10.000  1.00  0.00          ZN
HETATM   14  O   HOH C 401      14.000  10.000  10.000  1.00  0.00           O
END
""")


def _protein_ion_glycan(tmp_path):
    path = tmp_path / "pig.pdb"
    path.write_text(_PROTEIN_ION_GLYCAN_PDB)
    return path


def test_exclusions_that_name_nothing_are_refused_with_the_ids(tmp_path):
    """069_soluble_1aol (glm v5): --exclude-ligand-ids NAG ZN HOH matched nothing."""
    out = split_molecules(str(_protein_ion_glycan(tmp_path)), output_dir=str(tmp_path / "s"),
                          exclude_ligand_ids=["NAG", "ZN", "HOH"])
    assert out["success"] is False
    assert out["code"] == "exclude_ligand_id_unmatched"
    assert "--exclude-ligand-ids B:NAG:2 C:ZN:301" in out["next_action"]
    assert any("crystal water is always removed" in h for h in out["hints"])


def test_ion_and_glycan_ids_are_excluded(tmp_path):
    out = split_molecules(str(_protein_ion_glycan(tmp_path)), output_dir=str(tmp_path / "s"),
                          exclude_ligand_ids=["C:ZN:301,B:NAG:2"])
    assert out["success"], out["errors"]
    assert out["ion_files"] == [] and out["glycan_files"] == []
    assert len(out["protein_files"]) == 1


def test_excluded_glycan_is_not_pulled_back_by_the_linkage_rule(tmp_path):
    path = _protein_ion_glycan(tmp_path)
    inspection = _inspect_molecules_impl(str(path))
    protein = next(c for c in inspection["chains"] if c["chain_type"] == "protein")
    glycan = next(c for c in inspection["chains"] if c["chain_type"] == "glycan")
    linkages = [{"protein": {"chain": "A", "resnum": 1, "resname": "ASN"},
                 "glycan": {"chain": "B", "resnum": 2, "resname": "NAG"}}]
    added, links, adjustment = pc._expand_covalently_linked_glycan_selection(
        select_chains=[protein["chain_id"]], include_types=None, inspection=inspection,
        linkages=linkages, excluded_labels={glycan["chain_id"]})
    assert adjustment is None and links == []
    _added, links, adjustment = pc._expand_covalently_linked_glycan_selection(
        select_chains=[protein["chain_id"]], include_types=None, inspection=inspection,
        linkages=linkages)
    assert adjustment["added_chain_ids"] == [glycan["chain_id"]] and len(links) == 1


_CIF_PROTEIN_A_LIGAND_C_AUTH_A = "data_TEST\n#\n" + _ATOM_SITE_HEADER + """\
ATOM   1 N N   . ALA A 1 1   ? 0.0 0.0 0.0 1.0 0.0 ? 1   ALA A N   1
ATOM   2 C CA  . ALA A 1 1   ? 1.5 0.0 0.0 1.0 0.0 ? 1   ALA A CA  1
ATOM   3 C C   . ALA A 1 1   ? 2.0 1.5 0.0 1.0 0.0 ? 1   ALA A C   1
ATOM   4 O O   . ALA A 1 1   ? 1.5 2.5 0.0 1.0 0.0 ? 1   ALA A O   1
HETATM 5 S SD  . SAH C 2 .   ? 5.0 0.0 0.0 1.0 0.0 ? 287 SAH A SD  1
HETATM 6 C CE  . SAH C 2 .   ? 6.5 0.0 0.0 1.0 0.0 ? 287 SAH A CE  1
#
"""


def test_associated_ligand_refusal_spells_both_commands(tmp_path):
    path = tmp_path / "sah.cif"
    path.write_text(_CIF_PROTEIN_A_LIGAND_C_AUTH_A)
    out = split_molecules(str(path), output_dir=str(tmp_path / "s"), select_chains=["A"],
                          include_types=["protein", "ligand"])
    assert out["code"] == "associated_ligands_require_selection"
    assert "--include-ligand-ids A:SAH:287" in out["next_action"]
    assert "--exclude-ligand-ids A:SAH:287" in out["next_action"]
    assert "associated_ligand_candidates" not in out["context"]
    excluded = split_molecules(str(path), output_dir=str(tmp_path / "e"), select_chains=["A"],
                               include_types=["protein", "ligand"],
                               exclude_ligand_ids=["A:SAH:287"])
    assert excluded["success"], excluded["errors"]
    assert excluded["ligand_files"] == []


# --------------------------------------------------------------------------
# Terminal caps on a native end
# --------------------------------------------------------------------------

def _peptide_cif(tmp_path):
    rows, serial = [], 1
    for seq, (number, resname) in enumerate(((268, "ILE"), (269, "ASN"), (270, "PHE")), start=1):
        for name, element, dx in (("N", "N", 0.0), ("CA", "C", 1.4), ("C", "C", 2.4), ("O", "O", 3.0)):
            rows.append(f"ATOM {serial} {element} {name} . {resname} A 1 {seq} ? "
                        f"{(seq - 1) * 3.8 + dx:.3f} 0.000 0.000 1.00 0.00 ? {number} {resname} A {name} 1")
            serial += 1
    scheme = "\n".join(
        f"A 1 {seq} {resname} {seq} {number} {number} {resname} {resname} A . n"
        for seq, (number, resname) in enumerate(((268, "ILE"), (269, "ASN"), (270, "PHE")), start=1))
    text = ("data_PEPTIDE\n" + _ATOM_SITE_HEADER + "\n".join(rows) + "\n#\nloop_\n"
            + "\n".join(f"_pdbx_poly_seq_scheme.{key}" for key in (
                "asym_id", "entity_id", "seq_id", "mon_id", "ndb_seq_num", "pdb_seq_num",
                "auth_seq_num", "pdb_mon_id", "auth_mon_id", "pdb_strand_id", "pdb_ins_code",
                "hetero")) + "\n" + scheme + "\n#\n")
    path = tmp_path / "peptide.cif"
    path.write_text(text)
    return path


def test_caps_on_the_entity_ends_are_refused_and_cut_ends_allowed(tmp_path):
    """1AA3 268-330 (campaign v5) is the whole entity; capping it changes the molecule."""
    from mdclaw.structure.terminal_caps import native_terminus_cap_next_action, native_terminus_caps

    cif = _peptide_cif(tmp_path)
    whole = split_molecules(str(cif), output_dir=str(tmp_path / "w"), select_chains=["A"])
    native = native_terminus_caps(cif, whole, n_cap="ACE", c_cap="NME")
    assert sorted(item["terminus"] for item in native) == ["C", "N"]
    assert "Drop --cap-termini" in native_terminus_cap_next_action(native, n_cap="ACE", c_cap="NME")
    cut = split_molecules(str(cif), output_dir=str(tmp_path / "c"), select_chains=["A"],
                          residue_ranges=["A:269-270"])
    native = native_terminus_caps(cif, cut, n_cap="ACE", c_cap="NME")
    assert [item["terminus"] for item in native] == ["C"]
    assert "--n-terminal-cap ACE" in native_terminus_cap_next_action(native, n_cap="ACE", c_cap="NME")
    assert native_terminus_caps(cif, cut, n_cap="ACE", c_cap=None) == []


def test_prepare_complex_refuses_native_caps_before_running(tmp_path):
    result = pc.prepare_complex(structure_file=str(_peptide_cif(tmp_path)),
                                output_dir=str(tmp_path / "out"), select_chains=["A"],
                                include_types=["protein"], cap_termini=True)
    assert result["code"] == "terminal_cap_on_native_terminus"
    assert "proteins" not in result or not result["proteins"]


# --------------------------------------------------------------------------
# Ligand chemistry decided before the node runs
# --------------------------------------------------------------------------

def _ligand_pdb(tmp_path, resname):
    path = tmp_path / f"{resname.lower()}.pdb"
    path.write_text(textwrap.dedent(f"""\
        ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N
        ATOM      2  CA  ALA A   1       1.500   0.000   0.000  1.00  0.00           C
        ATOM      3  C   ALA A   1       2.500   1.200   0.000  1.00  0.00           C
        ATOM      4  O   ALA A   1       2.000   2.300   0.000  1.00  0.00           O
        TER
        HETATM    5  C1  {resname} A 202       5.000   5.000   5.000  1.00  0.00           C
        HETATM    6  C2  {resname} A 202       6.500   5.000   5.000  1.00  0.00           C
        END
        """))
    return path


@pytest.fixture
def glutarimide(monkeypatch):
    monkeypatch.setattr(lc, "_fetch_smiles_from_ccd", lambda name, timeout=10: "CC[C@@]1(C)CC(=O)NC1=O")
    monkeypatch.setattr(lc, "_protonate_smiles_dimorphite", lambda smiles, ph: [
        ("CC[C@@]1(C)CC(=O)[N-]C1=O", -1), ("CC[C@@]1(C)CC(=O)NC1=O", 0)])


def test_ligand_with_states_of_different_charge_is_refused_with_the_flag(tmp_path, glutarimide):
    """5OH3's 9V2 (campaign v5) came back -1 when the task said 0."""
    result = pc.prepare_complex(structure_file=str(_ligand_pdb(tmp_path, "9V2")),
                                output_dir=str(tmp_path / "out"), process_proteins=False,
                                include_types=["protein", "ligand"])
    assert result["code"] == "ligand_protonation_ambiguous"
    assert """--ligand-net-charges '{"9V2": 0}'""" in result["next_action"]
    assert sorted(c["charge"] for c in result["context"]["candidates"]) == [-1, 0]


def test_stated_net_charge_picks_the_state(tmp_path, glutarimide, monkeypatch):
    seen = {}

    def fake_clean_ligand(**kwargs):
        seen.update(kwargs)
        return {"success": False, "errors": ["stopped by the test"], "warnings": []}

    monkeypatch.setattr(pc, "clean_ligand", fake_clean_ligand)
    result = pc.prepare_complex(structure_file=str(_ligand_pdb(tmp_path, "9V2")),
                                output_dir=str(tmp_path / "out"), process_proteins=False,
                                include_types=["protein", "ligand"],
                                ligand_net_charges={"9V2": 0})
    assert result.get("code") != "ligand_protonation_ambiguous"
    assert seen["expected_net_charge"] == 0


def test_net_charges_for_an_unselected_ligand_are_refused(tmp_path, glutarimide):
    result = pc.prepare_complex(structure_file=str(_ligand_pdb(tmp_path, "9V2")),
                                output_dir=str(tmp_path / "out"), process_proteins=False,
                                include_types=["protein", "ligand"],
                                ligand_net_charges={"9V2": 0, "XYZ": 1})
    assert result["code"] == "invalid_ligand_net_charges"
    bad = pc.prepare_complex(structure_file=str(_ligand_pdb(tmp_path, "9V2")),
                             output_dir=str(tmp_path / "out2"), ligand_net_charges={"9V2": "zero"})
    assert bad["code"] == "invalid_ligand_net_charges"


def test_smiles_that_rewires_the_ccd_ligand_is_refused(tmp_path, monkeypatch):
    """1G74 OLA (campaign v5): a stearate SMILES built oleate without its double bond."""
    pytest.importorskip("rdkit")
    oleate = "CCCCCCCC/C=C\\CCCCCCCC(=O)O"
    monkeypatch.setattr(lc, "_fetch_smiles_from_ccd", lambda name, timeout=10: oleate)
    result = pc.prepare_complex(structure_file=str(_ligand_pdb(tmp_path, "OLA")),
                                output_dir=str(tmp_path / "out"), process_proteins=False,
                                include_types=["protein", "ligand"],
                                ligand_smiles={"OLA": "CCCCCCCCCCCCCCCCCC(=O)[O-]"})
    assert result["code"] == "ligand_smiles_ccd_mismatch"
    assert oleate in result["next_action"]
    # the same molecule in another protonation state passes the check
    assert lc.smiles_ccd_mismatch("OLA", "CCCCCCCC/C=C\\CCCCCCCC(=O)[O-]") is None
    # a generic residue name is not a CCD identity
    assert lc.smiles_ccd_mismatch("LIG", "CCCCCCCCCCCCCCCCCC(=O)[O-]") is None


def test_ligand_refusal_leaves_the_prep_node_pending(tmp_path, glutarimide):
    from mdclaw.research.source_node import register_local_structure

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    update_job_params(str(job_dir), {"solvent_regime": "explicit"})
    source = create_node(str(job_dir), "source")["node_id"]
    registered = register_local_structure(str(_ligand_pdb(tmp_path, "9V2")), str(job_dir), source)
    assert registered["success"], registered
    prep = create_node(str(job_dir), "prep")["node_id"]
    result = pc.prepare_complex(job_dir=str(job_dir), node_id=prep, process_proteins=False,
                                include_types=["protein", "ligand"])
    assert result["code"] == "ligand_protonation_ambiguous"
    assert read_node(str(job_dir), prep)["status"] == "pending"
