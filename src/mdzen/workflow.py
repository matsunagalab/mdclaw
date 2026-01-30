"""Workflow step definitions for MDZen.

This module is the single source of truth for all workflow step configurations.
All step-related information is accessed through STEP_CONFIG.
"""

from typing import TypedDict


class StepConfig(TypedDict):
    """Configuration for a single workflow step."""

    tool: str  # Primary MCP tool name for this step
    inputs: str  # Human-readable description of required inputs
    servers: list[str]  # MCP servers needed for this step
    allowed_tools: list[str]  # All tool names allowed during this step
    estimate: str  # Time estimate for user feedback


# Ordered list of workflow steps (explicit solvent - default)
SETUP_STEPS: list[str] = [
    "prepare_complex",
    "solvate",
    "build_topology",
    "run_simulation",
]

# Steps for implicit solvent (skip solvate)
SETUP_STEPS_IMPLICIT: list[str] = [
    "prepare_complex",
    "build_topology",
    "run_simulation",
]


def get_steps_for_solvation_type(solvation_type: str) -> list[str]:
    """Return workflow steps based on solvation type.

    Args:
        solvation_type: "explicit" or "implicit"

    Returns:
        List of step names for the workflow
    """
    if solvation_type == "implicit":
        return SETUP_STEPS_IMPLICIT
    return SETUP_STEPS

# Centralized step configuration
STEP_CONFIG: dict[str, StepConfig] = {
    "prepare_complex": {
        "tool": "prepare_complex",
        "inputs": "Requires: PDB ID or structure file",
        "servers": ["research", "structure", "genesis"],
        "allowed_tools": ["prepare_complex", "download_structure", "get_alphafold_structure", "predict_structure"],
        "estimate": "1-5 minutes",
    },
    "solvate": {
        "tool": "solvate_structure",
        "inputs": "Requires: merged_pdb from outputs['merged_pdb']",
        "servers": ["solvation"],
        "allowed_tools": ["solvate_structure", "embed_in_membrane"],
        "estimate": "2-10 minutes (membrane: 10-30 minutes)",
    },
    "build_topology": {
        "tool": "build_amber_system",
        "inputs": "Requires: solvated_pdb, box_dimensions",
        "servers": ["amber", "metal"],
        "allowed_tools": ["build_amber_system", "parameterize_metal_ion", "detect_metal_ions"],
        "estimate": "1-3 minutes (with metals: 3-5 minutes)",
    },
    "run_simulation": {
        "tool": "run_md_simulation",
        "inputs": "Requires: parm7, rst7",
        "servers": ["md_simulation"],
        "allowed_tools": ["run_md_simulation"],
        "estimate": "5-60 minutes (depends on simulation_time)",
    },
}

# =============================================================================
# Workflow v2: (1)→(2)→(3)→(4)→(quick_md)→(validation)
# =============================================================================


class WorkflowV2StepConfig(TypedDict):
    """Configuration for a single step in the v2 stepwise workflow.

    Notes:
    - This workflow is designed for stepwise prompts + shared scratchpad state.
    - `tool` is informational only; some steps can call multiple tools.
    """

    tool: str
    inputs: str
    required_state: list[str]  # Required keys in workflow_state/scratchpad
    outputs: list[str]  # Keys expected to be produced by this step
    servers: list[str]  # MCP servers needed for this step
    allowed_tools: list[str]  # All tool names allowed during this step
    estimate: str


# Primary workflow steps for MDZen (new default)
WORKFLOW_STEPS: list[str] = [
    "acquire_structure",
    "select_prepare",
    "structure_decisions",
    "solvate_or_membrane",
    "quick_md",
    "validation",
]


