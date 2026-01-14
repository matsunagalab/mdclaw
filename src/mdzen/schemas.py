"""Pydantic schemas for MCP-MD ADK.

Defines the core data models used throughout the MD setup workflow.
"""

from typing import Optional
from pydantic import BaseModel, Field


# =============================================================================
# Structure Analysis Models (Phase 1 detailed analysis)
# =============================================================================


class DisulfideBondSpec(BaseModel):
    """User-editable disulfide bond specification.

    Detected in Phase 1, can be modified by user before Phase 2 execution.
    """

    chain1: str = Field(..., description="Chain ID of first cysteine")
    resnum1: int = Field(..., description="Residue number of first cysteine")
    chain2: str = Field(..., description="Chain ID of second cysteine")
    resnum2: int = Field(..., description="Residue number of second cysteine")
    distance_angstrom: Optional[float] = Field(
        None, description="S-S distance in Angstroms"
    )
    form_bond: bool = Field(
        True, description="Whether to form this disulfide bond (user can set to False)"
    )


class HistidineStateSpec(BaseModel):
    """User-editable histidine protonation state specification.

    Detected in Phase 1 with pKa estimation, can be modified by user.
    States: HID (δ-protonated), HIE (ε-protonated), HIP (doubly protonated)
    """

    chain: str = Field(..., description="Chain ID")
    resnum: int = Field(..., description="Residue number")
    state: str = Field(
        ...,
        description="Protonation state: 'HID', 'HIE', 'HIP', or 'HIS' (auto)",
    )
    estimated_pka: Optional[float] = Field(
        None, description="Estimated pKa from propka"
    )
    user_specified: bool = Field(
        False, description="True if user manually changed this state"
    )


class MissingResidueHandling(BaseModel):
    """User-editable missing residue handling specification."""

    chain: str = Field(..., description="Chain ID")
    start_resnum: int = Field(..., description="Start residue number")
    end_resnum: int = Field(..., description="End residue number")
    location: str = Field(
        ..., description="Location: 'N-terminal', 'C-terminal', 'internal'"
    )
    action: str = Field(
        "ignore",
        description="Action: 'ignore', 'model' (add missing), 'cap' (add caps)",
    )


class LigandSpec(BaseModel):
    """User-editable ligand processing specification."""

    chain: str = Field(..., description="Chain ID containing the ligand")
    resname: str = Field(..., description="Residue name (e.g., 'ATP', 'LIG')")
    include: bool = Field(True, description="Include this ligand in simulation")
    smiles: Optional[str] = Field(
        None, description="User-specified SMILES (overrides auto-detection)"
    )
    net_charge: Optional[int] = Field(
        None, description="User-specified net charge (overrides estimation)"
    )
    estimated_charge: Optional[int] = Field(
        None, description="Auto-estimated charge at simulation pH"
    )
    charge_method: str = Field("bcc", description="Charge method: 'bcc' or 'gas'")
    atom_type: str = Field("gaff2", description="Atom type: 'gaff' or 'gaff2'")


class NonstandardResidueSpec(BaseModel):
    """Non-standard residue handling specification."""

    chain: str = Field(..., description="Chain ID")
    resnum: int = Field(..., description="Residue number")
    resname: str = Field(..., description="Non-standard residue name (e.g., 'MSE')")
    standard_equivalent: Optional[str] = Field(
        None, description="Standard equivalent (e.g., 'MET' for MSE)"
    )
    action: str = Field(
        "replace", description="Action: 'replace', 'keep', 'remove'"
    )


class StructureAnalysis(BaseModel):
    """Structure analysis results from Phase 1.

    Contains detailed structural information detected during Phase 1,
    with user-editable recommendations that are passed to Phase 2.
    """

    # Analysis metadata
    analysis_performed: bool = Field(
        False, description="Whether detailed analysis was performed"
    )
    analysis_ph: float = Field(
        7.4, description="pH used for protonation analysis"
    )
    structure_file: Optional[str] = Field(
        None, description="Structure file that was analyzed"
    )

    # Disulfide bonds (user-editable)
    disulfide_bonds: list[DisulfideBondSpec] = Field(
        default_factory=list,
        description="Detected disulfide bond candidates with user choices",
    )

    # Histidine states (user-editable)
    histidine_states: list[HistidineStateSpec] = Field(
        default_factory=list,
        description="Histidine protonation states with user choices",
    )

    # Missing residue handling (user-editable)
    missing_residue_handling: list[MissingResidueHandling] = Field(
        default_factory=list,
        description="Missing residue segments with handling choices",
    )

    # Non-standard residue handling
    nonstandard_residues: list[NonstandardResidueSpec] = Field(
        default_factory=list,
        description="Non-standard residues with handling choices",
    )
    replace_nonstandard: bool = Field(
        True, description="Replace non-standard residues with standard equivalents"
    )

    # Ligand specifications (user-editable)
    ligands: list[LigandSpec] = Field(
        default_factory=list,
        description="Ligand processing specifications with user choices",
    )

    # Ligand selection by unique ID (user-editable)
    include_ligand_ids: Optional[list[str]] = Field(
        None,
        description="List of ligand unique IDs to include (format: 'chain:resname:resnum', e.g., ['A:ACP:501']). If set, only these ligands are processed.",
    )
    exclude_ligand_ids: Optional[list[str]] = Field(
        None,
        description="List of ligand unique IDs to exclude (format: 'chain:resname:resnum', e.g., ['A:ACT:401']). These ligands are skipped.",
    )

    # Terminal capping
    cap_termini: bool = Field(
        False, description="Add ACE/NME caps to termini"
    )
    cap_termini_chains: list[str] = Field(
        default_factory=list,
        description="Specific chains to cap (empty = all chains)",
    )


