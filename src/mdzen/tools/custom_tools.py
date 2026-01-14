"""Custom FunctionTools for MDZen.

This module defines custom tools that wrap Python functions for use
with ADK's LlmAgent.
"""

from typing import Optional

from google.adk.tools import ToolContext

from mdzen.schemas import SimulationBrief, StructureAnalysis
from mdzen.utils import safe_dict, safe_list
from mdzen.workflow import (
    SETUP_STEPS,
    STEP_CONFIG,
    get_current_step_info,
    validate_step_prerequisites,
)


def _normalize_null(value):
    """Convert LLM 'null' strings to Python None.

    LLMs sometimes pass the string 'null' instead of Python None.
    This helper normalizes those values.
    """
    if value is None:
        return None
    if isinstance(value, str) and value.lower() in ("null", "none", ""):
        return None
    return value


# =============================================================================
# PHASE 1: CLARIFICATION TOOLS
# =============================================================================


def get_session_dir(tool_context: ToolContext) -> str:
    """Get the current session directory path.

    Use this to get the output directory for saving downloaded files.
    Pass this path as the output_dir parameter to download_structure
    and other tools.

    Returns:
        Absolute path to the session directory (job_XXXXXXXX format)
    """
    session_dir = tool_context.state.get("session_dir", "")
    return str(session_dir) if session_dir else ""


