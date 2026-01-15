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
    # Helper functions
    "get_step_config",
    "get_steps_for_solvation_type",
    "get_prerequisites",
    "validate_step_prerequisites",
    "get_current_step_info",
]