# =============================================================================
# Simulation Brief (main workflow schema)
# =============================================================================


class SimulationBrief(BaseModel):
    """Structured MD simulation setup parameters.

    This schema captures all information needed to set up an MD simulation,
    gathered during the clarification phase.
    """

    # Structure source
    pdb_id: Optional[str] = Field(
        None,
        description="PDB ID to fetch (e.g., '1AKE')"
    )
    fasta_sequence: Optional[str] = Field(
        None,
        description="FASTA sequence for Boltz-2 prediction"
    )
    structure_file: Optional[str] = Field(
        None,
        description="Path to local structure file"
    )

    # Chain selection
    select_chains: Optional[list[str]] = Field(
        None,
        description="Chain IDs to process (e.g., ['A', 'B'])"
    )

    # Ligand information
    ligand_smiles: Optional[dict[str, str]] = Field(
        None,
        description="Manual SMILES for ligands {'LIG1': 'SMILES_string'}"
    )
    charge_method: str = Field(
        "bcc",
        description="Ligand charge method ('bcc' or 'gas')"
    )
    atom_type: str = Field(
        "gaff2",
        description="Ligand atom type ('gaff' or 'gaff2')"
    )

    # Component selection
    include_types: Optional[list[str]] = Field(
        None,
        description="Components to include ['protein', 'ligand', 'ion', 'water']"
    )
    ph: float = Field(
        7.0,
        description="pH value for protonation"
    )
    cap_termini: bool = Field(
        False,
        description="Add ACE/NME caps to protein termini"
    )

    # Box parameters
    box_padding: float = Field(
        12.0,
        description="Box padding distance in Angstroms"
    )
    cubic_box: bool = Field(
        True,
        description="Use cubic box (True) or rectangular (False)"
    )
    salt_concentration: float = Field(
        0.15,
        description="Salt concentration in M"
    )
    cation_type: str = Field(
        "Na+",
        description="Cation type for neutralization"
    )
    anion_type: str = Field(
        "Cl-",
        description="Anion type for neutralization"
    )

    # Membrane settings
    is_membrane: bool = Field(
        False,
        description="Whether this is a membrane system"
    )
    lipids: Optional[str] = Field(
        None,
        description="Lipid composition for membrane (e.g., 'POPC')"
    )
    lipid_ratio: Optional[str] = Field(
        None,
        description="Lipid ratio (e.g., '3:1')"
    )

    # Force field (Amber Manual 2024 recommendations)
    force_field: str = Field(
        "ff19SB",
        description="Protein force field (ff19SB recommended with OPC water)"
    )
    water_model: str = Field(
        "opc",
        description="Water model (OPC strongly recommended with ff19SB)"
    )

    # Simulation parameters
    temperature: float = Field(
        300.0,
        description="Simulation temperature in K"
    )
    pressure_bar: Optional[float] = Field(
        1.0,
        description="Pressure in bar (None for NVT)"
    )
    timestep: float = Field(
        2.0,
        description="Integration timestep in fs"
    )
    simulation_time_ns: float = Field(
        1.0,
        description="Total simulation time in ns"
    )
    minimize_steps: int = Field(
        500,
        description="Energy minimization iterations"
    )
    nonbonded_cutoff: float = Field(
        10.0,
        description="Nonbonded interaction cutoff in Angstroms"
    )
    constraints: str = Field(
        "HBonds",
        description="Bond constraints ('HBonds', 'AllBonds', or 'None')"
    )
    output_frequency_ps: float = Field(
        10.0,
        description="Trajectory output interval in ps"
    )

    # Boltz-2 settings
    use_boltz2_docking: bool = Field(
        True,
        description="Use Boltz-2 for docking"
    )
    use_msa: bool = Field(
        True,
        description="Use MSA server for Boltz-2 predictions"
    )
    num_models: int = Field(
        5,
        description="Number of Boltz-2 models to generate"
    )

    # Output settings
    output_formats: Optional[list[str]] = Field(
        None,
        description="Output formats (default: ['topology'])"
    )

    # Structure analysis from Phase 1 (detailed structural information)
    structure_analysis: Optional[StructureAnalysis] = Field(
        None,
        description="Detailed structure analysis with user-approved choices",
    )


__all__ = [
    "DisulfideBondSpec",
    "HistidineStateSpec",
    "MissingResidueHandling",
    "LigandSpec",
    "NonstandardResidueSpec",
    "StructureAnalysis",
    "SimulationBrief",
]