def generate_simulation_brief(
    pdb_id: Optional[str] = None,
    fasta_sequence: Optional[str] = None,
    select_chains: Optional[list[str]] = None,
    ligand_smiles: Optional[dict[str, str]] = None,
    charge_method: str = "bcc",
    atom_type: str = "gaff2",
    include_types: Optional[list[str]] = None,
    ph: float = 7.0,
    cap_termini: bool = False,
    box_padding: float = 12.0,
    cubic_box: bool = True,
    salt_concentration: float = 0.15,
    cation_type: str = "Na+",
    anion_type: str = "Cl-",
    is_membrane: bool = False,
    lipids: Optional[str] = None,
    lipid_ratio: Optional[str] = None,
    force_field: str = "ff19SB",
    water_model: str = "tip3p",
    temperature: float = 300.0,
    pressure_bar: Optional[float] = 1.0,
    timestep: float = 2.0,
    simulation_time_ns: float = 1.0,
    minimize_steps: int = 500,
    nonbonded_cutoff: float = 10.0,
    constraints: str = "HBonds",
    output_frequency_ps: float = 10.0,
    use_boltz2_docking: bool = True,
    use_msa: bool = True,
    num_models: int = 5,
    output_formats: Optional[list[str]] = None,
    structure_analysis: Optional[dict] = None,
    tool_context: ToolContext = None,  # ADK injects this automatically
) -> dict:
    """Generate a structured SimulationBrief from gathered requirements.

    This tool creates a complete SimulationBrief with all MD setup parameters.
    Call this when you have gathered enough information from the user.
    The result is automatically saved to session state.

    Args:
        pdb_id: PDB ID to fetch (e.g., "1AKE")
        fasta_sequence: FASTA sequence for de novo structure prediction
        select_chains: Chain IDs to process (e.g., ["A", "B"])
        ligand_smiles: Manual SMILES for ligands {"LIG1": "SMILES_string"}
        charge_method: Ligand charge method ("bcc" or "gas")
        atom_type: Ligand atom type ("gaff" or "gaff2")
        include_types: Components to include ["protein", "ligand", "ion", "water"]
        ph: pH value for protonation
        cap_termini: Add ACE/NME caps to protein termini
        box_padding: Box padding distance in Angstroms
        cubic_box: Use cubic box (True) or rectangular (False)
        salt_concentration: Salt concentration in M
        cation_type: Cation type for neutralization (e.g., "Na+")
        anion_type: Anion type for neutralization (e.g., "Cl-")
        is_membrane: Whether this is a membrane system
        lipids: Lipid composition for membrane (e.g., "POPC")
        lipid_ratio: Lipid ratio (e.g., "3:1")
        force_field: Protein force field (e.g., "ff19SB")
        water_model: Water model (e.g., "tip3p")
        temperature: Simulation temperature in K
        pressure_bar: Pressure in bar (None for NVT)
        timestep: Integration timestep in fs
        simulation_time_ns: Total simulation time in ns
        minimize_steps: Energy minimization iterations
        nonbonded_cutoff: Nonbonded interaction cutoff in Angstroms
        constraints: Bond constraints ("HBonds", "AllBonds", or "None")
        output_frequency_ps: Trajectory output interval in ps
        use_boltz2_docking: Use Boltz-2 for docking
        use_msa: Use MSA server for Boltz-2 predictions
        num_models: Number of Boltz-2 models to generate
        output_formats: Output formats (default: ["topology"])
        structure_analysis: Detailed structure analysis from Phase 1. Contains
            user-approved settings for disulfide bonds, histidine protonation,
            missing residue handling, and ligand processing. This is passed to
            Phase 2 for execution.
        tool_context: ADK ToolContext (automatically injected)

    Returns:
        Dictionary representation of SimulationBrief
    """
    # Normalize "null" strings from LLM to Python None
    pdb_id = _normalize_null(pdb_id)
    fasta_sequence = _normalize_null(fasta_sequence)
    ligand_smiles = _normalize_null(ligand_smiles)
    lipids = _normalize_null(lipids)
    lipid_ratio = _normalize_null(lipid_ratio)
    pressure_bar = _normalize_null(pressure_bar)

    # Default lipid composition for membrane systems
    if is_membrane and not lipids:
        lipids = "POPC"  # Default: mammalian membrane
        lipid_ratio = lipid_ratio or "1"

    # Parse structure_analysis if provided
    parsed_analysis = None
    if structure_analysis and isinstance(structure_analysis, dict):
        try:
            parsed_analysis = StructureAnalysis(**structure_analysis)
        except Exception:
            # If parsing fails, pass None and let Phase 2 auto-detect
            pass

    brief = SimulationBrief(
        pdb_id=pdb_id,
        fasta_sequence=fasta_sequence,
        select_chains=select_chains,
        ligand_smiles=ligand_smiles,
        charge_method=charge_method,
        atom_type=atom_type,
        include_types=include_types or ["protein", "ligand", "ion"],
        ph=ph,
        cap_termini=cap_termini,
        box_padding=box_padding,
        cubic_box=cubic_box,
        salt_concentration=salt_concentration,
        cation_type=cation_type,
        anion_type=anion_type,
        is_membrane=is_membrane,
        lipids=lipids,
        lipid_ratio=lipid_ratio,
        force_field=force_field,
        water_model=water_model,
        temperature=temperature,
        pressure_bar=pressure_bar,
        timestep=timestep,
        simulation_time_ns=simulation_time_ns,
        minimize_steps=minimize_steps,
        nonbonded_cutoff=nonbonded_cutoff,
        constraints=constraints,
        output_frequency_ps=output_frequency_ps,
        use_boltz2_docking=use_boltz2_docking,
        use_msa=use_msa,
        num_models=num_models,
        output_formats=output_formats or ["topology"],
        structure_analysis=parsed_analysis,
    )

    brief_dict = brief.model_dump()

    # Save to session state for Phase 2-3 to access
    if tool_context is not None:
        tool_context.state["simulation_brief"] = brief_dict

    # Generate user-friendly summary with all parameters and descriptions
    summary = _format_simulation_brief_summary(brief)

    return {
        "success": True,
        "brief": brief_dict,
        "summary": summary,
    }


