"""Regression for Fix C: _reconcile_cyx_cys_in_pdb strips HG from every CYX.

tleap aborts with ``FATAL: Atom .R<CYX N>.A<HG> does not have a type`` when a
SS-bonded cysteine still carries its thiol hydrogen. This test exercises both
paths: an already-CYX residue from pdb2pqr with lingering HG, and a CYS
residue promoted to CYX by the reconciliation pass.
"""
from __future__ import annotations

from math import dist
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mdclaw"))


# Fixed-column PDB records. Residue 23 is ALREADY CYX (with a lingering HG),
# residue 97 is CYS that should be promoted to CYX by the reconciliation list.
# Atom columns: 12-16 name, 17-20 resname, 21 chain, 22-26 resnum.
_MIXED_PDB = """\
ATOM      1  N   CYX A  23       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  CYX A  23       1.500   0.000   0.000  1.00  0.00           C
ATOM      3  C   CYX A  23       2.000   1.500   0.000  1.00  0.00           C
ATOM      4  O   CYX A  23       1.500   2.500   0.000  1.00  0.00           O
ATOM      5  CB  CYX A  23       2.000  -1.000   1.000  1.00  0.00           C
ATOM      6  SG  CYX A  23       3.500  -1.500   1.500  1.00  0.00           S
ATOM      7  HG  CYX A  23       4.000  -0.500   1.700  1.00  0.00           H
ATOM      8  N   CYS A  97      10.000   0.000   0.000  1.00  0.00           N
ATOM      9  CA  CYS A  97      11.500   0.000   0.000  1.00  0.00           C
ATOM     10  C   CYS A  97      12.000   1.500   0.000  1.00  0.00           C
ATOM     11  O   CYS A  97      11.500   2.500   0.000  1.00  0.00           O
ATOM     12  CB  CYS A  97      12.000  -1.000   1.000  1.00  0.00           C
ATOM     13  SG  CYS A  97      13.500  -1.500   1.500  1.00  0.00           S
ATOM     14  HG  CYS A  97      14.000  -0.500   1.700  1.00  0.00           H
END
"""


@pytest.fixture
def mixed_pdb(tmp_path: Path) -> str:
    p = tmp_path / "mixed.pdb"
    p.write_text(_MIXED_PDB, encoding="utf-8")
    return str(p)


def test_reconcile_strips_hg_from_preexisting_and_promoted_cyx(mixed_pdb):
    from mdclaw.structure.disulfide import _reconcile_cyx_cys_in_pdb

    # Declare both residues as SS-bonded → both should end up CYX, no HG.
    bonds = [{
        "cys1": {"chain": "A", "resnum": 23},
        "cys2": {"chain": "A", "resnum": 97},
    }]
    result = _reconcile_cyx_cys_in_pdb(mixed_pdb, bonds)

    # Counters are per atom-line (the function scans line-by-line). Residue 97
    # has 7 atom lines (N, CA, C, O, CB, SG, HG) — each flips CYS→CYX, so the
    # count is 7. HG strip happens at most once per residue, so 2 total.
    assert result["renamed_to_cyx"] == 7
    assert result["renamed_to_cys"] == 0
    assert result["stripped_hg_from_cyx"] == 2  # one HG per residue

    content = Path(mixed_pdb).read_text()
    # Both residues must be CYX now.
    assert "CYS A" not in content, "no CYS A residue should remain"
    assert content.count("CYX A") >= 12   # 6+6 heavy atoms per residue
    # Absolutely no HG atom left on any CYX line.
    for line in content.splitlines():
        if line.startswith(("ATOM", "HETATM")):
            resname = line[17:20].strip()
            atom_name = line[12:16].strip()
            if resname == "CYX":
                assert atom_name != "HG", f"CYX still carries HG: {line!r}"


