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

from mdclaw._common import (  # noqa: E402
    ensure_directory, BaseToolWrapper,
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


PROTEIN_FORCEFIELDS = {
    "ff14SB": "leaprc.protein.ff14SB",
    "ff19SB": "leaprc.protein.ff19SB",
    "ff14sb": "leaprc.protein.ff14SB",
    "ff19sb": "leaprc.protein.ff19SB",
    # Implicit solvent specific
    "ff14SBonlysc": "leaprc.protein.ff14SBonlysc",
    "ff14sbonlysc": "leaprc.protein.ff14SBonlysc",
}

# Phosphorylated-residue (SEP/TPO/PTR) libraries paired by Amber convention to
# each protein forcefield. `phosaa19SB` was the Amber 2020+ refit for ff19SB;
# `phosaa14SB` is the ff14SB-compatible version; `phosaa10` is the older
# generic library and is used as a fallback.


PHOSAA_LIBRARY_FOR_FF = {
    "ff19SB": "leaprc.phosaa19SB",
    "ff14SB": "leaprc.phosaa14SB",
    "ff14SBonlysc": "leaprc.phosaa14SB",
}


_GLYCAM_TOPOLOGY_RESNAMES = {
    "0YB", "4YA", "4YB", "0LB", "VMB", "0MB", "0FA", "2MA", "0LA",
    "BMA", "MAN", "NAG", "0YA", "4YS", "0LS",
}


_GLYCAM_LINKED_ASN_RESNAME = "NLN"


WATER_FORCEFIELDS = {
    "tip3p": "leaprc.water.tip3p",
    "opc": "leaprc.water.opc",
    "opc3": "leaprc.water.opc3",
    "tip4pew": "leaprc.water.tip4pew",
    "spce": "leaprc.water.spce",
    # Case-insensitive aliases
    "TIP3P": "leaprc.water.tip3p",
    "OPC": "leaprc.water.opc",
    "OPC3": "leaprc.water.opc3",
    "TIP4PEW": "leaprc.water.tip4pew",
    "SPCE": "leaprc.water.spce",
    "SPC/E": "leaprc.water.spce",
}

# Ion parameters per water model (Amber Manual recommendations)
# - TIP3P, TIP4PEW: Joung-Cheatham parameters
# - OPC: Li-Merz 12-6 HFE set (best for OPC)
# - OPC3: Li-Merz 12-6 normal usage set
# - SPC/E: Joung-Cheatham parameters


WATER_ION_PARAMS = {
    "tip3p": "frcmod.ionsjc_tip3p",
    "opc": "frcmod.ionslm_hfe_opc",  # Li-Merz HFE set recommended for OPC
    "opc3": "frcmod.ionslm_126_opc3",
    "tip4pew": "frcmod.ionsjc_tip4pew",
    "spce": "frcmod.ionsjc_spce",
}


STANDARD_PROTEIN_RESIDUES = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "CYX", "GLN", "GLU", "GLY", "HIS",
    "HID", "HIE", "HIP", "ILE", "LEU", "LYS", "MET", "PHE", "PRO", "SER",
    "THR", "TRP", "TYR", "VAL",
}


WATER_RESIDUES = {"HOH", "WAT", "H2O", "TIP", "TIP3", "OPC"}


POLYPHOSPHATE_LIGANDS = {"AP5", "ATP", "ADP", "AMP", "GTP", "GDP", "NAD", "NAP"}


NUCLEIC_FORCEFIELDS = {
    "dna": "leaprc.DNA.OL15",
    "rna": "leaprc.RNA.OL3",
}


GLYCAN_FORCEFIELDS = {
    "auto": "leaprc.GLYCAM_06j-1",
    "glycam06j": "leaprc.GLYCAM_06j-1",
    "glycam_06j": "leaprc.GLYCAM_06j-1",
    "glycam06j-1": "leaprc.GLYCAM_06j-1",
    "glycam_06j-1": "leaprc.GLYCAM_06j-1",
    "GLYCAM_06j-1": "leaprc.GLYCAM_06j-1",
}


GLYCAM_LINKED_PROTEIN_RESNAMES = {"NLN", "OLS", "OLT", "OLP", "HYP"}

# =============================================================================
# Force Field Compatibility (based on Amber Manual 2024)
# =============================================================================
# ff19SB was developed with OPC water and is strongly recommended to use with OPC.
# The Amber manual explicitly warns against using ff19SB with TIP3P.


FORCEFIELD_WATER_COMPATIBILITY = {
    "ff19SB": {
        "recommended": ["opc"],  # Amber manual: "strongly recommend using ff19SB with OPC"
        "acceptable": ["opc3", "tip4pew"],
        "not_recommended": ["tip3p"],  # Amber manual: "TIP3P has serious limitations with ff19SB"
    },
    "ff14SB": {
        "recommended": ["tip3p", "opc", "tip4pew"],
        "acceptable": ["opc3", "spce"],
        "not_recommended": [],
    },
    "ff14SBonlysc": {
        # For implicit solvent (GB), ff14SBonlysc with igb=8 is recommended
        "recommended": [],  # Typically used with implicit solvent
        "acceptable": ["tip3p", "opc", "tip4pew"],
        "not_recommended": [],
    },
}

# Recommended combinations for different simulation types


RECOMMENDED_COMBINATIONS = {
    "explicit_protein": {
        "forcefield": "ff19SB",
        "water_model": "opc",
        "reason": "Amber manual strongly recommends ff19SB with OPC water"
    },
    "explicit_legacy": {
        "forcefield": "ff14SB",
        "water_model": "tip3p",
        "reason": "Well-tested combination for backward compatibility"
    },
    "implicit_protein": {
        "forcefield": "ff14SBonlysc",
        "gb_model": "igb=8",
        "radii": "mbondi3",
        "reason": "Best GB results with GBneck2 model"
    },
    "membrane": {
        "forcefield": "ff19SB",
        "water_model": "opc",
        "lipid_ff": "lipid21",
        "reason": "lipid21 is the recommended lipid force field"
    },
}


CANONICAL_PROTEIN_FORCEFIELDS = {
    "ff14sb": "ff14SB",
    "ff19sb": "ff19SB",
    "ff14sbonlysc": "ff14SBonlysc",
}

# =============================================================================
# Helper Functions
# =============================================================================