def _format_simulation_brief_summary(brief: SimulationBrief) -> str:
    """Format SimulationBrief as a user-friendly summary with all parameters.

    Groups parameters into logical categories and includes descriptions
    for each parameter to help users understand the simulation setup.
    """
    lines = []
    lines.append("=" * 60)
    lines.append("SIMULATION BRIEF - All Parameters")
    lines.append("=" * 60)

    # 1. Structure Source
    lines.append("\n## 1. Structure Source")
    lines.append("-" * 40)
    if brief.pdb_id:
        lines.append(f"  pdb_id: {brief.pdb_id}")
        lines.append("    → Fetch structure from PDB")
    elif brief.fasta_sequence:
        seq_preview = brief.fasta_sequence[:50] + "..." if len(brief.fasta_sequence) > 50 else brief.fasta_sequence
        lines.append(f"  fasta_sequence: {seq_preview}")
        lines.append("    → Predict structure from FASTA using Boltz-2")
    elif brief.structure_file:
        lines.append(f"  structure_file: {brief.structure_file}")
        lines.append("    → Load structure from local file")
    else:
        lines.append("  (No structure source specified)")

    # 2. Chain Selection
    lines.append("\n## 2. Chain Selection")
    lines.append("-" * 40)
    chains = brief.select_chains if brief.select_chains else ["all chains"]
    lines.append(f"  select_chains: {chains}")
    lines.append("    → Chains to include in simulation")

    # 3. Component Selection
    lines.append("\n## 3. Component Selection")
    lines.append("-" * 40)
    include = brief.include_types if brief.include_types else ["protein", "ligand", "ion"]
    lines.append(f"  include_types: {include}")
    lines.append("    → protein, ligand, ion, water (crystal waters)")
    lines.append(f"  ph: {brief.ph}")
    lines.append("    → pH for protonation state determination")
    lines.append(f"  cap_termini: {brief.cap_termini}")
    lines.append("    → True = add ACE/NME caps to termini")

    # 4. Ligand Parameters
    lines.append("\n## 4. Ligand Parameters")
    lines.append("-" * 40)
    if brief.ligand_smiles:
        lines.append(f"  ligand_smiles: {brief.ligand_smiles}")
        lines.append("    → Manually specified SMILES")
    else:
        lines.append("  ligand_smiles: (auto-detect)")
    lines.append(f"  charge_method: {brief.charge_method}")
    lines.append("    → Charge method (bcc=AM1-BCC, gas=Gasteiger)")
    lines.append(f"  atom_type: {brief.atom_type}")
    lines.append("    → Atom type (gaff2 recommended)")

    # 5. Solvation Parameters
    lines.append("\n## 5. Solvation Parameters")
    lines.append("-" * 40)
    lines.append(f"  box_padding: {brief.box_padding} Å")
    lines.append("    → Distance from protein surface to box edge")
    lines.append(f"  cubic_box: {brief.cubic_box}")
    lines.append("    → True = cubic box, False = rectangular")
    lines.append(f"  salt_concentration: {brief.salt_concentration} M")
    lines.append("    → Salt concentration (physiological: 0.15M)")
    lines.append(f"  cation_type: {brief.cation_type}")
    lines.append("    → Cation species (Na+, K+, etc.)")
    lines.append(f"  anion_type: {brief.anion_type}")
    lines.append("    → Anion species (Cl-, etc.)")

    # 6. Membrane Settings (if applicable)
    if brief.is_membrane:
        lines.append("\n## 6. Membrane Parameters")
        lines.append("-" * 40)
        lines.append(f"  is_membrane: {brief.is_membrane}")
        lines.append("    → Membrane protein system")
        lines.append(f"  lipids: {brief.lipids or 'not specified'}")
        lines.append("    → Lipid type (POPC, DPPC, etc.)")
        lines.append(f"  lipid_ratio: {brief.lipid_ratio or 'not specified'}")
        lines.append("    → Lipid ratio")

    # 7. Force Field
    lines.append("\n## 7. Force Field")
    lines.append("-" * 40)
    lines.append(f"  force_field: {brief.force_field}")
    lines.append("    → Protein force field (ff19SB recommended)")
    lines.append(f"  water_model: {brief.water_model}")
    lines.append("    → Water model (tip3p, opc, spce, etc.)")

    # 8. Simulation Parameters
    lines.append("\n## 8. Simulation Parameters")
    lines.append("-" * 40)
    lines.append(f"  temperature: {brief.temperature} K")
    lines.append("    → Temperature (300K=room temp, 310K=body temp)")
    if brief.pressure_bar is not None:
        lines.append(f"  pressure_bar: {brief.pressure_bar} bar")
        lines.append("    → Pressure (NPT ensemble)")
    else:
        lines.append("  pressure_bar: None")
        lines.append("    → NVT ensemble (constant volume)")
    lines.append(f"  timestep: {brief.timestep} fs")
    lines.append("    → Integration timestep (2fs standard)")
    lines.append(f"  simulation_time_ns: {brief.simulation_time_ns} ns")
    lines.append("    → Simulation time")
    lines.append(f"  minimize_steps: {brief.minimize_steps}")
    lines.append("    → Energy minimization steps")
    lines.append(f"  nonbonded_cutoff: {brief.nonbonded_cutoff} Å")
    lines.append("    → Nonbonded interaction cutoff")
    lines.append(f"  constraints: {brief.constraints}")
    lines.append("    → Constraints (HBonds=hydrogen bonds, AllBonds=all bonds)")
    lines.append(f"  output_frequency_ps: {brief.output_frequency_ps} ps")
    lines.append("    → Trajectory output interval")

    # 9. Output Settings
    lines.append("\n## 9. Output Settings")
    lines.append("-" * 40)
    formats = brief.output_formats if brief.output_formats else ["topology"]
    lines.append(f"  output_formats: {formats}")
    lines.append("    → Output format")

    # 10. Structure Analysis (if present)
    if brief.structure_analysis and brief.structure_analysis.analysis_performed:
        sa = brief.structure_analysis
        lines.append("\n## 10. Structure Analysis Results")
        lines.append("-" * 40)
        lines.append(f"  analysis_ph: {sa.analysis_ph}")

        if sa.disulfide_bonds:
            lines.append(f"  disulfide_bonds: {len(sa.disulfide_bonds)} bonds")
            for ds in sa.disulfide_bonds:
                status = "form" if ds.form_bond else "skip"
                lines.append(f"    - {ds.chain1}:{ds.resnum1} - {ds.chain2}:{ds.resnum2} ({status})")

        if sa.histidine_states:
            lines.append(f"  histidine_states: {len(sa.histidine_states)} residues")
            for his in sa.histidine_states:
                user = " (user-specified)" if his.user_specified else ""
                lines.append(f"    - {his.chain}:{his.resnum} → {his.state}{user}")

    lines.append("\n" + "=" * 60)

    return "\n".join(lines)