WORKFLOW_STEP_CONFIG: dict[str, WorkflowV2StepConfig] = {
    "acquire_structure": {
        "tool": "multi",
        "inputs": "Requires: user request (PDB/UniProt/FASTA/SMILES)",
        "required_state": [],
        "outputs": ["structure_file"],
        "servers": ["research", "genesis"],
        "allowed_tools": [
            # research
            "search_structures",
            "get_structure_info",
            "download_structure",
            "get_alphafold_structure",
            "search_proteins",
            "get_protein_info",
            # genesis
            "boltz2_protein_from_seq",
            "rdkit_validate_smiles",
            "pubchem_get_smiles_from_name",
        ],
        "estimate": "10-60 seconds (Boltz: minutes)",
    },
    "select_prepare": {
        "tool": "prepare_complex",
        "inputs": "Requires: structure_file from acquire_structure",
        "required_state": ["structure_file"],
        "outputs": ["merged_pdb"],
        "servers": ["research", "structure"],
        "allowed_tools": [
            # research
            "inspect_molecules",
            # structure
            "split_molecules",
            "merge_structures",
            "prepare_complex",
        ],
        "estimate": "1-5 minutes",
    },
    "structure_decisions": {
        "tool": "multi",
        "inputs": "Requires: merged_pdb from select_prepare",
        "required_state": ["merged_pdb"],
        "outputs": ["structure_analysis", "merged_pdb"],
        "servers": ["research", "structure"],
        "allowed_tools": [
            # research
            "analyze_structure_details",
            # structure
            "prepare_complex",
        ],
        "estimate": "30-120 seconds (+ optional re-prepare)",
    },
    "solvate_or_membrane": {
        "tool": "solvate_structure",
        "inputs": "Requires: merged_pdb and solvation_type decision",
        "required_state": ["merged_pdb"],
        "outputs": ["solvated_pdb", "box_dimensions"],
        "servers": ["solvation"],
        "allowed_tools": [
            "solvate_structure",
            "embed_in_membrane",
            "list_available_lipids",
        ],
        "estimate": "2-10 minutes (membrane: 10-30 minutes)",
    },
    "quick_md": {
        "tool": "run_md_simulation",
        "inputs": "Requires: solvated_pdb (or membrane_pdb) and box_dimensions (explicit solvent)",
        "required_state": ["solvated_pdb"],
        "outputs": ["parm7", "rst7", "trajectory"],
        "servers": ["amber", "md_simulation"],
        "allowed_tools": [
            "build_amber_system",
            "run_md_simulation",
        ],
        "estimate": "5-20 minutes (quick)",
    },
    "validation": {
        "tool": "run_validation_tool",
        "inputs": "Requires: outputs from quick_md",
        "required_state": ["parm7", "rst7"],
        "outputs": ["validation_result"],
        "servers": [],
        "allowed_tools": [
            # FunctionTool (not MCP)
            "run_validation_tool",
        ],
        "estimate": "5-30 seconds",
    },
}

# Default parameters for Step (5) quick_md.
QUICK_MD_DEFAULTS: dict[str, object] = {
    "forcefield": "ff19SB",
    "water_model": "opc",
    "simulation_time_ns": 0.1,
    "temperature_kelvin": 300.0,
    "pressure_bar": 1.0,
    "timestep_fs": 2.0,
    "output_frequency_ps": 10.0,
    "trajectory_format": "dcd",
}


# =============================================================================
# Helper functions
# =============================================================================


def get_step_config(step: str) -> StepConfig:
    """Get configuration for a workflow step.

    Args:
        step: Step name

    Returns:
        StepConfig dictionary

    Raises:
        ValueError: If step name is not recognized
    """
    if step not in STEP_CONFIG:
        valid_steps = list(STEP_CONFIG.keys())
        raise ValueError(f"Unknown step '{step}'. Valid steps: {valid_steps}")
    return STEP_CONFIG[step]


def get_workflow_v2_step_config(step: str) -> WorkflowV2StepConfig:
    """Get configuration for a v2 workflow step."""
    if step not in WORKFLOW_STEP_CONFIG:
        valid_steps = list(WORKFLOW_STEP_CONFIG.keys())
        raise ValueError(f"Unknown v2 step '{step}'. Valid steps: {valid_steps}")
    return WORKFLOW_STEP_CONFIG[step]


def get_next_workflow_v2_step(current_step: str | None) -> str | None:
    """Return the next step name in WORKFLOW_STEPS, or None if finished."""
    if current_step is None:
        return WORKFLOW_STEPS[0] if WORKFLOW_STEPS else None
    try:
        idx = WORKFLOW_STEPS.index(current_step)
    except ValueError:
        return None
    if idx + 1 >= len(WORKFLOW_STEPS):
        return None
    return WORKFLOW_STEPS[idx + 1]


def validate_workflow_v2_state(step: str, workflow_state: dict) -> tuple[bool, list[str]]:
    """Validate that required state keys for the given v2 step exist."""
    cfg = get_workflow_v2_step_config(step)
    missing = [k for k in cfg["required_state"] if not workflow_state.get(k)]
    return (len(missing) == 0), missing


