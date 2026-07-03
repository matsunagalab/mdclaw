"""
Amber Server — curated Amber → OpenMM System builder.

Provides tools for:
- ``build_amber_system``: load a prepared PDB through OpenFF Pablo, apply Amber
  protein / nucleic / glycan / lipid / PTM force fields plus topology-time
  ligand templates (``GAFFTemplateGenerator``), and emit a portable
  ``system.xml`` +
  ``topology.pdb`` + ``state.xml`` triple consumed by ``run_minimization`` /
  ``run_equilibration`` / ``run_production``, plus a minimization report for
  benchmark evidence.
- Supporting both implicit (no PBC) and explicit (with PBC, optionally
  membrane) solvent setups.
- Handling protein-ligand complexes by consuming prep-stage
  ``ligand_chemistry`` records; topology parameterizes the small molecules
  with ``GAFFTemplateGenerator``.
- Handling glycoproteins by converting deposited glycan residues to
  Amber/GLYCAM notation at topology time, preserving the generated bond plan,
  and completing only GLYCAM-specific hydrogens before System creation.

The XML triple is the only topology contract on the run side; tleap and
parm7/rst7 are not produced or consumed anywhere. AmberTools
(``pdb4amber`` and ``cpptraj``) remain available for structure-preparation
support; ligand parameterization is not a prep-stage mdclaw artifact.
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

from pathlib import Path  # noqa: E402
from typing import Optional, Dict, Any  # noqa: E402

from mdclaw._common import (  # noqa: E402
    ensure_directory, BaseToolWrapper, guess_pdb_element,
    is_glycan_residue_name,
)
from mdclaw.chemistry_constants import (  # noqa: E402
    PHOSPHO_RESNAMES,
    STANDARD_DNA_RESNAMES,
    STANDARD_RNA_RESNAMES,
)

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

# Initialize tool wrappers.
# ``tleap`` is no longer used: the curated build path runs through
# ``openmmforcefields.SystemGenerator`` and emits the modern
# ``system.xml`` + ``topology.pdb`` + ``state.xml`` triple (PR3 of the
# openmmforcefields-unification refactor). ``cpptraj`` is still used for
# the GLYCAM ``prepareforleap`` glycan conversion stage; see
# ``_prepare_glycam_pdb_with_cpptraj`` for context.
cpptraj_wrapper = BaseToolWrapper("cpptraj")


# =============================================================================
# Force Field Mappings (based on Amber Manual 2024 recommendations)
# =============================================================================

from mdclaw.amber.forcefield_constants import POLYPHOSPHATE_LIGANDS, STANDARD_PROTEIN_RESIDUES, WATER_RESIDUES  # noqa: E402


def _gemmi_available() -> bool:
    try:
        import gemmi  # noqa: F401
    except ImportError:
        return False
    return True


def _scan_pdb_text_for_ptm_residues(path: Path) -> list[dict]:
    sites = []
    seen = set()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return sites
    for line in lines:
        if not line.startswith(("ATOM  ", "HETATM")) or len(line) < 26:
            continue
        name = line[17:20].strip()
        if name not in PHOSPHO_RESNAMES:
            continue
        chain = line[21].strip()
        try:
            resnum = int(line[22:26])
        except ValueError:
            resnum = None
        key = (chain, resnum, name)
        if key in seen:
            continue
        seen.add(key)
        sites.append({"chain": chain, "resnum": resnum, "name": name})
    return sites


_PABLO_ION_RESNAME_RENAMES = {
    "NA": "NA",
    "NA+": "NA",
    "SOD": "NA",
    "CL": "CL",
    "CL-": "CL",
    "CLA": "CL",
    "K": "K",
    "K+": "K",
    "POT": "K",
    "MG": "MG",
    "MG+": "MG",
    "MG2": "MG",
    "ZN": "ZN",
    "ZN+": "ZN",
    "ZN2": "ZN",
    "CA": "CA",
    "CA+": "CA",
    "CA2": "CA",
    "FE": "FE",
    "FE+": "FE",
    "FE2": "FE",
    "FE3": "FE",
    "MN": "MN",
    "MN2": "MN",
    "CU": "CU",
    "CU1": "CU",
    "CU2": "CU",
    "CO": "CO",
    "CO2": "CO",
}


def _normalize_pdb_chain_id(chain_id: Optional[str]) -> str:
    return str(chain_id or "").strip()


def _canonical_pablo_ion_resname(resname: str) -> Optional[str]:
    return _PABLO_ION_RESNAME_RENAMES.get(str(resname or "").strip().upper())


def _ion_element_symbol(canonical_resname: str) -> str:
    return canonical_resname[:1] + canonical_resname[1:].lower()


def _rewrite_pablo_ion_pdb_line(line: str) -> tuple[str, bool]:
    if not line.startswith(("ATOM  ", "HETATM")):
        return line, False
    raw_resname = line[17:20]
    raw_atom_name = line[12:16]
    canonical_res = _canonical_pablo_ion_resname(raw_resname)
    if canonical_res is None:
        return line, False
    # Operate on the record body only and re-append the original line
    # terminator last. Some writers emit ion records shorter than the
    # 80-column PDB width (e.g. a 66-column line with no element column); if
    # the trailing newline is left inside the body, padding it out to the
    # element columns splices the break into the middle of the record and
    # drops every following atom on parse.
    body = line.rstrip("\r\n")
    terminator = line[len(body):]
    rn_key = raw_resname.strip().upper()
    an_key = raw_atom_name.strip().upper()
    new_resname = f"{canonical_res:>3}"
    new_atom = body[12:16]
    if an_key in {rn_key, canonical_res}:
        new_atom = f"{canonical_res:>4}"
    rebuilt = body[:12] + new_atom + body[16:17] + new_resname + body[20:]
    element = _ion_element_symbol(canonical_res)
    # Write the element symbol in columns 77-78 (0-based 76:78), padding the
    # record out when needed while preserving anything past column 78
    # (e.g. the formal-charge columns).
    rebuilt = f"{rebuilt[:76]:<76}{element:>2}{rebuilt[78:]}"
    rewritten = rebuilt + terminator
    return rewritten, rewritten != line


def _scan_pdb_ion_residue_names(path: Path) -> list[str]:
    """Return canonical ion residue names present in a PDB file."""
    ions: set[str] = set()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.startswith("HETATM"):
            continue
        canonical = _canonical_pablo_ion_resname(line[17:20])
        if canonical:
            ions.add(canonical)
    return sorted(ions)


def detect_nucleic_content(pdb_path: Path) -> dict:
    """Detect standard and unsupported nucleic residues in a PDB input."""
    residues: dict[tuple[str, str, str], dict[str, Any]] = {}
    for line in pdb_path.read_text().splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        resname = line[17:20].strip().upper()
        chain = line[21].strip() or "A"
        resnum = line[22:26].strip()
        icode = line[26].strip()
        key = (chain, resnum, icode)
        entry = residues.setdefault(
            key,
            {"chain": chain, "resnum": resnum, "resname": resname, "atoms": set()},
        )
        entry["atoms"].add(line[12:16].strip())

    standard_names = set()
    subtypes = set()
    unsupported = []
    has_protein = False
    sugar_phosphate_markers = {"P", "O3'", "C3'", "C4'", "C5'", "O5'", "C1'"}

    for residue in residues.values():
        resname = residue["resname"]
        if resname in STANDARD_PROTEIN_RESIDUES:
            has_protein = True
            continue
        if resname in STANDARD_DNA_RESNAMES:
            standard_names.add(resname)
            subtypes.add("dna")
            continue
        if resname in STANDARD_RNA_RESNAMES:
            standard_names.add(resname)
            subtypes.add("rna")
            continue
        atoms = residue["atoms"]
        if len(atoms & sugar_phosphate_markers) >= 4 and resname not in (
            STANDARD_PROTEIN_RESIDUES | WATER_RESIDUES | POLYPHOSPHATE_LIGANDS
        ):
            unsupported.append({
                "chain": residue["chain"],
                "resnum": residue["resnum"],
                "resname": resname,
            })

    subtype = None
    if subtypes == {"dna"}:
        subtype = "dna"
    elif subtypes == {"rna"}:
        subtype = "rna"
    elif subtypes:
        subtype = "hybrid"

    return {
        "has_nucleic": bool(subtypes or unsupported),
        "nucleic_subtype": subtype,
        "subtypes": sorted(subtypes),
        "standard_residue_names": sorted(standard_names),
        "unsupported_modified_residues": unsupported,
        "has_protein": has_protein,
    }


def detect_glycan_content(pdb_path: Path) -> dict:
    """Detect glycan residues in a PDB input."""
    residues: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for line in pdb_path.read_text().splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        resname = line[17:20].strip().upper()
        if not is_glycan_residue_name(resname):
            continue
        chain = line[21].strip() or "A"
        resnum = line[22:26].strip()
        icode = line[26].strip()
        key = (chain, resnum, icode, resname)
        residues.setdefault(
            key,
            {
                "chain": chain,
                "resnum": int(resnum) if resnum.lstrip("-").isdigit() else resnum,
                "icode": icode,
                "resname": resname,
            },
        )
    return {
        "has_glycan": bool(residues),
        "residue_names": sorted({r["resname"] for r in residues.values()}),
        "residues": list(residues.values()),
    }


def _pdb_residue_atom_names(pdb_path: Path, residue_name: str) -> list[str]:
    atoms = set()
    for line in pdb_path.read_text().splitlines():
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 26:
            continue
        if line[17:20].strip().upper() == residue_name:
            atoms.add(line[12:16].strip())
    return sorted(atoms)


def _pdb_residue_instance_counts(pdb_path: Path) -> Dict[str, int]:
    """Return unique residue-instance counts keyed by residue name."""
    counts: Dict[str, int] = {}
    seen: set[tuple[str, str, str]] = set()
    for line in pdb_path.read_text().splitlines():
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 26:
            continue
        resname = line[17:20].strip().upper()
        if not resname:
            continue
        key = (line[21].strip(), line[22:26].strip(), line[26].strip(), resname)
        if key in seen:
            continue
        seen.add(key)
        counts[resname] = counts.get(resname, 0) + 1
    return counts


def _pdb_heavy_atoms_for_contacts(pdb_path: Path) -> list[dict]:
    atoms: list[dict] = []
    for line in pdb_path.read_text().splitlines():
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 54:
            continue
        atom_name = line[12:16].strip()
        element = guess_pdb_element(atom_name, line[76:78] if len(line) >= 78 else "")
        if element == "H":
            continue
        try:
            x = float(line[30:38])
            y = float(line[38:46])
            z = float(line[46:54])
        except ValueError:
            continue
        resname = line[17:20].strip().upper()
        atoms.append({
            "atom_name": atom_name,
            "element": element,
            "residue_name": resname,
            "chain_id": line[21].strip(),
            "resnum": line[22:26].strip(),
            "record": line[:6].strip(),
            "coords": (x, y, z),
        })
    return atoms


def get_coordinate_range(pdb_path: Path) -> dict:
    """Calculate coordinate range from PDB file.

    Args:
        pdb_path: Path to PDB file

    Returns:
        Dict with min/max coordinates and ranges for each dimension
    """
    x_coords, y_coords, z_coords = [], [], []

    try:
        with open(pdb_path, 'r') as f:
            for line in f:
                if line.startswith(('ATOM', 'HETATM')):
                    x = float(line[30:38])
                    y = float(line[38:46])
                    z = float(line[46:54])
                    x_coords.append(x)
                    y_coords.append(y)
                    z_coords.append(z)
    except Exception as e:
        logger.warning(f"Could not read coordinates from PDB: {e}")
        return {"success": False}

    if not x_coords:
        return {"success": False}

    return {
        "success": True,
        "x_min": min(x_coords), "x_max": max(x_coords), "x_range": max(x_coords) - min(x_coords),
        "y_min": min(y_coords), "y_max": max(y_coords), "y_range": max(y_coords) - min(y_coords),
        "z_min": min(z_coords), "z_max": max(z_coords), "z_range": max(z_coords) - min(z_coords),
    }


def detect_water_type(pdb_path: Path) -> dict:
    """Detect water model type from PDB file by counting atoms per water.

    packmol-memgen always produces TIP3P waters (3 atoms: O, H1, H2).
    OPC water has 4 atoms (O, H1, H2, EPW).
    TIP4P has 4 atoms (O, H1, H2, M).
    TIP5P has 5 atoms (O, H1, H2, LP1, LP2).

    Args:
        pdb_path: Path to PDB file

    Returns:
        Dict with:
        - water_count: Number of water residues found
        - atoms_per_water: Average atoms per water (3=TIP3P, 4=OPC/TIP4P, 5=TIP5P)
        - detected_type: "tip3p", "opc", "tip4p", "tip5p", or "unknown"
        - has_waters: Whether waters were found
    """
    result = {
        "water_count": 0,
        "atoms_per_water": 0,
        "detected_type": "unknown",
        "has_waters": False
    }

    # Water residue names (different naming conventions)
    water_names = {"WAT", "HOH", "SOL", "TP3", "OPC", "T4P", "T5P"}

    water_atoms_count = 0
    water_residues = set()  # Track unique water residue numbers

    try:
        with open(pdb_path, 'r') as f:
            for line in f:
                if line.startswith(('ATOM', 'HETATM')):
                    res_name = line[17:20].strip()
                    if res_name in water_names:
                        # Get residue number (columns 22-26)
                        res_num = line[22:26].strip()
                        # Get chain ID (column 21)
                        chain = line[21:22]
                        water_key = f"{chain}:{res_num}"
                        water_residues.add(water_key)
                        water_atoms_count += 1
    except Exception as e:
        logger.warning(f"Could not detect water type: {e}")
        return result

    if water_residues:
        result["has_waters"] = True
        result["water_count"] = len(water_residues)
        result["atoms_per_water"] = round(water_atoms_count / len(water_residues), 1)

        # Determine water type based on atoms per residue
        atoms = result["atoms_per_water"]
        if 2.5 <= atoms <= 3.5:
            result["detected_type"] = "tip3p"
        elif 3.5 < atoms <= 4.5:
            result["detected_type"] = "opc"  # or tip4p
        elif 4.5 < atoms <= 5.5:
            result["detected_type"] = "tip5p"

        logger.info(f"Detected {result['water_count']} waters with {atoms} atoms each -> {result['detected_type']}")

    return result