# =============================================================================
# PHASE 2: SETUP TOOLS
# =============================================================================


def get_workflow_status(
    completed_steps: list[str],
    outputs: dict,
) -> dict:
    """Get current workflow progress and validate prerequisites.

    Call this before each step to check progress and ensure prerequisites are met.

    Args:
        completed_steps: List of completed step names
        outputs: Dictionary of output file paths from previous steps

    Returns:
        Dictionary with workflow status including:
        - completed_steps: List of completed steps
        - current_step: Name of current step to execute
        - next_tool: Name of MCP tool to call
        - step_index: Current step number (1-based)
        - total_steps: Total number of steps (4)
        - prerequisites_met: Whether prerequisites are satisfied
        - prerequisite_errors: List of missing prerequisites
        - is_complete: Whether all steps are done
        - progress: Progress indicator string (e.g., "[2/4]")
        - remaining_steps: List of steps not yet completed
        - estimated_time: Time estimate for current step
        - allowed_tools: List of tool names allowed for current step
    """
    step_info = get_current_step_info(completed_steps)
    current_step = step_info["current_step"]

    # Calculate progress metrics
    unique_completed = list(set(completed_steps))
    progress_count = len(unique_completed)
    remaining = [s for s in SETUP_STEPS if s not in unique_completed]

    result = {
        # Core status
        "completed_steps": unique_completed,
        "current_step": current_step,
        "next_tool": step_info["next_tool"],
        "step_index": step_info["step_index"],
        "total_steps": step_info["total_steps"],
        "is_complete": step_info["is_complete"],
        "available_outputs": outputs,
        # Progress visualization (Best Practice #3 enhancement)
        "progress": f"[{progress_count}/4]",
        "remaining_steps": remaining,
        "estimated_time": STEP_CONFIG.get(current_step, {}).get("estimate", "unknown") if current_step else "N/A",
        "allowed_tools": STEP_CONFIG.get(current_step, {}).get("allowed_tools", []) if current_step else [],
    }

    # Validate prerequisites if not complete
    if not step_info["is_complete"] and current_step:
        is_valid, errors = validate_step_prerequisites(
            current_step,
            outputs,
        )
        result["prerequisites_met"] = is_valid
        result["prerequisite_errors"] = errors
        result["input_requirements"] = step_info["input_requirements"]
    else:
        result["prerequisites_met"] = True
        result["prerequisite_errors"] = []
        result["input_requirements"] = ""

    return result


