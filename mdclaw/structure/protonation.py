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
from typing import Optional, Dict, Any  # noqa: E402

from openmm.app import PDBFile  # noqa: E402
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


def _extract_histidine_states(pdb_file: Path) -> dict:
    """Extract histidine protonation states from PDB file.

    Parses the PDB file to identify HID, HIE, and HIP residues assigned
    by pdb2pqr/propka.

    Args:
        pdb_file: Path to PDB file with protonation assigned

    Returns:
        Dict mapping residue identifier to protonation state
        e.g., {"A:126": "HIE", "A:134": "HID", "B:172": "HIP"}
    """
    his_states = {}
    try:
        with open(pdb_file) as f:
            for line in f:
                if line.startswith(("ATOM", "HETATM")):
                    resname = line[17:20].strip()
                    if resname in ("HID", "HIE", "HIP"):
                        chain = line[21].strip() or "A"
                        resnum = line[22:26].strip()
                        key = f"{chain}:{resnum}"
                        if key not in his_states:
                            his_states[key] = resname
    except Exception as e:
        logger.warning(f"Could not extract histidine states: {e}")
    return his_states


def _apply_histidine_states(pdb_file: Path, histidine_states: dict) -> None:
    """Apply user-specified histidine protonation states to a PDB file.

    Renames HIS/HID/HIE/HIP residues to the user-specified state.
    Modifies the file in place.

    Args:
        pdb_file: Path to PDB file to modify
        histidine_states: Dict mapping "chain:resnum" to state ("HID", "HIE", "HIP")
                         e.g., {"A:126": "HIE", "A:152": "HID"}
    """
    if not histidine_states:
        return

    try:
        with open(pdb_file) as f:
            lines = f.readlines()

        modified_lines = []
        for line in lines:
            if line.startswith(("ATOM", "HETATM")):
                resname = line[17:20].strip()
                if resname in ("HIS", "HID", "HIE", "HIP"):
                    chain = line[21].strip() or "A"
                    resnum = line[22:26].strip()
                    key = f"{chain}:{resnum}"
                    if key in histidine_states:
                        new_state = histidine_states[key]
                        # Replace residue name (columns 18-20, 1-indexed)
                        line = line[:17] + f"{new_state:>3}" + line[20:]
            modified_lines.append(line)

        with open(pdb_file, 'w') as f:
            f.writelines(modified_lines)

        logger.info(f"Applied {len(histidine_states)} histidine state(s) to {pdb_file}")
    except Exception as e:
        logger.warning(f"Could not apply histidine states: {e}")


_PROTONATION_STATE_ALIASES = {
    "HSD": "HID",
    "HSE": "HIE",
    "HSP": "HIP",
}


_PROTONATION_STATE_SPECS: Dict[str, Dict[str, Any]] = {
    "ASP": {
        "base": "ASP",
        "modeller_variant": "ASP",
        "input_names": {"ASP", "ASH"},
        "present": set(),
        "absent": {"HD2"},
    },
    "ASH": {
        "base": "ASP",
        "modeller_variant": "ASH",
        "input_names": {"ASP", "ASH"},
        "present": {"HD2"},
        "absent": set(),
    },
    "GLU": {
        "base": "GLU",
        "modeller_variant": "GLU",
        "input_names": {"GLU", "GLH"},
        "present": set(),
        "absent": {"HE2"},
    },
    "GLH": {
        "base": "GLU",
        "modeller_variant": "GLH",
        "input_names": {"GLU", "GLH"},
        "present": {"HE2"},
        "absent": set(),
    },
    "HID": {
        "base": "HIS",
        "modeller_variant": "HID",
        "input_names": {"HIS", "HID", "HIE", "HIP", "HSD", "HSE", "HSP"},
        "present": {"HD1"},
        "absent": {"HE2"},
    },
    "HIE": {
        "base": "HIS",
        "modeller_variant": "HIE",
        "input_names": {"HIS", "HID", "HIE", "HIP", "HSD", "HSE", "HSP"},
        "present": {"HE2"},
        "absent": {"HD1"},
    },
    "HIP": {
        "base": "HIS",
        "modeller_variant": "HIP",
        "input_names": {"HIS", "HID", "HIE", "HIP", "HSD", "HSE", "HSP"},
        "present": {"HD1", "HE2"},
        "absent": set(),
    },
    "LYS": {
        "base": "LYS",
        "modeller_variant": "LYS",
        "input_names": {"LYS", "LYN"},
        "present": {"HZ3"},
        "absent": set(),
    },
    "LYN": {
        "base": "LYS",
        "modeller_variant": "LYN",
        "input_names": {"LYS", "LYN"},
        "present": set(),
        "absent": {"HZ3"},
    },
    "CYS": {
        "base": "CYS",
        "modeller_variant": "CYS",
        "input_names": {"CYS", "CYM", "CYX"},
        "present": {"HG"},
        "absent": set(),
    },
    "CYX": {
        "base": "CYS",
        "modeller_variant": "CYX",
        "input_names": {"CYS", "CYM", "CYX"},
        "present": set(),
        "absent": {"HG"},
    },
    # OpenMM's hydrogen-definition variant for a deprotonated cysteine and
    # disulfide cysteine is the same no-HG pattern (CYX). Amber's force-field
    # template distinguishes the thiolate as CYM, so we stamp CYM after H
    # rebuilding while asking Modeller for the CYX hydrogen pattern.
    "CYM": {
        "base": "CYS",
        "modeller_variant": "CYX",
        "input_names": {"CYS", "CYM", "CYX"},
        "present": set(),
        "absent": {"HG"},
    },
}