# Prerequisites for each step (output keys required from previous steps)
# Note: For IMPLICIT solvent, build_topology needs merged_pdb (not solvated_pdb)
STEP_PREREQUISITES: dict[str, list[str]] = {
    "prepare_complex": [],
    "solvate": ["merged_pdb"],
    "build_topology": ["solvated_pdb", "box_dimensions"],  # Explicit solvent
    "run_simulation": ["parm7", "rst7"],
}

# Prerequisites for implicit solvent workflow
STEP_PREREQUISITES_IMPLICIT: dict[str, list[str]] = {
    "prepare_complex": [],
    "build_topology": ["merged_pdb"],  # Use merged_pdb directly (no solvate step)
    "run_simulation": ["parm7", "rst7"],
}


def get_prerequisites(step: str, solvation_type: str = "explicit") -> list[str]:
    """Get prerequisites for a step based on solvation type.

    Args:
        step: Step name
        solvation_type: "explicit" or "implicit"

    Returns:
        List of required output keys
    """
    if solvation_type == "implicit":
        return STEP_PREREQUISITES_IMPLICIT.get(step, [])
    return STEP_PREREQUISITES.get(step, [])


def validate_step_prerequisites(
    step: str,
    outputs: dict,
    solvation_type: str = "explicit"
) -> tuple[bool, list[str]]:
    """Validate that prerequisites for a step are met.

    Args:
        step: Step name (e.g., "solvate")
        outputs: Current outputs dictionary
        solvation_type: "explicit" or "implicit"

    Returns:
        Tuple of (is_valid, list of missing requirements)
    """
    # Compatibility aliases:
    # - Historically tests and some callers use "prmtop" key for Amber topology.
    # - Internally MDZen often uses "parm7".
    # Treat either as satisfying the topology prerequisite.
    if step == "run_simulation":
        missing: list[str] = []
        if not (outputs.get("parm7") or outputs.get("prmtop")):
            missing.append("prmtop (or parm7)")
        if not outputs.get("rst7"):
            missing.append("rst7")
        return (len(missing) == 0), missing

    prereqs = get_prerequisites(step, solvation_type)
    missing = [key for key in prereqs if key not in outputs]
    return len(missing) == 0, missing


def get_current_step_info(
    completed_steps: list,
    solvation_type: str = "explicit"
) -> dict:
    """Get information about the current workflow step.

    Args:
        completed_steps: List of completed step names (may have duplicates)
        solvation_type: "explicit" or "implicit" (determines step sequence)

    Returns:
        Dictionary with current_step, next_tool, step_index, and input_requirements
    """
    # Get steps for this solvation type
    steps = get_steps_for_solvation_type(solvation_type)

    # Deduplicate completed steps
    completed_set = set(completed_steps)

    # Find first incomplete step in order
    for i, step in enumerate(steps):
        if step not in completed_set:
            cfg = STEP_CONFIG[step]
            return {
                "current_step": step,
                "next_tool": cfg["tool"],
                "step_index": i + 1,
                "total_steps": len(steps),
                "input_requirements": cfg["inputs"],
                "is_complete": False,
                "solvation_type": solvation_type,
            }

    # All steps completed
    return {
        "current_step": None,
        "next_tool": None,
        "step_index": len(steps),
        "total_steps": len(steps),
        "input_requirements": "",
        "is_complete": True,
        "solvation_type": solvation_type,
    }


__all__ = [
    # Primary configuration
    "SETUP_STEPS",
    "SETUP_STEPS_IMPLICIT",
    "STEP_CONFIG",
    "STEP_PREREQUISITES",
    "STEP_PREREQUISITES_IMPLICIT",
    "StepConfig",
    # Workflow v2 configuration
    "WORKFLOW_STEPS",
    "WORKFLOW_STEP_CONFIG",
    "WorkflowV2StepConfig",
    "QUICK_MD_DEFAULTS",
    # Helper functions
    "get_step_config",
    "get_steps_for_solvation_type",
    "get_prerequisites",
    "validate_step_prerequisites",
    "get_current_step_info",
    "get_workflow_v2_step_config",
    "get_next_workflow_v2_step",
    "validate_workflow_v2_state",
]