# =============================================================================
# PHASE 3: VALIDATION TOOLS
# =============================================================================


def run_validation(
    simulation_brief: dict,
    session_dir: str,
    setup_outputs: dict,
    decision_log: list[dict],
    compressed_setup: str = "",
    clarification_log_path: str = "",
) -> dict:
    """Run validation phase and generate report.

    Validates that required files exist and generates a comprehensive report.

    Args:
        simulation_brief: SimulationBrief dictionary
        session_dir: Path to session directory
        setup_outputs: Dictionary of output file paths
        decision_log: List of tool execution logs
        compressed_setup: Compressed setup summary
        clarification_log_path: Path to clarification chat history file

    Returns:
        Dictionary with validation_results and final_report
    """
    from pathlib import Path

    # Read clarification log if available
    clarification_log = ""
    if clarification_log_path and Path(clarification_log_path).exists():
        try:
            clarification_log = Path(clarification_log_path).read_text()
        except Exception:
            pass  # Ignore read errors

    # Validate required outputs
    validation_results = {
        "success": True,
        "required_files": {},
        "optional_files": {},
        "errors": [],
        "warnings": [],
    }

    # Standard file locations (fallback if not in outputs dictionary)
    session_path = Path(session_dir) if session_dir else None
    standard_paths = {
        "parm7": session_path / "topology" / "system.parm7" if session_path else None,
        "rst7": session_path / "topology" / "system.rst7" if session_path else None,
        "trajectory": session_path / "md_simulation" / "trajectory.dcd" if session_path else None,
        "merged_pdb": session_path / "merge" / "merged.pdb" if session_path else None,
        "solvated_pdb": session_path / "solvate" / "solvated.pdb" if session_path else None,
    }

    # Required files (parm7 = Amber topology file with .parm7 extension)
    required_keys = ["parm7", "rst7"]
    for key in required_keys:
        # First check outputs dict, then fall back to standard paths
        path = setup_outputs.get(key)
        if not path and standard_paths.get(key):
            fallback = standard_paths[key]
            if fallback and fallback.exists():
                path = str(fallback)

        if path and Path(path).exists():
            validation_results["required_files"][key] = {
                "path": path,
                "exists": True,
                "size": Path(path).stat().st_size,
            }
        else:
            validation_results["required_files"][key] = {
                "path": path,
                "exists": False,
            }
            validation_results["success"] = False
            validation_results["errors"].append(f"Required file missing: {key}")

    # Optional files
    optional_keys = ["trajectory", "merged_pdb", "solvated_pdb"]
    for key in optional_keys:
        # First check outputs dict, then fall back to standard paths
        path = setup_outputs.get(key)
        if not path and standard_paths.get(key):
            fallback = standard_paths[key]
            if fallback and fallback.exists():
                path = str(fallback)

        if path and Path(path).exists():
            validation_results["optional_files"][key] = {
                "path": path,
                "exists": True,
            }

    # Collect all important output files (*.parm7, *.rst7, *.pdb, *.dcd)
    important_files = {}

    # Check standard locations for important files
    if session_path and session_path.exists():
        # Find all important files recursively
        for pattern, label in [
            ("**/*.parm7", "topology"),
            ("**/*.rst7", "coordinates"),
            ("**/*.pdb", "structure"),
            ("**/*.dcd", "trajectory"),
        ]:
            for f in session_path.glob(pattern):
                # Use relative path from session_dir for cleaner output
                rel_path = f.relative_to(session_path)
                key = f"{label}:{rel_path}"
                important_files[str(rel_path)] = {
                    "path": str(f),
                    "type": label,
                    "size_mb": round(f.stat().st_size / (1024 * 1024), 2),
                }

    # Generate comprehensive summary report
    report_lines = [
        "=" * 60,
        "MD SIMULATION WORKFLOW - COMPLETE SUMMARY",
        "=" * 60,
        "",
    ]

    # 1. Input Summary
    report_lines.append("## 1. Input")
    report_lines.append("-" * 40)
    if simulation_brief:
        pdb_id = simulation_brief.get('pdb_id', 'N/A')
        fasta = simulation_brief.get('fasta_sequence')
        if pdb_id and pdb_id != 'N/A':
            report_lines.append(f"  Structure: PDB {pdb_id}")
        elif fasta:
            report_lines.append(f"  Structure: FASTA sequence ({len(fasta)} residues)")
        chains = simulation_brief.get('select_chains', ['all'])
        report_lines.append(f"  Chains: {chains}")
        include = simulation_brief.get('include_types', ['protein', 'ligand', 'ion'])
        report_lines.append(f"  Components: {include}")
    report_lines.append("")

    # 2. Simulation Parameters
    report_lines.append("## 2. Simulation Parameters")
    report_lines.append("-" * 40)
    if simulation_brief:
        report_lines.append(f"  Temperature: {simulation_brief.get('temperature', 300)} K")
        pressure = simulation_brief.get('pressure_bar')
        if pressure is not None:
            report_lines.append(f"  Pressure: {pressure} bar (NPT)")
        else:
            report_lines.append("  Ensemble: NVT (constant volume)")
        report_lines.append(f"  Simulation time: {simulation_brief.get('simulation_time_ns', 1)} ns")
        report_lines.append(f"  Timestep: {simulation_brief.get('timestep', 2)} fs")
        report_lines.append(f"  Force field: {simulation_brief.get('force_field', 'ff19SB')}")
        report_lines.append(f"  Water model: {simulation_brief.get('water_model', 'tip3p')}")
    report_lines.append("")

    # 3. Output Files (only important ones)
    report_lines.append("## 3. Output Files")
    report_lines.append("-" * 40)

    # Group by type
    file_types = {"topology": [], "coordinates": [], "structure": [], "trajectory": []}
    for rel_path, info in important_files.items():
        file_types[info["type"]].append((rel_path, info))

    # Show files by type
    type_labels = {
        "topology": "Topology (*.parm7)",
        "coordinates": "Coordinates (*.rst7)",
        "structure": "Structures (*.pdb)",
        "trajectory": "Trajectories (*.dcd)",
    }

    for ftype, label in type_labels.items():
        files = file_types.get(ftype, [])
        if files:
            report_lines.append(f"\n  ### {label}")
            for rel_path, info in files:
                size = info["size_mb"]
                report_lines.append(f"    - {rel_path} ({size} MB)")

    report_lines.append("")

    # 4. Status
    report_lines.append("## 4. Status")
    report_lines.append("-" * 40)
    if validation_results["success"]:
        report_lines.append("  ✓ Workflow completed successfully")
        report_lines.append("  ✓ All required files generated")
    else:
        report_lines.append("  ✗ Workflow incomplete")
        for error in validation_results["errors"]:
            report_lines.append(f"  ✗ {error}")

    if validation_results["warnings"]:
        for warning in validation_results["warnings"]:
            report_lines.append(f"  ⚠ {warning}")

    report_lines.append("")

    # 5. Session Info
    report_lines.append("## 5. Session Info")
    report_lines.append("-" * 40)
    report_lines.append(f"  Directory: {session_dir}")

    report_lines.append("")
    report_lines.append("=" * 60)

    final_report = "\n".join(report_lines)

    return {
        "success": validation_results["success"],
        "final_report": final_report,
        "important_files": important_files,
        "validation_results": validation_results,
    }


