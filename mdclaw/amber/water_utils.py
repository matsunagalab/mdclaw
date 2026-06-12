"""
Amber Server — curated Amber → OpenMM System builder.

Provides tools for:
- ``build_amber_system``: load a prepared PDB through OpenFF Pablo, apply Amber
  protein / nucleic / glycan / lipid / PTM force fields plus topology-time
  ligand templates (geostd XML when available, otherwise
  ``GAFFTemplateGenerator``), and emit a portable ``system.xml`` +
  ``topology.pdb`` + ``state.xml`` triple consumed by ``run_minimization`` /
  ``run_equilibration`` / ``run_production``, plus a minimization report for
  benchmark evidence.
- Supporting both implicit (no PBC) and explicit (with PBC, optionally
  membrane) solvent setups.
- Handling protein-ligand complexes by consuming prep-stage
  ``ligand_chemistry`` records; topology resolves geostd templates first and
  falls back to ``GAFFTemplateGenerator`` for the remaining small molecules.
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
from typing import List, Optional, Dict, Any  # noqa: E402

from mdclaw._common import (  # noqa: E402
    CANONICAL_WATER_MODELS,
    ensure_directory, BaseToolWrapper, create_guardrail_result, normalize_choice,
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

from mdclaw.amber.forcefield_constants import CANONICAL_PROTEIN_FORCEFIELDS, FORCEFIELD_WATER_COMPATIBILITY  # noqa: E402


def _canonical_forcefield_name(forcefield: Optional[str]) -> Optional[str]:
    """Normalize force field aliases to their canonical names."""
    return normalize_choice(forcefield, CANONICAL_PROTEIN_FORCEFIELDS)


def _canonical_water_model_name(water_model: Optional[str]) -> Optional[str]:
    """Normalize water model aliases to their canonical names."""
    return normalize_choice(water_model, CANONICAL_WATER_MODELS)


def _evaluate_forcefield_water_guardrails(forcefield: str, water_model: str) -> list[Dict[str, Any]]:
    """Evaluate explicit-solvent forcefield/water guardrails."""
    compat = FORCEFIELD_WATER_COMPATIBILITY.get(forcefield, {})
    recommended = compat.get("recommended", [])
    acceptable = compat.get("acceptable", [])
    not_recommended = compat.get("not_recommended", [])
    pair = f"{forcefield} + {water_model}"
    results = []

    if water_model in not_recommended:
        preferred_pair = f"{forcefield} + {recommended[0]}" if recommended else forcefield
        results.append(create_guardrail_result(
            "water_model",
            f"{pair} is blocked by MDClaw. Amber strongly recommends OPC with ff19SB and warns against TIP3P for this force field.",
            severity="error",
            actual=pair,
            expected=preferred_pair,
            suggested_fix=(
                "Use water_model='opc' with forcefield='ff19SB', "
                "or explicitly choose forcefield='ff14SB' with water_model='tip3p' for legacy systems."
            ),
            code="forcefield_water_blocked",
        ))
    elif water_model in acceptable:
        results.append(create_guardrail_result(
            "water_model",
            f"{pair} is allowed, but {forcefield} is optimized for {', '.join(recommended)}.",
            severity="warning",
            actual=pair,
            expected=", ".join(recommended) if recommended else None,
            suggested_fix=f"Prefer water_model='{recommended[0]}' for new {forcefield} systems." if recommended else None,
            code="forcefield_water_not_preferred",
        ))
    elif recommended and water_model not in recommended:
        results.append(create_guardrail_result(
            "water_model",
            f"{pair} is allowed, but recommended water models for {forcefield} are: {', '.join(recommended)}.",
            severity="warning",
            actual=pair,
            expected=", ".join(recommended),
            suggested_fix=f"Prefer water_model='{recommended[0]}' for new {forcefield} systems.",
            code="forcefield_water_recommended_alternative",
        ))

    return results


def fix_ligand_residue_names(pdb_path: Path, output_path: Path, 
                              ligand_residue_names: List[str]) -> dict:
    """Fix ligand residue names in PDB file.
    
    packmol-memgen sometimes renames unknown ligands to "UNL".
    This function replaces UNL with the correct residue name.
    
    Args:
        pdb_path: Input PDB file path
        output_path: Output PDB file path
        ligand_residue_names: List of correct ligand residue names
    
    Returns:
        Dict with statistics about replacements made
    """
    result = {
        "success": True,
        "unl_count": 0,
        "replacements": [],
        "errors": [],
    }
    
    if not ligand_residue_names:
        # No ligands to fix, just copy file
        import shutil
        shutil.copy(pdb_path, output_path)
        return result
    
    unique_ligand_names = sorted({name[:3].upper() for name in ligand_residue_names if name})
    target_residue = unique_ligand_names[0] if len(unique_ligand_names) == 1 else None
    
    lines_out = []
    with open(pdb_path, 'r') as f:
        for line in f:
            if line.startswith(('ATOM', 'HETATM')):
                # Check if residue name is UNL (columns 17-20)
                res_name = line[17:20].strip()
                if res_name == 'UNL':
                    result["unl_count"] += 1
                    if target_residue:
                        # Replace UNL with target residue name (right-padded to 3 chars)
                        new_line = line[:17] + f"{target_residue:>3}" + line[20:]
                        lines_out.append(new_line)
                        continue
            lines_out.append(line)

    if result["unl_count"] > 0 and not target_residue:
        result["success"] = False
        result["errors"].append(
            "Ambiguous UNL residue repair: input PDB contains UNL but multiple ligand "
            f"templates are present ({unique_ligand_names}). Refusing to guess."
        )
        return result
    
    with open(output_path, 'w') as f:
        f.writelines(lines_out)
    
    if result["unl_count"] > 0:
        result["replacements"].append(f"Replaced {result['unl_count']} UNL atoms with {target_residue}")
        logger.info(f"Fixed {result['unl_count']} UNL residue atoms -> {target_residue}")

    return result


def fix_histidine_protonation_consistency(pdb_path: Path, output_path: Path) -> dict:
    """Fix inconsistent HIS residue names vs present hydrogen atom names.

    Amber/OpenMM residue template matching at openmmforcefields build time
    requires the residue name to agree with the hydrogen atoms that are
    present (e.g. a residue named HIE that still carries HD1 fails to
    match any HIS template). This can happen when upstream tools relabel
    residues but keep their original hydrogen names.

    Rules (Amber):
    - HID: delta-protonated -> has HD1 (and typically no HE2)
    - HIE: epsilon-protonated -> has HE2 (and typically no HD1)
    - HIP: doubly protonated -> has both HD1 and HE2

    This function rewrites residue names to match present atom names.
    It does NOT add/remove atoms; it only changes residue name columns.
    """
    result = {"changed": 0, "changes": []}

    # First pass: collect per-residue whether HD1/HE2 are present
    residues: dict[tuple[str, str, str], dict[str, bool]] = {}
    lines = pdb_path.read_text().splitlines(keepends=True)
    for line in lines:
        if not line.startswith(("ATOM", "HETATM")):
            continue
        resname = line[17:20].strip().upper()
        if resname not in {"HIS", "HID", "HIE", "HIP"}:
            continue
        chain = line[21:22]
        resnum = line[22:26]
        icode = line[26:27]
        key = (chain, resnum, icode)
        atom = line[12:16].strip().upper()
        flags = residues.setdefault(key, {"hd1": False, "he2": False, "resname": resname})
        if atom == "HD1":
            flags["hd1"] = True
        elif atom == "HE2":
            flags["he2"] = True

    # Determine desired residue names
    desired: dict[tuple[str, str, str], str] = {}
    for key, flags in residues.items():
        hd1 = bool(flags.get("hd1"))
        he2 = bool(flags.get("he2"))
        current = str(flags.get("resname", "HIS")).upper()
        target = current
        if hd1 and he2:
            target = "HIP"
        elif hd1 and not he2:
            target = "HID"
        elif he2 and not hd1:
            target = "HIE"
        # If neither hydrogen present, leave as-is (HIS/HID/HIE from upstream)
        if target != current:
            desired[key] = target

    # Second pass: rewrite residue name field for matching residues.
    # Filter on resname too — packmol-memgen reuses chain IDs and residue
    # numbers for waters and ions, so a water molecule can share
    # (chain, resnum, icode) with a HIS residue. Without the resname
    # guard, the water's resname would be silently renamed to HID/HIE/HIP
    # too, breaking residue template matching at openmmforcefields build time.
    _his_family = {"HIS", "HID", "HIE", "HIP"}
    out_lines: list[str] = []
    for line in lines:
        if line.startswith(("ATOM", "HETATM")):
            resname_cur = line[17:20].strip().upper()
            if resname_cur in _his_family:
                chain = line[21:22]
                resnum = line[22:26]
                icode = line[26:27]
                key = (chain, resnum, icode)
                if key in desired:
                    old = line[17:20]
                    new = f"{desired[key]:>3}"
                    if old != new:
                        result["changed"] += 1
                        result["changes"].append(f"{chain.strip() or '_'}:{resnum.strip()}{icode.strip() or ''} {old.strip()} -> {new.strip()}")
                        line = line[:17] + new + line[20:]
        out_lines.append(line)

    output_path.write_text("".join(out_lines))
    return result


def strip_crystal_waters(input_pdb: Path, output_pdb: Path) -> dict:
    """Remove crystal water molecules from a PDB file.

    This function removes all water residues (HOH, WAT, SOL, etc.) from the PDB.
    Crystal waters should be removed for both implicit and explicit solvent simulations:
    - Implicit: GB models don't support discrete water molecules
    - Explicit: Bulk water will be added by solvate_structure

    Args:
        input_pdb: Path to input PDB file
        output_pdb: Path to output PDB file (can be same as input)

    Returns:
        dict with:
            - success: bool
            - waters_removed: int - Number of water residues removed
            - atoms_removed: int - Number of water atoms removed
    """
    water_names = {"WAT", "HOH", "SOL", "TP3", "OPC", "T4P", "T5P", "H2O", "DOD", "D2O"}

    result = {
        "success": False,
        "waters_removed": 0,
        "atoms_removed": 0,
    }

    try:
        lines_to_keep = []
        water_residues = set()
        atoms_removed = 0

        with open(input_pdb, 'r') as f:
            for line in f:
                if line.startswith(('ATOM', 'HETATM')):
                    res_name = line[17:20].strip()
                    if res_name in water_names:
                        # Track water residue
                        res_num = line[22:26].strip()
                        chain = line[21:22]
                        water_residues.add(f"{chain}:{res_num}")
                        atoms_removed += 1
                        continue  # Skip this line
                lines_to_keep.append(line)

        with open(output_pdb, 'w') as f:
            f.writelines(lines_to_keep)

        result["success"] = True
        result["waters_removed"] = len(water_residues)
        result["atoms_removed"] = atoms_removed

        if len(water_residues) > 0:
            logger.info(f"Stripped {len(water_residues)} crystal water(s) ({atoms_removed} atoms) from PDB")

    except Exception as e:
        logger.error(f"Failed to strip crystal waters: {e}")
        result["errors"] = [str(e)]

    return result
