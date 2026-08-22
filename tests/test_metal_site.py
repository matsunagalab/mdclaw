"""A cysteine on a metal is a thiolate, and nothing used to say so.

Protonation runs on the protein alone: the split hands pdb2pqr a file with no
ion in it, so propka decides every cysteine's state without knowing a metal is
there.  Measured on RCSB 6W9C / 6WRH / 4OW0 (2026-08-22) that leaves a
four-cysteine structural zinc with one thiolate and three neutral thiols, and
across 1 ns the three neutral sulfurs move to 5-12 A while the thiolate holds
at 2.03 +/- 0.06 A.

What is deliberately *not* assigned matters as much as what is.  A histidine
ligand is reported and left to propka, because which nitrogen coordinates
decides the tautomer and a distance does not say.
"""

from __future__ import annotations

from pathlib import Path

import pytest

gemmi = pytest.importorskip("gemmi")

from mdclaw.structure.metal_site import (  # noqa: E402
    detect_metal_sites,
    protonation_states_for_metal_sites,
)

DATA = Path(__file__).parent / "data"
METAL_SITE = DATA / "metal_site_cys4.pdb"
BPTI = DATA / "disulfide_bpti.pdb"


def assigned(sites):
    return sorted((s["chain"], s["resnum"], s["state"])
                  for s in protonation_states_for_metal_sites(sites))


def test_every_cysteine_on_the_zinc_becomes_a_thiolate():
    sites = detect_metal_sites(METAL_SITE)
    assert assigned(sites) == [("C", "189", "CYM"), ("C", "192", "CYM"),
                               ("C", "224", "CYM")]


def test_a_distant_metal_claims_nothing():
    """The fixture carries a second zinc 41 A away; it must contribute no site."""
    sites = detect_metal_sites(METAL_SITE)
    assert len(sites) == 1, [s["label"] for s in sites]
    assert sites[0]["motif"] == "ZN-Cys3"


def test_a_protein_with_no_metal_yields_nothing():
    assert detect_metal_sites(BPTI) == []
    assert protonation_states_for_metal_sites(detect_metal_sites(BPTI)) == []


def test_chain_selection_is_honoured():
    """Only the selected copy's residues may be assigned."""
    assert detect_metal_sites(METAL_SITE, select_chains=["A"]) == []
    assert assigned(detect_metal_sites(METAL_SITE, select_chains=["C"])) == [
        ("C", "189", "CYM"), ("C", "192", "CYM"), ("C", "224", "CYM")]


def test_a_histidine_ligand_is_reported_and_not_assigned(tmp_path):
    """Which nitrogen coordinates decides the tautomer; a distance does not say."""
    lines = [
        "ATOM      1  ND1 HIS A   1      10.000  10.000  10.000  1.00  0.00           N\n",
        "ATOM      2  NE2 HIS A   1      12.100  10.000  10.000  1.00  0.00           N\n",
        "ATOM      3  SG  CYS A   2       8.000   9.000  10.000  1.00  0.00           S\n",
        "HETATM    4 ZN    ZN A 101       8.200  10.400  10.000  1.00  0.00          ZN\n",
        "END\n",
    ]
    path = tmp_path / "znhis.pdb"
    path.write_text("".join(lines))
    sites = detect_metal_sites(path)
    assert len(sites) == 1
    site = sites[0]
    assert assigned(sites) == [("A", "2", "CYM")], "the cysteine is assigned"
    assert [item["resname"] for item in site["deferred_ligands"]] == ["HIS"], \
        "the histidine is reported, not assigned"
    assert "other" in site["motif"], site["motif"]


# --- what the guard must NOT do ---------------------------------------------
# The assignment used to fire for any metal with any single cysteine inside
# 3.5 A, and CYX -- a cysteine already holding a disulfide -- was eligible.

def write(tmp_path, name, rows):
    path = tmp_path / name
    path.write_text("".join(rows) + "END\n")
    return path