# =============================================================================
# STATE WRAPPER TOOLS (extract from ToolContext.state)
# =============================================================================


def get_workflow_status_tool(tool_context: ToolContext) -> dict:
    """Get current workflow progress and validate prerequisites. Call this before each step.

    Returns:
        dict: Current step info, completed steps, validation status, session_dir, and simulation_brief
    """
    completed_steps = safe_list(tool_context.state.get("completed_steps"))
    outputs = safe_dict(tool_context.state.get("outputs"))
    session_dir = str(tool_context.state.get("session_dir", ""))
    simulation_brief = safe_dict(tool_context.state.get("simulation_brief"))

    result = get_workflow_status(completed_steps, outputs)

    # Add session_dir to available_outputs for agent access
    if session_dir:
        result["available_outputs"]["session_dir"] = session_dir

    # Add simulation_brief for agent to access user's choices (include_types, etc.)
    if simulation_brief:
        result["simulation_brief"] = simulation_brief

    # CRITICAL: Warn if build_topology is next but box_dimensions is missing
    # This helps catch the bug where solvate step output wasn't passed correctly
    current_step = result.get("current_step")
    if current_step == "build_topology":
        box_dims = outputs.get("box_dimensions")
        if not box_dims:
            result["critical_warning"] = (
                "CRITICAL: box_dimensions is MISSING from outputs! "
                "The solvate step should have stored box_dimensions via mark_step_complete. "
                "Without box_dimensions, build_amber_system will create an IMPLICIT solvent system. "
                "Check that mark_step_complete was called with box_dimensions after solvate_structure."
            )
        elif isinstance(box_dims, dict) and not all(
            box_dims.get(k, 0) > 0 for k in ["box_a", "box_b", "box_c"]
        ):
            result["critical_warning"] = (
                f"CRITICAL: box_dimensions has invalid values: {box_dims}. "
                "Ensure the solvate step returned valid box dimensions."
            )

    return result


