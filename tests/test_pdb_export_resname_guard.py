"""Root-cause guard against silently reintroducing the residue-name loss.

OpenMM's ``PDBFile`` LOADER normalizes Amber/PTM/water residue names on read
(``GLH``->``GLU``, ``HID``->``HIS``, ``SEP``->``SER``, ``WAT``->``HOH`` ...) so
its ForceField can match templates. Any code that loads a structure through
OpenMM and re-exports it via ``PDBFile.writeFile`` therefore drops those names
unless it restores them on the way out (the normalization itself must NOT be
removed — OpenMM needs it for template matching, so restoration is the correct
layer).

We fixed every export site once; this test stops a NEW ``PDBFile.writeFile``
from silently bringing the bug back. It pins the full inventory of call sites
and requires each ``restore`` site to actually reference a restore helper. If
this test fails you either added/moved/removed a ``PDBFile.writeFile`` — update
EXPECTED and, for an export that round-trips OpenMM-normalized names, restore
them after the write (``restore_resnames_from_source_pdb`` /
``restore_resnames_by_residue_key`` /
``render_simulation_pdb_preserving_resnames``) — or you removed a helper a
``restore`` site relied on.
"""

from pathlib import Path

import pytest

MDCLAW = Path(__file__).resolve().parent.parent / "mdclaw"

RESTORE_HELPERS = (
    "restore_resnames_from_source_pdb",
    "restore_resnames_by_residue_key",
    "render_simulation_pdb_preserving_resnames",
    "preserve_long_resnames_in_pdb_text",
    "_restore_reference_resnames",
)

# relpath under mdclaw/ -> (number of PDBFile.writeFile call lines, kind)
#   "restore" — export round-trips OpenMM-normalized names; the file MUST also
#               reference a restore helper (verified below).
#   "source"  — writes a name-correct or name-irrelevant structure and needs no
#               restore: a transient pre-protonation intermediate, a standard
#               nucleic chain (DA/DC/DG/DT, A/G/U are not normalized), or the
#               render helper's own internal write.
#   "custom"  — restores names with bespoke inline logic, not a shared helper.
EXPECTED = {
    "structure/pdb_utils.py": (1, "source"),            # render helper internals
    "amber/openmm_build.py": (1, "restore"),            # topology.pdb contract
    "openmm_system/build.py": (1, "restore"),           # topology.pdb (openmm FF)
    "simulation/platform.py": (1, "restore"),           # diagnostic state export
    "structure/terminal_caps.py": (1, "restore"),       # cap H completion
    "solvation/water.py": (1, "restore"),               # openmm solvation fallback
    "sidechain_packer.py": (1, "restore"),              # HPacker mutation output
    "structure/protonation.py": (1, "restore"),         # user-state Modeller path
    "structure/clean_protein.py": (2, "source"),        # transient + nucleic_h
}


def _writefile_call_lines(path: Path) -> int:
    return sum(
        1
        for line in path.read_text().splitlines()
        if line.lstrip().startswith("PDBFile.writeFile")
    )


def _discover() -> dict[str, int]:
    found: dict[str, int] = {}
    for py in MDCLAW.rglob("*.py"):
        n = _writefile_call_lines(py)
        if n:
            found[py.relative_to(MDCLAW).as_posix()] = n
    return found


def test_pdb_writefile_inventory_is_pinned():
    found = _discover()
    expected_counts = {k: v[0] for k, v in EXPECTED.items()}
    assert found == expected_counts, (
        "PDBFile.writeFile inventory changed. A new/moved export can silently "
        "drop Amber/PTM residue names (OpenMM's loader normalized them). For an "
        "export that round-trips OpenMM-loaded names, restore them after writing "
        f"({', '.join(RESTORE_HELPERS[:3])}), then update EXPECTED.\n"
        f"  discovered: {sorted(found.items())}\n"
        f"  expected:   {sorted(expected_counts.items())}"
    )


@pytest.mark.parametrize(
    "relpath",
    [k for k, v in EXPECTED.items() if v[1] == "restore"],
)
def test_restore_sites_reference_a_helper(relpath):
    text = (MDCLAW / relpath).read_text()
    assert any(h in text for h in RESTORE_HELPERS), (
        f"{relpath} writes a PDB that round-trips OpenMM-normalized names but "
        f"references no restore helper ({', '.join(RESTORE_HELPERS)}). Restore "
        "the residue names after PDBFile.writeFile, or reclassify the site."
    )
