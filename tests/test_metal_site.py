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
