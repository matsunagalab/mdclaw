"""
Structure Server - PDB retrieval and structure cleaning tools.

Provides tools for:
- Automatic retrieval of structure files from PDB/AlphaFold/PDB-REDO (prefers mmCIF)
- Chain separation and classification using gemmi
- Structure cleaning, missing residue modeling, water/heterogen removal, and protonation using PDBFixer
- Automatic detection of disulfide bonds and CYS->CYX renaming
- Mutation modeling with HPacker
- Ligand chemistry preparation with SMILES/SDF template matching
- LLM-friendly structure validation and error reporting at each step
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import List, Optional, Dict  # noqa: E402

from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,
)

# Default working directory for prepare_complex when output_dir is not specified
WORKING_DIR = Path(".")
PDB_CHAIN_ID_POOL = (
    list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + list("abcdefghijklmnopqrstuvwxyz")
    + list("0123456789")
)
_DEUTERIUM_FALLBACK_ATOM_NAME_RE = re.compile(r"^D[0-9]*$")
DEFAULT_TERMINAL_CAP_FORCEFIELD = "ff19SB"
SUPPORTED_N_TERMINAL_CAPS = {"ACE"}
SUPPORTED_C_TERMINAL_CAPS = {"NME"}
TERMINAL_CAP_RESIDUES = SUPPORTED_N_TERMINAL_CAPS | SUPPORTED_C_TERMINAL_CAPS
SUPPORTED_PREP_SOLVENT_TYPES = {"explicit", "implicit", "vacuum"}

# Initialize tool wrappers
pdb2pqr_wrapper = BaseToolWrapper("pdb2pqr")
pdb4amber_wrapper = BaseToolWrapper("pdb4amber")


def _reconcile_cyx_cys_in_pdb(pdb_file: str, disulfide_bonds: List[dict]) -> Dict[str, int]:
    """Rewrite CYS/CYX residue names in *pdb_file* to match *disulfide_bonds*.

    pdb2pqr geometrically detects SS-bonded cysteines and renames them to
    CYX independently of what ``clean_protein`` is told. When the caller
    supplies an explicit ``disulfide_pairs`` list (complete replacement),
    ``result["disulfide_bonds"]`` is the authoritative view and this
    helper brings the merged PDB in line with it:

    - CYX residues *not* in ``disulfide_bonds`` are demoted back to CYS
      (otherwise the Amber CYX template would be applied to a residue
      without an SS bond, leaving SG unprotonated — chemically wrong).
    - CYS residues that *are* in ``disulfide_bonds`` are promoted to CYX.

    Additionally, every final CYX residue has its ``HG`` thiol hydrogen
    stripped. SS-bonded cysteines have their SG bonded to another SG,
    not to a proton, and the Amber CYX template has no ``HG`` atom — a
    surviving HG fails template matching at openmmforcefields build time
    (and historically caused tleap to abort with
    ``FATAL: Atom .R<CYX N>.A<HG> does not have a type``).
    Observed for 5vm0_A and 7on5_A in the 2422-row batch.

    Runs unconditionally after merge; it is a no-op whenever the
    auto-detection path agrees with pdb2pqr (the common case).
    """
    target_cyx: set = set()
    for bond in disulfide_bonds:
        for key in ("cys1", "cys2"):
            entry = bond.get(key) or {}
            chain = entry.get("chain")
            resnum = entry.get("resnum")
            if chain is not None and resnum is not None:
                target_cyx.add((chain, int(resnum)))

    path = Path(pdb_file)
    lines = path.read_text().splitlines()
    out: List[str] = []
    renamed_to_cys = 0
    renamed_to_cyx = 0
    stripped_hg = 0

    for line in lines:
        if len(line) >= 27 and line.startswith(("ATOM", "HETATM")):
            resname = line[17:20].strip()
            chain = line[21].strip()
            try:
                resnum = int(line[22:26].strip())
            except ValueError:
                out.append(line)
                continue
            key = (chain, resnum)
            final_resname = resname
            if resname == "CYX" and key not in target_cyx:
                line = line[:17] + "CYS" + line[20:]
                final_resname = "CYS"
                renamed_to_cys += 1
            elif resname == "CYS" and key in target_cyx:
                line = line[:17] + "CYX" + line[20:]
                final_resname = "CYX"
                renamed_to_cyx += 1

            # Drop the thiol hydrogen from every CYX record. This covers
            # both the CYS→CYX promotion path above and pre-existing CYX
            # residues from pdb2pqr that still carry HG (which would fail
            # template matching against the Amber CYX residue template at
            # openmmforcefields build time).
            if final_resname == "CYX" and line[12:16].strip() == "HG":
                stripped_hg += 1
                continue
        out.append(line)

    path.write_text("\n".join(out) + ("\n" if lines and not lines[-1].endswith("\n") else ""))
    return {
        "renamed_to_cys": renamed_to_cys,
        "renamed_to_cyx": renamed_to_cyx,
        "stripped_hg_from_cyx": stripped_hg,
    }


def _merge_disulfide_pairs(
    ssbond_pairs: List[dict],
    distance_pairs: List[dict],
    select_chains: Optional[List[str]] = None,
) -> List[dict]:
    """Merge explicit SSBOND records with distance-based candidates.

    Dedupes on the unordered pair of ``(chain, resnum)``. When the same
    pair appears in both sources, the SSBOND entry wins but its
    ``source`` is updated to ``"pdb_ssbond+distance"`` and the measured
    ``distance_angstrom`` from the distance-based result is preferred
    (since the SSBOND column value may be absent for non-1555 symmetry).

    When ``select_chains`` is given, pairs are filtered to those where
    BOTH residues' chains are selected — pairs that span dropped chains
    cannot exist in the merged PDB downstream.
    """
    def _key(pair: dict) -> frozenset:
        return frozenset({
            (pair["cys1"]["chain"], pair["cys1"]["resnum"]),
            (pair["cys2"]["chain"], pair["cys2"]["resnum"]),
        })

    selected = set(select_chains) if select_chains else None

    def _passes_chain_filter(pair: dict) -> bool:
        if selected is None:
            return True
        return (
            pair["cys1"]["chain"] in selected
            and pair["cys2"]["chain"] in selected
        )

    merged: Dict[frozenset, dict] = {}
    for pair in ssbond_pairs:
        if not _passes_chain_filter(pair):
            continue
        merged[_key(pair)] = dict(pair)  # shallow copy

    for pair in distance_pairs:
        if not _passes_chain_filter(pair):
            continue
        k = _key(pair)
        if k in merged:
            existing = merged[k]
            existing["source"] = "pdb_ssbond+distance"
            if pair.get("distance_angstrom") is not None:
                existing["distance_angstrom"] = pair["distance_angstrom"]
        else:
            merged[k] = dict(pair)

    return list(merged.values())


# =============================================================================
# Known Ligand SMILES Dictionary (for template matching)
# =============================================================================
# These SMILES are from PDB Chemical Component Dictionary (CCD)
# Used as fallback when CCD API is unavailable