def mark_step_complete(
    step_name: str,
    output_files: dict,
    tool_context: ToolContext,
) -> dict:
    """Mark a workflow step as completed and store its output file paths.

    CRITICAL: You MUST call this after EACH successful MCP tool call to track progress.
    Without calling this, the workflow will not know which steps are complete.

    Args:
        step_name: Name of the completed step. Must be one of:
            - "prepare_complex"
            - "solvate"
            - "build_topology"
            - "run_simulation"
        output_files: Dictionary of output file paths from the step. Include all
            relevant paths, e.g.:
            - After prepare_complex: {"merged_pdb": "/path/to/merged.pdb"}
            - After solvate: {"solvated_pdb": "/path/to/solvated.pdb", "box_dimensions": {...}}
            - After build_topology: {"parm7": "/path/to/system.parm7", "rst7": "/path/to/system.rst7"}
            - After run_simulation: {"trajectory": "/path/to/traj.dcd"}
        tool_context: ADK ToolContext (automatically injected)

    Returns:
        dict: Updated workflow status with the new completed step
    """
    import json

    # Get current state
    completed_steps = safe_list(tool_context.state.get("completed_steps"))
    outputs = safe_dict(tool_context.state.get("outputs"))

    # Validate step name
    valid_steps = ["prepare_complex", "solvate", "build_topology", "run_simulation"]
    if step_name not in valid_steps:
        return {
            "success": False,
            "error": f"Invalid step_name '{step_name}'. Must be one of: {valid_steps}",
        }

    # Add step to completed list (avoid duplicates)
    if step_name not in completed_steps:
        completed_steps.append(step_name)

    # Merge output files
    if isinstance(output_files, dict):
        outputs.update(output_files)

    # Update session state (ADK requires JSON strings for complex types)
    tool_context.state["completed_steps"] = json.dumps(completed_steps)
    tool_context.state["outputs"] = json.dumps(outputs)

    return {
        "success": True,
        "step_marked": step_name,
        "completed_steps": completed_steps,
        "outputs": outputs,
        "message": f"Step '{step_name}' marked as complete. Progress: {len(completed_steps)}/4",
    }


def run_validation_tool(tool_context: ToolContext) -> dict:
    """Run validation and generate report. Reads parameters from session state.

    Returns:
        dict: validation_results and final_report
    """
    state = tool_context.state
    simulation_brief = safe_dict(state.get("simulation_brief"))
    session_dir = str(state.get("session_dir", "")) if state.get("session_dir") else ""
    setup_outputs = safe_dict(state.get("outputs"))
    decision_log = safe_list(state.get("decision_log"))
    compressed_setup = str(state.get("compressed_setup", "")) if state.get("compressed_setup") else ""
    clarification_log_path = str(state.get("clarification_log_path", "")) if state.get("clarification_log_path") else ""

    return run_validation(
        simulation_brief=simulation_brief,
        session_dir=session_dir,
        setup_outputs=setup_outputs,
        decision_log=decision_log,
        compressed_setup=compressed_setup,
        clarification_log_path=clarification_log_path,
    )
