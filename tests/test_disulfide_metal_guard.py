"""Distance alone cannot tell a disulfide from a metal site.

Measured on RCSB 6W9C (2026-08-22), the 3.0 A candidate window produced four
disulfide candidates and every one of them was wrong:

    A192-A224  2.94 A   both ligate A:ZN501
    C192-C224  3.00 A   both ligate C:ZN402
    A270-B270  2.84 A   both ligate C:ZN401, a second zinc shared by three chains
    A270-C270  3.00 A   same site

The C192-C224 pair reached the built system as a real 0.2038 nm bond term and
the two sulfurs closed from 3.00 to 2.04 A during production, which destroys
the zinc site the deposit exists to show.

These tests pin that down and, just as importantly, pin down that the guard
does not cost real disulfides: BPTI's three are 2.01-2.03 A and must survive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

gemmi = pytest.importorskip("gemmi")

from mdclaw.structure.disulfide import (  # noqa: E402
    METAL_SULFUR_ANGSTROM,
    _detect_disulfide_candidates,
    _enforce_one_disulfide_per_cysteine,
    _merge_disulfide_pairs,
    _parse_ssbond_records,
)

DATA = Path(__file__).parent / "data"
METAL_SITE = DATA / "metal_site_cys4.pdb"
BPTI = DATA / "disulfide_bpti.pdb"


def ends(pairs):
    return sorted(
        tuple(sorted((f"{p['cys1']['chain']}{p['cys1']['resnum']}",
                      f"{p['cys2']['chain']}{p['cys2']['resnum']}")))
        for p in pairs
    )


def test_two_ligands_of_one_metal_are_not_a_disulfide():
    """The regression: 6W9C chain C, SG(192)-SG(224) at 3.00 A with a zinc on both."""
    assert _detect_disulfide_candidates(METAL_SITE) == []


def test_the_metal_site_fixture_still_contains_the_close_pair():
    """Guard against the fixture silently losing the geometry it exists to carry."""
    structure = gemmi.read_pdb(str(METAL_SITE))
    sg = {residue.seqid.num: residue.find_atom("SG", "*").pos
          for chain in structure[0] for residue in chain if residue.name == "CYS"}
    assert round(sg[192].dist(sg[224]), 2) == 3.00, "the pair must stay inside the window"
    zinc = [atom.pos for chain in structure[0] for residue in chain
            for atom in residue if residue.name == "ZN"]
    bridging = [z for z in zinc
                if z.dist(sg[192]) <= METAL_SULFUR_ANGSTROM
                and z.dist(sg[224]) <= METAL_SULFUR_ANGSTROM]
    assert len(bridging) == 1, "exactly one of the two zincs coordinates both sulfurs"


def test_real_disulfides_survive_the_guard():
    """The positive control. BPTI's three bonds are 2.01-2.03 A and carry SSBOND."""
    candidates = _detect_disulfide_candidates(BPTI)
    assert ends(candidates) == [("A14", "A38"), ("A30", "A51"), ("A5", "A55")]
    assert all(c["confidence"] == "high" for c in candidates)
    merged = _merge_disulfide_pairs(_parse_ssbond_records(BPTI), candidates)
    assert ends(merged) == [("A14", "A38"), ("A30", "A51"), ("A5", "A55")]


def test_a_distant_metal_does_not_suppress_anything():
    """metal_site_cys4.pdb carries a second zinc 41-45 A away; it must not count."""
    structure = gemmi.read_pdb(str(METAL_SITE))
    sulfurs = [residue.find_atom("SG", "*").pos
               for chain in structure[0] for residue in chain
               if residue.name == "CYS" and residue.find_atom("SG", "*")]
    zincs = [atom.pos for chain in structure[0] for residue in chain
             if residue.name == "ZN" for atom in residue]
    assert len(zincs) == 2 and len(sulfurs) == 3
    closest = sorted(min(z.dist(s) for s in sulfurs) for z in zincs)
    assert closest[0] < METAL_SULFUR_ANGSTROM, "one zinc coordinates the site"
    assert closest[1] > 40.0, "the other is 41 A away and must be ignored"


def test_a_cysteine_cannot_hold_two_disulfides():
    """6W9C offers A270 a bond to both B270 and C270; only the shorter survives."""
    def pair(c1, n1, c2, n2, distance, source="distance"):
        return {"cys1": {"chain": c1, "resnum": n1, "resname": "CYS"},
                "cys2": {"chain": c2, "resnum": n2, "resname": "CYS"},
                "distance_angstrom": distance, "confidence": "medium",
                "recommendation": "review", "source": source}

    kept = _enforce_one_disulfide_per_cysteine(
        [pair("A", 270, "B", 270, 2.84), pair("A", 270, "C", 270, 3.00)]
    )
    assert ends(kept) == [("A270", "B270")]


def test_a_recorded_bond_outranks_a_shorter_distance_candidate():
    """A deposit's own SSBOND wins the residue even when a closer pair competes."""
    recorded = {"cys1": {"chain": "A", "resnum": 5, "resname": "CYS"},
                "cys2": {"chain": "A", "resnum": 55, "resname": "CYS"},
                "distance_angstrom": 2.20, "confidence": "high",
                "recommendation": "form_bond", "source": "pdb_ssbond"}
    competing = {"cys1": {"chain": "A", "resnum": 5, "resname": "CYS"},
                 "cys2": {"chain": "A", "resnum": 30, "resname": "CYS"},
                 "distance_angstrom": 2.05, "confidence": "high",
                 "recommendation": "form_bond", "source": "distance"}
    assert ends(_enforce_one_disulfide_per_cysteine([recorded, competing])) == [("A5", "A55")]