def _parse_protonation_site_key(key: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in str(key).split(":")]
    if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
        raise ValueError(
            "Protonation site keys must be '<chain>:<resnum>' or "
            "'<chain>:<resnum>:<icode>'"
        )
    return parts[0], parts[1], parts[2] if len(parts) == 3 else ""


def _canonical_protonation_state(state: Any) -> str:
    canonical = _PROTONATION_STATE_ALIASES.get(str(state).strip().upper(), str(state).strip().upper())
    if canonical not in _PROTONATION_STATE_SPECS:
        supported = ", ".join(sorted(_PROTONATION_STATE_SPECS))
        raise ValueError(f"Unsupported protonation state {state!r}. Supported states: {supported}")
    return canonical


def _normalize_protonation_state_overrides(
    protonation_states: Optional[Dict[str, Any]] = None,
    histidine_states: Optional[dict[str, str]] = None,
) -> list[dict[str, str]]:
    """Normalize user-specified site protonation overrides.

    Supported inputs:
    - [{"chain": "A", "resnum": 57, "state": "HIP"}, ...]
    - {"A:57": "HIP", "A:25": "ASH"}
    - legacy histidine_states={"A:57": "HIP"}
    """
    records: list[dict[str, str]] = []

    def add_record(chain: Any, resnum: Any, state: Any, icode: Any = "") -> None:
        if chain is None or str(chain).strip() == "":
            raise ValueError("Protonation state records require a non-empty 'chain'")
        if resnum is None or str(resnum).strip() == "":
            raise ValueError("Protonation state records require a non-empty 'resnum'")
        records.append({
            "chain": str(chain).strip(),
            "resnum": str(resnum).strip(),
            "icode": str(icode or "").strip(),
            "state": _canonical_protonation_state(state),
        })

    if isinstance(protonation_states, dict):
        for key, state in protonation_states.items():
            chain, resnum, icode = _parse_protonation_site_key(str(key))
            add_record(chain, resnum, state, icode)
    elif isinstance(protonation_states, list):
        for entry in protonation_states:
            if not isinstance(entry, dict):
                raise ValueError("Each protonation state entry must be a dict")
            add_record(
                entry.get("chain"),
                entry.get("resnum", entry.get("residue_number")),
                entry.get("state", entry.get("protonation_state")),
                entry.get("icode", entry.get("insertion_code", "")),
            )
    elif protonation_states is not None:
        raise ValueError("protonation_states must be a dict, list of dicts, or None")

    if histidine_states:
        for key, state in histidine_states.items():
            chain, resnum, icode = _parse_protonation_site_key(str(key))
            add_record(chain, resnum, state, icode)

    deduped: dict[tuple[str, str, str], dict[str, str]] = {}
    for record in records:
        key = (record["chain"], record["resnum"], record["icode"])
        prior = deduped.get(key)
        if prior and prior["state"] != record["state"]:
            raise ValueError(
                f"Conflicting protonation states for {record['chain']}:{record['resnum']}"
                f"{(':' + record['icode']) if record['icode'] else ''}: "
                f"{prior['state']} vs {record['state']}"
            )
        deduped[key] = record
    return list(deduped.values())