def test_reconcile_demotes_cyx_to_cys_and_keeps_hg(tmp_path):
    """The reverse path: a CYX residue not in the disulfide list is demoted
    to CYS. CYS legitimately carries HG, so the HG line must NOT be stripped."""
    from mdclaw.structure.disulfide import _reconcile_cyx_cys_in_pdb

    pdb = """\
ATOM      1  N   CYX A  23       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  SG  CYX A  23       3.500  -1.500   1.500  1.00  0.00           S
ATOM      3  HG  CYX A  23       4.000  -0.500   1.700  1.00  0.00           H
END
"""
    p = tmp_path / "unpaired.pdb"
    p.write_text(pdb, encoding="utf-8")

    # Empty disulfide list → the CYX at residue 23 is demoted to CYS.
    # Counter is per atom-line: 3 atoms (N, SG, HG) each flip CYX→CYS.
    result = _reconcile_cyx_cys_in_pdb(str(p), [])

    assert result["renamed_to_cys"] == 3
    assert result["renamed_to_cyx"] == 0
    assert result["stripped_hg_from_cyx"] == 0, (
        "CYS keeps its HG; strip applies only to final CYX residues"
    )
    assert result["rebuilt_hg_on_demoted_cys"] == 0
    content = p.read_text()
    assert "CYS A" in content
    assert "HG  CYS A  23" in content or " HG CYS A  23" in content  # HG survives the demotion


def test_reconcile_rebuilds_hg_on_two_close_cyx_demoted_to_cys(tmp_path):
    """An explicit empty pair list must make two 2.03 A thiols, not CCYX."""
    from mdclaw.structure.disulfide import _reconcile_cyx_cys_in_pdb

    pdb = """\
ATOM      1  N   CYX A  10      11.240  15.750  30.422  1.00  0.00           N
ATOM      2  CA  CYX A  10      10.387  16.143  31.524  1.00  0.00           C
ATOM      3  C   CYX A  10      10.054  17.651  31.423  1.00  0.00           C
ATOM      4  O   CYX A  10      10.923  18.482  31.198  1.00  0.00           O
ATOM      5  CB  CYX A  10      11.130  16.033  32.926  1.00  0.00           C
ATOM      6  SG  CYX A  10      11.211  14.227  33.306  1.00  0.00           S
ATOM      7  N   CYX A  20       6.772  14.876  34.904  1.00  0.00           N
ATOM      8  CA  CYX A  20       8.035  14.842  35.681  1.00  0.00           C
ATOM      9  C   CYX A  20       8.827  16.109  35.521  1.00  0.00           C
ATOM     10  O   CYX A  20       8.332  17.027  34.791  1.00  0.00           O
ATOM     11  CB  CYX A  20       8.837  13.576  35.206  1.00  0.00           C
ATOM     12  SG  CYX A  20       9.301  13.545  33.474  1.00  0.00           S
TER
END
"""
    path = tmp_path / "close_cyx.pdb"
    path.write_text(pdb)

    result = _reconcile_cyx_cys_in_pdb(str(path), [])

    atoms: dict[str, set[str]] = {}
    sg_positions = []
    for line in path.read_text().splitlines():
        if not line.startswith("ATOM"):
            continue
        resnum = line[22:26].strip()
        atoms.setdefault(resnum, set()).add(line[12:16].strip())
        if line[12:16].strip() == "SG":
            sg_positions.append(tuple(float(line[start:start + 8])
                                      for start in (30, 38, 46)))
        assert line[17:20].strip() == "CYS"

    assert result["rebuilt_hg_on_demoted_cys"] == 2
    assert atoms["10"] >= {"SG", "HG"}
    assert atoms["20"] >= {"SG", "HG"}
    assert dist(*sg_positions) == pytest.approx(2.03, abs=0.01)


def test_reconcile_noop_on_consistent_input(tmp_path):
    """No CYX, no CYS-in-target — function should be a no-op."""
    from mdclaw.structure.disulfide import _reconcile_cyx_cys_in_pdb

    pdb = """\
ATOM      1  N   ALA A  10       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ALA A  10       1.500   0.000   0.000  1.00  0.00           C
END
"""
    p = tmp_path / "ala.pdb"
    p.write_text(pdb, encoding="utf-8")
    before = p.read_text()
    result = _reconcile_cyx_cys_in_pdb(str(p), [])
    assert result == {
        "renamed_to_cys": 0,
        "renamed_to_cyx": 0,
        "stripped_hg_from_cyx": 0,
        "rebuilt_hg_on_demoted_cys": 0,
        # Reported so the caller can fail on it: a declared endpoint that could
        # not be identified must not pass as a warning.
        "unresolved_endpoints": [],
    }
    assert p.read_text() == before
