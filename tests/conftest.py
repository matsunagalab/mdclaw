"""Shared test fixtures for MDZen test suite."""

import sys
import textwrap
from pathlib import Path

import pytest

# Add servers directory to path for direct imports
servers_dir = Path(__file__).parent.parent / "servers"
sys.path.insert(0, str(servers_dir))


# --- Minimal PDB fixtures ---

ALANINE_DIPEPTIDE_PDB = textwrap.dedent("""\
REMARK   Alanine dipeptide (ACE-ALA-NME) for testing
ATOM      1  CH3 ACE A   1       2.000   1.000   0.000  1.00  0.00           C
ATOM      2  C   ACE A   1       0.517   0.768   0.000  1.00  0.00           C
ATOM      3  O   ACE A   1       0.018   0.768  -1.133  1.00  0.00           O
ATOM      4  N   ALA A   2      -0.150   0.540   1.114  1.00  0.00           N
ATOM      5  CA  ALA A   2      -1.600   0.308   1.114  1.00  0.00           C
ATOM      6  HA  ALA A   2      -1.949  -0.013   0.138  1.00  0.00           H
ATOM      7  CB  ALA A   2      -1.905  -0.770   2.152  1.00  0.00           C
ATOM      8  C   ALA A   2      -2.326   1.608   1.432  1.00  0.00           C
ATOM      9  O   ALA A   2      -1.738   2.399   2.170  1.00  0.00           O
ATOM     10  N   NME A   3      -3.537   1.817   0.909  1.00  0.00           N
ATOM     11  CH3 NME A   3      -4.300   3.029   1.180  1.00  0.00           C
TER
END
""")

SMALL_PROTEIN_PDB = textwrap.dedent("""\
REMARK   Small protein fragment (5 residues) for testing
ATOM      1  N   ALA A   1       1.000   1.000   1.000  1.00 10.00           N
ATOM      2  CA  ALA A   1       2.450   1.000   1.000  1.00 10.00           C
ATOM      3  C   ALA A   1       3.000   2.400   1.000  1.00 10.00           C
ATOM      4  O   ALA A   1       2.300   3.400   1.000  1.00 10.00           O
ATOM      5  CB  ALA A   1       3.000   0.200   2.200  1.00 10.00           C
ATOM      6  N   GLY A   2       4.300   2.400   1.000  1.00 10.00           N
ATOM      7  CA  GLY A   2       5.000   3.700   1.000  1.00 10.00           C
ATOM      8  C   GLY A   2       6.500   3.600   1.000  1.00 10.00           C
ATOM      9  O   GLY A   2       7.100   2.500   1.000  1.00 10.00           O
ATOM     10  N   ALA A   3       7.100   4.700   1.000  1.00 10.00           N
ATOM     11  CA  ALA A   3       8.550   4.800   1.000  1.00 10.00           C
ATOM     12  C   ALA A   3       9.100   6.200   1.000  1.00 10.00           C
ATOM     13  O   ALA A   3       8.400   7.200   1.000  1.00 10.00           O
ATOM     14  CB  ALA A   3       9.100   4.000   2.200  1.00 10.00           C
ATOM     15  N   GLY A   4      10.400   6.200   1.000  1.00 10.00           N
ATOM     16  CA  GLY A   4      11.100   7.500   1.000  1.00 10.00           C
ATOM     17  C   GLY A   4      12.600   7.400   1.000  1.00 10.00           C
ATOM     18  O   GLY A   4      13.200   6.300   1.000  1.00 10.00           O
ATOM     19  N   ALA A   5      13.200   8.500   1.000  1.00 10.00           N
ATOM     20  CA  ALA A   5      14.650   8.600   1.000  1.00 10.00           C
ATOM     21  C   ALA A   5      15.200  10.000   1.000  1.00 10.00           C
ATOM     22  O   ALA A   5      14.500  11.000   1.000  1.00 10.00           O
ATOM     23  CB  ALA A   5      15.200   7.800   2.200  1.00 10.00           C
TER
END
""")


@pytest.fixture
def small_pdb(tmp_path):
    """Create a small protein PDB file for testing."""
    pdb_file = tmp_path / "small_protein.pdb"
    pdb_file.write_text(SMALL_PROTEIN_PDB)
    return str(pdb_file)


@pytest.fixture
def alanine_dipeptide_pdb(tmp_path):
    """Create an alanine dipeptide PDB file for testing."""
    pdb_file = tmp_path / "alanine_dipeptide.pdb"
    pdb_file.write_text(ALANINE_DIPEPTIDE_PDB)
    return str(pdb_file)