def _apply_protonation_states_with_modeller(
    pdb_file: Path,
    protonation_states: list[dict[str, str]],
    ph: float = 7.4,
) -> dict:
    """Rebuild user-specified residue protonation states with OpenMM Modeller.

    The input PDB is modified in place.  Residue names are canonicalized only
    transiently so ``Modeller.addHydrogens(variants=...)`` can apply the
    desired hydrogen pattern, then stamped back to the Amber variant name.
    """
    result: dict[str, Any] = {
        "success": False,
        "applied_states": [],
        "histidine_states": {},
        "errors": [],
        "warnings": [],
    }
    if not protonation_states:
        result["success"] = True
        return result

    try:
        from openmm.app import Modeller
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"OpenMM Modeller is required for protonation states: {exc}")
        return result

    try:
        pdb = PDBFile(str(pdb_file))
        modeller = Modeller(pdb.topology, pdb.positions)
        residues = list(modeller.topology.residues())

        residue_by_site: dict[tuple[str, str, str], Any] = {}
        for residue in residues:
            site = (
                str(residue.chain.id).strip(),
                str(residue.id).strip(),
                str(getattr(residue, "insertionCode", "") or "").strip(),
            )
            residue_by_site[site] = residue

        variants: list[Optional[str]] = [None] * len(residues)
        matched: dict[int, dict[str, str]] = {}

        for record in protonation_states:
            site = (record["chain"], record["resnum"], record.get("icode", ""))
            residue = residue_by_site.get(site)
            if residue is None:
                result["errors"].append(
                    f"Protonation target not found: {record['chain']}:{record['resnum']}"
                    f"{(':' + record.get('icode', '')) if record.get('icode') else ''}"
                )
                continue
            state = record["state"]
            spec = _PROTONATION_STATE_SPECS[state]
            current_name = str(residue.name).strip().upper()
            if current_name not in spec["input_names"]:
                result["errors"].append(
                    f"State {state} is incompatible with residue {current_name} at "
                    f"{record['chain']}:{record['resnum']}; expected one of "
                    f"{sorted(spec['input_names'])}"
                )
                continue

            residue.name = spec["base"]
            variants[residue.index] = spec["modeller_variant"]
            matched[residue.index] = record

        if result["errors"]:
            return result

        actual_variants = modeller.addHydrogens(pH=ph, variants=variants)
        rebuilt_residues = list(modeller.topology.residues())

        for residue_index, record in matched.items():
            state = record["state"]
            residue = rebuilt_residues[residue_index]
            residue.name = state
            atoms = {atom.name for atom in residue.atoms()}
            spec = _PROTONATION_STATE_SPECS[state]
            missing = sorted(spec["present"] - atoms)
            forbidden = sorted(spec["absent"] & atoms)
            if missing or forbidden:
                result["errors"].append(
                    f"Protonation validation failed for {record['chain']}:{record['resnum']} "
                    f"as {state}: missing={missing}, forbidden_present={forbidden}"
                )
                continue
            applied = {
                "chain": record["chain"],
                "resnum": record["resnum"],
                "icode": record.get("icode", ""),
                "state": state,
                "modeller_variant": str(actual_variants[residue_index] or ""),
            }
            result["applied_states"].append(applied)
            if state in {"HID", "HIE", "HIP"}:
                key = f"{record['chain']}:{record['resnum']}"
                if record.get("icode"):
                    key += f":{record['icode']}"
                result["histidine_states"][key] = state

        if result["errors"]:
            return result

        tmp_file = pdb_file.with_suffix(pdb_file.suffix + ".protonation.tmp")
        with tmp_file.open("w") as fh:
            PDBFile.writeFile(modeller.topology, modeller.positions, fh, keepIds=True)
        variant_names = set(_PROTONATION_STATE_SPECS)
        normalized_lines = []
        for line in tmp_file.read_text().splitlines(keepends=True):
            if line.startswith("HETATM") and line[17:20].strip().upper() in variant_names:
                line = "ATOM  " + line[6:]
            normalized_lines.append(line)
        tmp_file.write_text("".join(normalized_lines))
        tmp_file.replace(pdb_file)
        result["success"] = True
        return result
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"OpenMM Modeller protonation rebuild failed: {type(exc).__name__}: {exc}")
        return result


# AMINO_ACIDS, AMBER_PROTEIN_RESIDUES, and WATER_NAMES are imported from
# mdclaw.chemistry_constants at the top of this module.