def sg(serial, resname, resnum, x, y, z, chain="A"):
    return (f"ATOM  {serial:5d}  SG  {resname} {chain}{resnum:4d}    "
            f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           S\n")


def zinc(serial, x, y, z, chain="A", resnum=901):
    return (f"HETATM{serial:5d} ZN    ZN {chain}{resnum:4d}    "
            f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          ZN\n")


def test_one_loose_cysteine_is_not_a_metal_site(tmp_path):
    """A single sulfur at 3.4 A is a contact, not a site, and is left alone."""
    path = write(tmp_path, "loose.pdb", [sg(1, "CYS", 10, 0, 0, 0), zinc(2, 3.4, 0, 0)])
    sites = detect_metal_sites(path)
    assert sites and sites[0]["established"] is False
    assert protonation_states_for_metal_sites(sites) == []


def test_two_ligands_with_one_at_coordination_distance_is_a_site(tmp_path):
    """The shape a real site has: one contact at 2.3 A, the distorted one at 3.2."""
    path = write(tmp_path, "site.pdb", [sg(1, "CYS", 10, 0, 0, 0),
                                        sg(2, "CYS", 20, 5.5, 0, 0),
                                        zinc(3, 2.3, 0, 0)])
    sites = detect_metal_sites(path)
    assert sites[0]["established"] is True
    assert assigned(sites) == [("A", "10", "CYM"), ("A", "20", "CYM")]


def test_a_disulfide_cysteine_near_a_metal_is_left_alone(tmp_path):
    """CYX already holds a bond; making it a thiolate would break it silently."""
    path = write(tmp_path, "cyx.pdb", [sg(1, "CYX", 10, 0, 0, 0),
                                       sg(2, "CYS", 20, 4.6, 0, 0),
                                       zinc(3, 2.3, 0, 0)])
    sites = detect_metal_sites(path)
    assert sites[0]["established"] is True
    assert assigned(sites) == [("A", "20", "CYM")], "only the free cysteine"


# --- a variant name must survive a round trip through HPacker ----------------
# Preparation now emits CYM at a metal site, and mutation runs on preparation's
# output.  HPacker's pipeline loses a residue whose name is a variant: measured
# on a prepared 6WRH chain, mutating S111C failed with "Protein residue missing
# after HPacker hydrogen rebuild: A:189 CYS", and renaming that file's CYM to
# CYS -- nothing else changed -- made the same mutation succeed.

def test_variants_are_canonicalised_for_hpacker_and_restored(tmp_path):
    from mdclaw.structure.mutation import (
        _canonicalise_variants_for_hpacker,
        _restore_variants,
    )

    rows = [
        "ATOM      1  N   CYM A 189      10.000  10.000  10.000  1.00  0.00           N\n",
        "ATOM      2  SG  CYM A 189      11.000  10.000  10.000  1.00  0.00           S\n",
        "ATOM      3  N   HIP A 269      20.000  10.000  10.000  1.00  0.00           N\n",
        "ATOM      4  N   ALA A 270      24.000  10.000  10.000  1.00  0.00           N\n",
        "HETATM    5 ZN    ZN A 901      12.000  11.000  10.000  1.00  0.00          ZN\n",
        "END\n",
    ]
    source = tmp_path / "in.pdb"
    source.write_text("".join(rows))
    packed = tmp_path / "packed.pdb"

    original = _canonicalise_variants_for_hpacker(source, packed)
    names = [line[17:20].strip() for line in packed.read_text().splitlines()
             if line.startswith(("ATOM", "HETATM"))]
    assert names == ["CYS", "CYS", "HIP", "ALA", "ZN"], \
        "the cysteine goes to HPacker as CYS; the histidine keeps its tautomer"
    assert list(original.values()) == ["CYM"], \
        "one entry, for the one residue whose name was changed"

    _restore_variants(packed, original)
    names = [line[17:20].strip() for line in packed.read_text().splitlines()
             if line.startswith(("ATOM", "HETATM"))]
    assert names == ["CYM", "CYM", "HIP", "ALA", "ZN"], "and come back as themselves"


def test_a_rebuilt_hydrogen_does_not_come_back_with_the_variant(tmp_path):
    """A CYM carrying HG is not a thiolate.

    HPacker's pipeline rebuilds hydrogens on the residue it was handed, so a
    CYM sent in as CYS returns with the HG that completes a thiol.  Restoring
    the name alone produced an 11-atom CYM, which the topology builder refused
    with "expected 4, restored 4, validated 0".
    """
    from mdclaw.structure.mutation import _restore_variants

    path = tmp_path / "rebuilt.pdb"
    path.write_text(
        "ATOM      1  SG  CYS A 189      11.000  10.000  10.000  1.00  0.00           S\n"
        "ATOM      2  HG  CYS A 189      11.800  10.400  10.000  1.00  0.00           H\n"
        "END\n")
    removed = _restore_variants(path, {("A", " 189", " "): "CYM"})
    kept = [line[12:16].strip() for line in path.read_text().splitlines()
            if line.startswith("ATOM")]
    assert kept == ["SG"], "the hydrogen a thiolate cannot hold is taken back"
    assert removed == {"CYM": 1}, "and the removal is reported"


def test_histidine_tautomers_are_not_canonicalised(tmp_path):
    """Canonicalising them loses which nitrogen carries the proton.

    Measured on a real chain: sending HID and HIE through under the name HIS
    brought some of them back with the other tautomer's hydrogen, and stripping
    by name then left them at 16 atoms where they went in at 17.  They pass
    through HPacker intact, so they are left alone.
    """
    from mdclaw.structure.mutation import _canonicalise_variants_for_hpacker

    source = tmp_path / "his.pdb"
    source.write_text(
        "ATOM      1  ND1 HID A  70      10.000  10.000  10.000  1.00  0.00           N\n"
        "ATOM      2  NE2 HIE A  86      20.000  10.000  10.000  1.00  0.00           N\n"
        "END\n")
    target = tmp_path / "out.pdb"
    assert _canonicalise_variants_for_hpacker(source, target) == {}
    assert source.read_text() == target.read_text()


def test_restoring_nothing_leaves_the_file_alone(tmp_path):
    path = tmp_path / "plain.pdb"
    text = "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\nEND\n"
    path.write_text(text)
    from mdclaw.structure.mutation import _restore_variants
    _restore_variants(path, {})
    assert path.read_text() == text


def test_a_conect_to_a_removed_hydrogen_goes_with_it(tmp_path):
    """The packer carries CONECT across, so a record must not outlive its atom."""
    from mdclaw.structure.mutation import _restore_variants

    path = tmp_path / "conect.pdb"
    path.write_text(
        "ATOM      1  SG  CYS A 189      11.000  10.000  10.000  1.00  0.00           S\n"
        "ATOM      2  HG  CYS A 189      11.800  10.400  10.000  1.00  0.00           H\n"
        "ATOM      3  SG  CYS A 224      14.000  10.000  10.000  1.00  0.00           S\n"
        "CONECT    1    2\n"
        "CONECT    1    3\n"
        "END\n")
    _restore_variants(path, {("A", " 189", " "): "CYM"})
    text = path.read_text()
    assert "CONECT    1    2" not in text, "the record naming the removed HG is gone"
    assert "CONECT    1    3" in text, "and the one that still has both atoms stays"


def test_only_subset_variants_may_be_canonicalised():
    """ASH and GLH add a hydrogen; no amount of removing atoms puts one back."""
    from mdclaw.structure.mutation import _HPACKER_UNSAFE_VARIANTS

    assert set(_HPACKER_UNSAFE_VARIANTS) == {"CYM", "CYX"}
    for name in ("ASH", "GLH", "HIP", "HID", "HIE"):
        assert name not in _HPACKER_UNSAFE_VARIANTS, \
            f"{name} cannot be restored by removing atoms"
