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
# Read-only disulfide detection (SSBOND/_struct_conn records + S-S distances)
# =============================================================================


def _detect_disulfide_candidates(structure_path: Path) -> list[dict]:
    """Detect potential disulfide bonds by measuring CYS-CYS S-S distances.

    This is a read-only analysis that doesn't modify the structure.
    """
    try:
        import gemmi
    except ImportError:
        return []

    candidates = []

    try:
        suffix = structure_path.suffix.lower()
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            st = gemmi.make_structure_from_block(block)
        else:
            st = gemmi.read_pdb(str(structure_path))

        model = st[0]

        # Find all CYS residues with SG atoms
        cys_residues = []
        for chain in model:
            for res in chain:
                if res.name in ("CYS", "CYX"):
                    sg_atom = res.find_atom("SG", "*")
                    if sg_atom:
                        cys_residues.append({
                            "chain": chain.name,
                            "resnum": res.seqid.num,
                            "resname": res.name,
                            "sg_pos": sg_atom.pos,
                        })

        # Check all pairs for S-S distance
        for i, cys1 in enumerate(cys_residues):
            for cys2 in cys_residues[i + 1:]:
                # Calculate S-S distance
                dx = cys1["sg_pos"].x - cys2["sg_pos"].x
                dy = cys1["sg_pos"].y - cys2["sg_pos"].y
                dz = cys1["sg_pos"].z - cys2["sg_pos"].z
                distance = (dx * dx + dy * dy + dz * dz) ** 0.5

                # Typical S-S distance is ~2.03Å, consider up to 3.0Å as candidates
                if distance < 3.0:
                    confidence = "high" if distance < 2.5 else "medium"
                    candidates.append({
                        "cys1": {
                            "chain": cys1["chain"],
                            "resnum": cys1["resnum"],
                            "resname": cys1["resname"],
                        },
                        "cys2": {
                            "chain": cys2["chain"],
                            "resnum": cys2["resnum"],
                            "resname": cys2["resname"],
                        },
                        "distance_angstrom": round(distance, 2),
                        "confidence": confidence,
                        "recommendation": "form_bond" if confidence == "high" else "review",
                        "source": "distance",
                    })
    except Exception as e:
        logger.warning(f"Error detecting disulfide candidates: {e}")

    return candidates


def _parse_ssbond_records(structure_path: Path) -> list[dict]:
    """Parse explicit disulfide bond records from PDB SSBOND or mmCIF _struct_conn.

    Uses gemmi's unified ``Structure.connections`` which exposes both PDB
    SSBOND lines and mmCIF ``_struct_conn`` entries with
    ``conn_type_id="disulf"``. The returned entries use the same schema as
    ``_detect_disulfide_candidates`` so the two sources can be merged
    downstream, with the additional field ``source="pdb_ssbond"``.

    The ``distance_angstrom`` is recomputed from the actual SG atom
    coordinates — the SSBOND ``Length`` column (74-78) is optional and
    only meaningful when both symmetry operators are 1555, so the
    measured value is preferred.
    """
    try:
        import gemmi
    except ImportError:
        return []

    out: list[dict] = []
    try:
        suffix = structure_path.suffix.lower()
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            st = gemmi.make_structure_from_block(block)
        else:
            st = gemmi.read_pdb(str(structure_path))

        if len(st) == 0:
            return []
        model = st[0]

        def _find_sg_atom(addr):
            """Locate the SG atom described by a gemmi AtomAddress, if any."""
            try:
                chain = model.find_chain(addr.chain_name)
                if chain is None:
                    return None
                # Prefer exact seqid match; fallback to iterating residues.
                for res in chain:
                    if res.seqid.num == addr.res_id.seqid.num and res.name == addr.res_id.name:
                        return res.find_atom(addr.atom_name or "SG", "*")
                return None
            except Exception:
                return None

        for conn in st.connections:
            if conn.type != gemmi.ConnectionType.Disulf:
                continue
            p1, p2 = conn.partner1, conn.partner2
            entry = {
                "cys1": {
                    "chain": p1.chain_name,
                    "resnum": p1.res_id.seqid.num,
                    "resname": p1.res_id.name,
                },
                "cys2": {
                    "chain": p2.chain_name,
                    "resnum": p2.res_id.seqid.num,
                    "resname": p2.res_id.name,
                },
                "distance_angstrom": None,
                "confidence": "high",
                "recommendation": "form_bond",
                "source": "pdb_ssbond",
            }

            a1 = _find_sg_atom(p1)
            a2 = _find_sg_atom(p2)
            if a1 is not None and a2 is not None:
                dx = a1.pos.x - a2.pos.x
                dy = a1.pos.y - a2.pos.y
                dz = a1.pos.z - a2.pos.z
                entry["distance_angstrom"] = round((dx * dx + dy * dy + dz * dz) ** 0.5, 2)

            out.append(entry)
    except Exception as e:
        logger.warning(f"Error parsing SSBOND records: {e}")

    return out


# =============================================================================
# Known Ligand SMILES Dictionary (for template matching)
# =============================================================================
# These SMILES are from PDB Chemical Component Dictionary (CCD)
# Used as fallback when CCD API is unavailable
