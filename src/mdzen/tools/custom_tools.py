"""Custom FunctionTools for MDZen.

This module defines custom tools that wrap Python functions for use
with ADK's LlmAgent.
"""

from typing import Optional

from google.adk.tools import ToolContext

from mdzen.schemas import SimulationBrief, StructureAnalysis
from mdzen.utils import safe_dict, safe_list


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


def save_context(
    key: str,
    value: str | list | dict,
    tool_context: ToolContext,
) -> dict:
    """Save key information to session context file for later use.

    Call this to store important information gathered during clarification.
    This ensures information is preserved for generate_simulation_brief,
    even if you forget to pass it explicitly.

    Keys to save:
    - pdb_id: The PDB ID (e.g., "1AKE")
    - structure_file: Path to downloaded structure
    - chains: Selected chains (e.g., ["A"])
    - ligand_handling: "include" or "exclude"
    - histidine_states: Dict of residue -> state (e.g., {"A:126": "HIE"})

    Args:
        key: Context key name
        value: Value to store (string, list, or dict)
        tool_context: ADK ToolContext (auto-injected)

    Returns:
        Success status and current context
    """
    import json
    from pathlib import Path

    session_dir = tool_context.state.get("session_dir", "")
    if not session_dir:
        return {"success": False, "error": "session_dir not set"}

    context_path = Path(session_dir) / "clarification_context.json"

    # Load existing context
    context = {}
    if context_path.exists():
        try:
            context = json.loads(context_path.read_text())
        except Exception:
            pass

    # Update context
    context[key] = value

    # Save back
    context_path.write_text(json.dumps(context, indent=2))

    return {"success": True, "context": context}


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
    water_model: str = "opc",
    solvation_type: str = "explicit",
    implicit_solvent_model: str = "OBC2",
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
        water_model: Water model (e.g., "tip3p", "opc")
        solvation_type: "explicit" (water box) or "implicit" (GB continuum)
        implicit_solvent_model: GB model for implicit solvent (OBC2, GBn2, etc.)
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
    import json
    from pathlib import Path

    # Normalize "null" strings from LLM to Python None
    pdb_id = _normalize_null(pdb_id)
    fasta_sequence = _normalize_null(fasta_sequence)
    ligand_smiles = _normalize_null(ligand_smiles)
    lipids = _normalize_null(lipids)
    lipid_ratio = _normalize_null(lipid_ratio)
    pressure_bar = _normalize_null(pressure_bar)

    # Read saved clarification context to fill in missing parameters
    # This ensures we don't lose important info like pdb_id when the LLM forgets to pass it
    if tool_context is not None:
        session_dir = tool_context.state.get("session_dir", "")
        if session_dir:
            context_path = Path(session_dir) / "clarification_context.json"
            if context_path.exists():
                try:
                    saved = json.loads(context_path.read_text())
                    # Fill in missing parameters from saved context
                    if pdb_id is None:
                        pdb_id = saved.get("pdb_id")
                    if select_chains is None:
                        select_chains = saved.get("chains")
                    if structure_analysis is None:
                        structure_analysis = {}
                    # Apply ligand handling preference
                    if "ligand_handling" in saved:
                        if saved["ligand_handling"] == "exclude":
                            structure_analysis["exclude_all_ligands"] = True
                    # Apply histidine states
                    if "histidine_states" in saved:
                        structure_analysis["histidine_states"] = saved["histidine_states"]
                except Exception:
                    pass  # Ignore errors reading context file

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
        solvation_type=solvation_type,
        implicit_solvent_model=implicit_solvent_model,
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

    # Also save to session directory for MCP servers to access
    # (MCP servers don't have access to ADK session state)
    try:
        from common.utils import save_simulation_brief
        save_simulation_brief(brief_dict)
    except Exception:
        pass  # Ignore errors if session not set

    # Initialize scratchpad for Phase 2 state tracking
    scratchpad_path = None
    try:
        if tool_context is not None:
            session_dir = tool_context.state.get("session_dir", "")
            if session_dir:
                scratchpad_path = initialize_scratchpad(brief_dict, session_dir)
    except Exception:
        pass  # Ignore errors if scratchpad initialization fails

    # Generate user-friendly summary with all parameters and descriptions
    summary = _format_simulation_brief_summary(brief)

    result = {
        "success": True,
        "brief": brief_dict,
        "summary": summary,
    }

    if scratchpad_path:
        result["scratchpad_path"] = scratchpad_path
        result["scratchpad_message"] = (
            "Scratchpad initialized. In Phase 2, call read_scratchpad() first to see the next command."
        )

    return result


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
    lines.append(f"  solvation_type: {brief.solvation_type}")
    if brief.solvation_type == "implicit":
        lines.append("    → Using Generalized Born implicit solvent (no water box)")
        lines.append(f"  implicit_solvent_model: {brief.implicit_solvent_model}")
        lines.append("    → GB model (OBC2=igb5, GBn2=igb8 recommended)")
    else:
        lines.append("    → Explicit water box with periodic boundaries")
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
    import json

    # Read clarification log if available (best-effort; validation can proceed without it)
    if clarification_log_path and Path(clarification_log_path).exists():
        try:
            _ = Path(clarification_log_path).read_text()
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
            size = Path(path).stat().st_size
            validation_results["required_files"][key] = {
                "path": path,
                "exists": True,
                "size": size,
            }
            # Guard against false positives: empty files should be treated as failure.
            if size == 0:
                validation_results["success"] = False
                validation_results["errors"].append(f"Required file is empty: {key}")
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

    # -------------------------------------------------------------------------
    # QC v1: topology/coordinate sanity + composition summary
    # -------------------------------------------------------------------------
    qc_v1: dict = {"performed": False, "errors": [], "warnings": []}

    def _qc_v1_from_amber(parm7_path: str, rst7_path: str) -> dict:
        """Compute fast QC metrics from Amber topology/coords.

        This is designed to be robust and fast (seconds). It should never raise.
        """
        qc: dict = {
            "performed": False,
            "errors": [],
            "warnings": [],
            "atom_count": None,
            "residue_count": None,
            "net_charge_e": None,
            "composition": {},
        }
        try:
            import parmed as pmd

            struct = pmd.load_file(parm7_path, xyz=rst7_path)
            qc["performed"] = True

            atom_count = len(getattr(struct, "atoms", []))
            residue_count = len(getattr(struct, "residues", []))
            qc["atom_count"] = atom_count
            qc["residue_count"] = residue_count

            # Net charge (e)
            try:
                net_q = float(sum(float(a.charge) for a in struct.atoms))
                qc["net_charge_e"] = round(net_q, 6)
            except Exception:
                qc["warnings"].append("Could not compute net charge from topology.")

            # Composition summary by residue name
            aa3 = {
                "ALA","ARG","ASN","ASP","CYS","GLN","GLU","GLY","HIS","ILE","LEU","LYS",
                "MET","PHE","PRO","SER","THR","TRP","TYR","VAL",
            }
            water = {"WAT", "HOH", "TIP3", "SOL", "OPC", "TP3", "H2O"}
            lipids = {"POPC", "POPE", "DOPC", "DOPE", "DOPG", "DPPC", "CHL1", "CHL"}
            ions = {
                "NA", "Na+", "K", "K+", "CL", "Cl-", "MG", "CA", "ZN", "FE", "MN", "CO", "NI", "CU",
            }

            comp_counts = {
                "protein_residues": 0,
                "water_residues": 0,
                "ion_residues": 0,
                "lipid_residues": 0,
                "other_residues": 0,
                "other_resnames": {},
            }
            for res in struct.residues:
                rn = str(getattr(res, "name", "")).strip()
                rnu = rn.upper()
                if rnu in aa3:
                    comp_counts["protein_residues"] += 1
                elif rnu in water:
                    comp_counts["water_residues"] += 1
                elif rnu in lipids:
                    comp_counts["lipid_residues"] += 1
                elif rnu in ions:
                    comp_counts["ion_residues"] += 1
                else:
                    comp_counts["other_residues"] += 1
                    comp_counts["other_resnames"][rnu] = comp_counts["other_resnames"].get(rnu, 0) + 1

            qc["composition"] = comp_counts

            # Basic sanity checks
            if atom_count == 0:
                qc["errors"].append("Topology contains zero atoms.")
            if residue_count == 0:
                qc["errors"].append("Topology contains zero residues.")

        except ImportError:
            qc["warnings"].append("ParmEd not installed; will try OpenMM fallback for QC v1.")
        except Exception as e:
            # ParmEd can fail on some prmtop variants; fall back to OpenMM + minimal parsing.
            qc["warnings"].append(f"ParmEd QC failed: {type(e).__name__}: {e}")

        # Fallback path (or to enrich missing fields): OpenMM Amber readers + minimal prmtop parsing
        if not qc["performed"]:
            try:
                from openmm.app import AmberInpcrdFile, AmberPrmtopFile

                prmtop = AmberPrmtopFile(parm7_path)
                inpcrd = AmberInpcrdFile(rst7_path)

                atoms = list(prmtop.topology.atoms())
                residues = list(prmtop.topology.residues())
                qc["atom_count"] = len(atoms)
                qc["residue_count"] = len(residues)
                qc["performed"] = True

                # Coordinate consistency check
                try:
                    pos = inpcrd.positions
                    if pos is not None and len(pos) != len(atoms):
                        qc["errors"].append(
                            f"Atom/coord mismatch: topology atoms={len(atoms)} vs positions={len(pos)}"
                        )
                except Exception:
                    qc["warnings"].append("Could not validate coordinates vs topology atom count.")

                # Composition summary by residue name (from Topology)
                aa3 = {
                    "ALA","ARG","ASN","ASP","CYS","GLN","GLU","GLY","HIS","ILE","LEU","LYS",
                    "MET","PHE","PRO","SER","THR","TRP","TYR","VAL",
                }
                water = {"WAT", "HOH", "TIP3", "SOL", "OPC", "TP3", "H2O"}
                lipids = {"POPC", "POPE", "DOPC", "DOPE", "DOPG", "DPPC", "CHL1", "CHL"}
                ions = {
                    "NA", "Na+", "K", "K+", "CL", "Cl-", "MG", "CA", "ZN", "FE", "MN", "CO", "NI", "CU",
                }
                comp_counts = {
                    "protein_residues": 0,
                    "water_residues": 0,
                    "ion_residues": 0,
                    "lipid_residues": 0,
                    "other_residues": 0,
                    "other_resnames": {},
                }
                for res in residues:
                    rn = str(getattr(res, "name", "")).strip()
                    rnu = rn.upper()
                    if rnu in aa3:
                        comp_counts["protein_residues"] += 1
                    elif rnu in water:
                        comp_counts["water_residues"] += 1
                    elif rnu in lipids:
                        comp_counts["lipid_residues"] += 1
                    elif rnu in ions:
                        comp_counts["ion_residues"] += 1
                    else:
                        comp_counts["other_residues"] += 1
                        comp_counts["other_resnames"][rnu] = comp_counts["other_resnames"].get(rnu, 0) + 1
                qc["composition"] = comp_counts

                # Net charge parsing from prmtop (%FLAG CHARGE, units: e*18.2223)
                try:
                    charges: list[float] = []
                    in_charge = False
                    with open(parm7_path, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if line.startswith("%FLAG "):
                                in_charge = line.strip().endswith("CHARGE")
                                continue
                            if in_charge:
                                if line.startswith("%FORMAT") or line.startswith("%COMMENT"):
                                    continue
                                if line.startswith("%FLAG "):
                                    in_charge = False
                                    continue
                                for tok in line.split():
                                    try:
                                        charges.append(float(tok))
                                    except Exception:
                                        pass
                    if charges:
                        net_q = sum(charges) / 18.2223
                        qc["net_charge_e"] = round(float(net_q), 6)
                    else:
                        qc["warnings"].append("Could not parse %FLAG CHARGE from prmtop for net charge.")
                except Exception:
                    qc["warnings"].append("Could not parse net charge from prmtop.")

            except Exception as e:
                qc["errors"].append(f"QC v1 fallback failed: {type(e).__name__}: {e}")

        return qc

    parm7_path = validation_results["required_files"].get("parm7", {}).get("path")
    rst7_path = validation_results["required_files"].get("rst7", {}).get("path")
    if validation_results["success"] and parm7_path and rst7_path:
        qc_v1 = _qc_v1_from_amber(parm7_path, rst7_path)
        validation_results["qc_v1"] = qc_v1
        # Treat QC errors as validation failure ONLY if QC couldn't be performed at all,
        # or if we detect a hard mismatch (e.g., atom/coord mismatch).
        if qc_v1.get("errors"):
            hard = any("Atom/coord mismatch" in str(e) for e in qc_v1["errors"])
            if hard or not qc_v1.get("performed"):
                validation_results["success"] = False
                for e in qc_v1["errors"]:
                    validation_results["errors"].append(f"QC v1: {e}")
    else:
        validation_results["qc_v1"] = qc_v1

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

    # 5. QC (v1)
    report_lines.append("## 5. QC (v1)")
    report_lines.append("-" * 40)
    qc = validation_results.get("qc_v1", {}) if isinstance(validation_results, dict) else {}
    if qc and qc.get("performed"):
        report_lines.append(f"  Atoms: {qc.get('atom_count')}")
        report_lines.append(f"  Residues: {qc.get('residue_count')}")
        report_lines.append(f"  Net charge (e): {qc.get('net_charge_e')}")
        comp = qc.get("composition", {}) or {}
        report_lines.append("  Composition (residues):")
        report_lines.append(f"    - protein: {comp.get('protein_residues')}")
        report_lines.append(f"    - water: {comp.get('water_residues')}")
        report_lines.append(f"    - ions: {comp.get('ion_residues')}")
        report_lines.append(f"    - lipids: {comp.get('lipid_residues')}")
        report_lines.append(f"    - other: {comp.get('other_residues')}")
        if comp.get('other_resnames'):
            report_lines.append(f"  Other resnames: {comp.get('other_resnames')}")
    else:
        report_lines.append("  QC v1 not performed.")
        if qc and qc.get("warnings"):
            for w in qc["warnings"]:
                report_lines.append(f"  ⚠ {w}")
    report_lines.append("")

    # 5. Session Info
    report_lines.append("## 6. Session Info")
    report_lines.append("-" * 40)
    report_lines.append(f"  Directory: {session_dir}")

    report_lines.append("")
    report_lines.append("=" * 60)

    final_report = "\n".join(report_lines)

    result = {
        "success": validation_results["success"],
        "final_report": final_report,
        "important_files": important_files,
        "validation_results": validation_results,
    }

    # Write machine-readable validation result for benchmark tooling
    try:
        if session_path:
            (session_path / "validation_result.json").write_text(
                json.dumps(result, indent=2, default=str),
                encoding="utf-8",
            )
    except Exception:
        pass

    return result


# =============================================================================
# STATE WRAPPER TOOLS (extract from ToolContext.state)
# =============================================================================


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


# =============================================================================
# SCRATCHPAD TOOLS (for smaller models)
# =============================================================================


def read_scratchpad(tool_context: ToolContext) -> str:
    """Read the current workflow state from scratchpad.

    ALWAYS call this FIRST in every turn to see:
    - What command to run next (CURRENT TASK section)
    - What outputs are available (OUTPUTS section)
    - Overall progress (COMPLETED section)

    Returns:
        Scratchpad contents (markdown) or error message
    """
    session_dir = tool_context.state.get("session_dir", "")
    if not session_dir:
        return "ERROR: session_dir not set. Cannot read scratchpad."

    from pathlib import Path
    path = Path(session_dir) / "scratchpad.md"
    if not path.exists():
        return "ERROR: Scratchpad not found. Run Phase 1 (clarification) first."

    return path.read_text()


def update_scratchpad(
    step: str,
    outputs: dict,
    tool_context: ToolContext,
) -> dict:
    """Update scratchpad after completing a step.

    This automatically:
    1. Marks the step as complete
    2. Stores output paths
    3. Generates the NEXT command to run

    Args:
        step: Completed step name (prepare_complex, solvate, build_topology, run_simulation)
        outputs: Dict of output paths from the tool result. Include all relevant paths:
            - After prepare_complex: {"merged_pdb": "/path/to/merged.pdb"}
            - After solvate: {"solvated_pdb": "/path/to/solvated.pdb", "box_dimensions": {...}}
            - After build_topology: {"parm7": "/path/to/system.parm7", "rst7": "/path/to/system.rst7"}
            - After run_simulation: {"trajectory": "/path/to/traj.dcd"}
        tool_context: ADK ToolContext (automatically injected)

    Returns:
        Updated scratchpad preview and next step info
    """
    import json
    from pathlib import Path

    session_dir = tool_context.state.get("session_dir", "")
    if not session_dir:
        return {"success": False, "error": "session_dir not set"}

    scratchpad_path = Path(session_dir) / "scratchpad.md"
    brief_path = Path(session_dir) / "simulation_brief.json"

    # Load simulation brief for context
    brief = {}
    if brief_path.exists():
        try:
            brief = json.loads(brief_path.read_text())
        except Exception:
            pass
    solvation_type = brief.get("solvation_type", "explicit")

    # Load current scratchpad state
    state = _load_scratchpad_state(scratchpad_path)
    if step not in state["completed"]:
        state["completed"].append(step)
    state["outputs"].update(outputs or {})

    # Also update ADK session state for compatibility
    completed_steps = safe_list(tool_context.state.get("completed_steps"))
    if step not in completed_steps:
        completed_steps.append(step)
    tool_context.state["completed_steps"] = json.dumps(completed_steps)

    current_outputs = safe_dict(tool_context.state.get("outputs"))
    current_outputs.update(outputs or {})
    tool_context.state["outputs"] = json.dumps(current_outputs)

    # Determine next step and generate command
    next_step, next_command = _generate_next_command(
        completed=state["completed"],
        outputs=state["outputs"],
        brief=brief,
        session_dir=session_dir,
    )

    # Write updated scratchpad
    new_content = _render_scratchpad(
        current_step=next_step,
        current_command=next_command,
        context=state["context"],
        outputs=state["outputs"],
        completed=state["completed"],
        solvation_type=solvation_type,
        brief=brief,
    )
    scratchpad_path.write_text(new_content)

    return {
        "success": True,
        "step_completed": step,
        "next_step": next_step,
        "message": f"Scratchpad updated. Next: {next_step}" if next_step else "WORKFLOW COMPLETE!",
    }


def _load_scratchpad_state(scratchpad_path) -> dict:
    """Load current state from scratchpad file."""
    from pathlib import Path
    import re

    state = {
        "completed": [],
        "outputs": {},
        "context": {},
    }

    path = Path(scratchpad_path)
    if not path.exists():
        return state

    content = path.read_text()

    # Parse completed steps from checklist
    completed_pattern = r'\d+\.\s*\[x\]\s*(\w+)'
    for match in re.finditer(completed_pattern, content):
        step = match.group(1)
        if step not in state["completed"]:
            state["completed"].append(step)

    # Parse outputs section
    outputs_section = re.search(r'## 📦 OUTPUTS.*?(?=━━━|$)', content, re.DOTALL)
    if outputs_section:
        output_text = outputs_section.group(0)
        output_pattern = r'-\s*(\w+):\s*(.+)$'
        for match in re.finditer(output_pattern, output_text, re.MULTILINE):
            key = match.group(1).strip()
            value = match.group(2).strip()
            if value and value != "(none yet)":
                state["outputs"][key] = value

    # Parse context section
    context_section = re.search(r'## 📋 CONTEXT.*?(?=━━━|$)', content, re.DOTALL)
    if context_section:
        context_text = context_section.group(0)
        context_pattern = r'-\s*([^:]+):\s*(.+)$'
        for match in re.finditer(context_pattern, context_text, re.MULTILINE):
            key = match.group(1).strip().lower().replace(" ", "_")
            value = match.group(2).strip()
            state["context"][key] = value

    return state


def _generate_next_command(
    completed: list,
    outputs: dict,
    brief: dict,
    session_dir: str
) -> tuple:
    """Generate the exact command for the next step."""
    import json

    solvation_type = brief.get("solvation_type", "explicit")

    # Define workflow based on solvation type
    if solvation_type == "implicit":
        workflow = ["prepare_complex", "build_topology", "run_simulation"]
    else:
        workflow = ["prepare_complex", "solvate", "build_topology", "run_simulation"]

    # Find next step
    next_step = None
    for step in workflow:
        if step not in completed:
            next_step = step
            break

    if next_step is None:
        return None, "WORKFLOW COMPLETE"

    # Generate command based on step
    pdb_id = brief.get("pdb_id", "")
    include_types = brief.get("include_types", ["protein", "ligand", "ion"])
    process_ligands = "true" if "ligand" in include_types else "false"

    if next_step == "prepare_complex":
        cmd = f'''prepare_complex(
    pdb_id="{pdb_id}",
    output_dir="{session_dir}",
    process_ligands={process_ligands}
)'''

    elif next_step == "solvate":
        merged_pdb = outputs.get("merged_pdb", f"{session_dir}/merge/merged.pdb")
        is_membrane = brief.get("is_membrane", False)
        if is_membrane:
            lipids = brief.get("lipids", "POPC")
            cmd = f'''embed_in_membrane(
    pdb_file="{merged_pdb}",
    lipid_type="{lipids}",
    output_dir="{session_dir}",
    output_name="membrane"
)'''
        else:
            cmd = f'''solvate_structure(
    pdb_file="{merged_pdb}",
    output_dir="{session_dir}",
    output_name="solvated"
)'''

    elif next_step == "build_topology":
        if solvation_type == "implicit":
            merged_pdb = outputs.get("merged_pdb", f"{session_dir}/merge/merged.pdb")
            cmd = f'''build_amber_system(
    pdb_file="{merged_pdb}",
    output_dir="{session_dir}",
    output_name="system"
)'''
        else:
            solvated_pdb = outputs.get("solvated_pdb", f"{session_dir}/solvate/solvated.pdb")
            box = outputs.get("box_dimensions", {})
            box_json = json.dumps(box) if isinstance(box, dict) else str(box)
            cmd = f'''build_amber_system(
    pdb_file="{solvated_pdb}",
    box_dimensions={box_json},
    output_dir="{session_dir}",
    output_name="system"
)'''

    elif next_step == "run_simulation":
        parm7 = outputs.get("parm7", f"{session_dir}/topology/system.parm7")
        rst7 = outputs.get("rst7", f"{session_dir}/topology/system.rst7")
        if solvation_type == "implicit":
            implicit_model = brief.get("implicit_solvent_model", "OBC2")
            cmd = f'''run_md_simulation(
    prmtop_file="{parm7}",
    inpcrd_file="{rst7}",
    implicit_solvent="{implicit_model}",
    output_dir="{session_dir}"
)'''
        else:
            cmd = f'''run_md_simulation(
    prmtop_file="{parm7}",
    inpcrd_file="{rst7}",
    output_dir="{session_dir}"
)'''
    else:
        cmd = f"# Unknown step: {next_step}"

    return next_step, cmd


def _render_scratchpad(
    current_step: str | None,
    current_command: str,
    context: dict,
    outputs: dict,
    completed: list,
    solvation_type: str,
    brief: dict,
) -> str:
    """Render the scratchpad markdown content."""
    from datetime import datetime

    # Determine workflow steps
    if solvation_type == "implicit":
        workflow = ["prepare_complex", "build_topology", "run_simulation"]
    else:
        workflow = ["prepare_complex", "solvate", "build_topology", "run_simulation"]

    total_steps = len(workflow)
    current_index = len(completed) + 1 if current_step else total_steps

    # Build completed checklist
    checklist_lines = []
    for i, step in enumerate(workflow):
        if step in completed:
            output_info = ""
            if step == "prepare_complex" and outputs.get("merged_pdb"):
                output_info = f" → {outputs['merged_pdb']}"
            elif step == "solvate" and outputs.get("solvated_pdb"):
                output_info = f" → {outputs['solvated_pdb']}"
            elif step == "build_topology" and outputs.get("parm7"):
                output_info = f" → {outputs['parm7']}"
            elif step == "run_simulation" and outputs.get("trajectory"):
                output_info = f" → {outputs['trajectory']}"
            checklist_lines.append(f"{i+1}. [x] {step}{output_info}")
        elif step == current_step:
            checklist_lines.append(f"{i+1}. [ ] {step} ← YOU ARE HERE")
        else:
            checklist_lines.append(f"{i+1}. [ ] {step}")
    checklist = "\n".join(checklist_lines)

    # Build outputs section
    if outputs:
        outputs_lines = [f"- {k}: {v}" for k, v in outputs.items() if v]
        outputs_text = "\n".join(outputs_lines) if outputs_lines else "(none yet)"
    else:
        outputs_text = "(none yet)"

    # Build after success section
    if current_step:
        after_success = _get_after_success_text(current_step)
    else:
        after_success = "No more tasks. Workflow is complete!"

    # Get context from brief
    session_dir = context.get("session_directory", brief.get("session_dir", ""))
    pdb_id = brief.get("pdb_id", "N/A")
    chains = brief.get("select_chains", ["all"])
    include = brief.get("include_types", ["protein", "ligand", "ion"])

    content = f'''# MDZen Scratchpad
Updated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 🎯 CURRENT TASK (Step {current_index} of {total_steps})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**Run this command:**

{current_command}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## ✅ AFTER SUCCESS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{after_success}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 📋 CONTEXT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

- Session directory: {session_dir}
- Solvation type: {solvation_type}
- PDB: {pdb_id}
- Chains: {chains}
- Include: {include}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 📦 OUTPUTS (use these paths)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{outputs_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## ✓ COMPLETED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{checklist}
'''
    return content


def _get_after_success_text(step: str) -> str:
    """Get the 'after success' instruction text for a step."""
    if step == "prepare_complex":
        return '''**Update scratchpad with:**

update_scratchpad(
    step="prepare_complex",
    outputs={"merged_pdb": "<result.merged_pdb>"}
)'''
    elif step == "solvate":
        return '''**Update scratchpad with:**

update_scratchpad(
    step="solvate",
    outputs={
        "solvated_pdb": "<result.output_file>",
        "box_dimensions": <result.box_dimensions>
    }
)'''
    elif step == "build_topology":
        return '''**Update scratchpad with:**

update_scratchpad(
    step="build_topology",
    outputs={
        "parm7": "<result.parm7>",
        "rst7": "<result.rst7>"
    }
)'''
    elif step == "run_simulation":
        return '''**Update scratchpad with:**

update_scratchpad(
    step="run_simulation",
    outputs={"trajectory": "<result.trajectory>"}
)'''
    else:
        return "No after-success action needed."


def initialize_scratchpad(brief_dict: dict, session_dir: str) -> str:
    """Initialize scratchpad at the end of Phase 1.

    Called from generate_simulation_brief when scratchpad mode is enabled.

    Args:
        brief_dict: SimulationBrief dictionary
        session_dir: Path to session directory

    Returns:
        Path to the created scratchpad file
    """
    from pathlib import Path

    solvation_type = brief_dict.get("solvation_type", "explicit")

    # First command is always prepare_complex
    pdb_id = brief_dict.get("pdb_id", "")
    include_types = brief_dict.get("include_types", ["protein", "ligand", "ion"])
    process_ligands = "true" if "ligand" in include_types else "false"

    first_cmd = f'''prepare_complex(
    pdb_id="{pdb_id}",
    output_dir="{session_dir}",
    process_ligands={process_ligands}
)'''

    # Generate initial scratchpad
    content = _render_scratchpad(
        current_step="prepare_complex",
        current_command=first_cmd,
        context={"session_directory": session_dir},
        outputs={},
        completed=[],
        solvation_type=solvation_type,
        brief=brief_dict,
    )

    scratchpad_path = Path(session_dir) / "scratchpad.md"
    scratchpad_path.write_text(content)

    return str(scratchpad_path)


# =============================================================================
# WORKFLOW v2: SHARED SCRATCHPAD/STATE TOOLS
# =============================================================================


def _workflow_v2_default_state() -> dict:
    """Return default workflow v2 state.

    This state is intentionally flat (top-level keys) so smaller models can
    reliably read/write it. Nested dicts are used only when unavoidable.
    """

    return {
        "current_step": "acquire_structure",
        "completed_steps": [],
        "awaiting_user_input": False,
        "pending_questions": [],
        "last_step_summary": "",
        # Common artifacts (filled across steps)
        "structure_file": "",
        "merged_pdb": "",
        "structure_analysis": {},
        "solvation_type": "",  # "explicit" or "membrane"
        "solvated_pdb": "",
        "membrane_pdb": "",
        "box_dimensions": {},
        "parm7": "",
        "rst7": "",
        "trajectory": "",
        "validation_result": {},
    }


def _workflow_v2_paths(session_dir: str):
    from pathlib import Path

    base = Path(session_dir)
    return (base / "workflow_state.json", base / "workflow_scratchpad.md")


def _render_workflow_v2_scratchpad(state: dict) -> str:
    """Render human-readable scratchpad for the v2 workflow."""
    from datetime import datetime

    def _s(v):
        return v if v else "(unset)"

    completed = state.get("completed_steps") or []
    pending_questions = state.get("pending_questions") or []

    lines: list[str] = []
    lines.append("# MDZen Workflow Scratchpad (v2)")
    lines.append(f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("## Status")
    lines.append(f"- current_step: {_s(state.get('current_step'))}")
    lines.append(f"- completed_steps: {completed if completed else '[]'}")
    lines.append(f"- awaiting_user_input: {bool(state.get('awaiting_user_input'))}")
    if pending_questions:
        lines.append("- pending_questions:")
        for q in pending_questions:
            lines.append(f"  - {q}")
    lines.append("")
    lines.append("## Key files")
    lines.append(f"- structure_file: {_s(state.get('structure_file'))}")
    lines.append(f"- merged_pdb: {_s(state.get('merged_pdb'))}")
    lines.append(f"- solvated_pdb: {_s(state.get('solvated_pdb'))}")
    lines.append(f"- membrane_pdb: {_s(state.get('membrane_pdb'))}")
    lines.append(f"- parm7: {_s(state.get('parm7'))}")
    lines.append(f"- rst7: {_s(state.get('rst7'))}")
    lines.append(f"- trajectory: {_s(state.get('trajectory'))}")
    lines.append("")
    lines.append("## Decisions")
    lines.append(f"- solvation_type: {_s(state.get('solvation_type'))}")
    lines.append(f"- box_dimensions: {state.get('box_dimensions') or {}}")
    lines.append("")
    lines.append("## Last summary")
    last = (state.get("last_step_summary") or "").strip()
    lines.append(last if last else "(none)")
    lines.append("")
    lines.append("---")
    lines.append("## Machine-readable state (JSON)")
    lines.append("```json")
    try:
        import json

        lines.append(json.dumps(state, indent=2, ensure_ascii=False, default=str))
    except Exception:
        # Best-effort; never fail rendering.
        lines.append("{}")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _load_workflow_v2_state(session_dir: str) -> dict:
    import json

    json_path, _ = _workflow_v2_paths(session_dir)
    if not json_path.exists():
        return _workflow_v2_default_state()
    try:
        loaded = json.loads(json_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            state = _workflow_v2_default_state()
            state.update(loaded)
            return state
    except Exception:
        pass
    return _workflow_v2_default_state()


def _save_workflow_v2_state(session_dir: str, state: dict) -> None:
    import json

    json_path, md_path = _workflow_v2_paths(session_dir)
    json_path.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    md_path.write_text(_render_workflow_v2_scratchpad(state), encoding="utf-8")


def read_workflow_state(tool_context: ToolContext) -> dict:
    """Read shared workflow v2 state.

    Always call this first in every step. If the state doesn't exist yet,
    it is initialized with defaults.
    """
    import json

    session_dir = str(tool_context.state.get("session_dir", "") or "")
    if not session_dir:
        return {"success": False, "error": "session_dir not set"}

    state = _load_workflow_v2_state(session_dir)

    # Mirror into ADK session state for CLI to read without filesystem access.
    tool_context.state["workflow_state"] = json.dumps(state, ensure_ascii=False, default=str)
    tool_context.state["workflow_current_step"] = str(state.get("current_step") or "")

    return {"success": True, "state": state}


def update_workflow_state(
    step: str | None = None,
    updates: dict | None = None,
    mark_step_complete: bool = False,
    awaiting_user_input: bool = False,
    pending_questions: list[str] | None = None,
    last_step_summary: str = "",
    tool_context: ToolContext = None,  # ADK injects this automatically
) -> dict:
    """Update shared workflow v2 state and write scratchpad.

    Typical usage in prompts:
    - At the start: read_workflow_state()
    - Before asking user questions: update_workflow_state(awaiting_user_input=True, pending_questions=[...])
    - After completing step: update_workflow_state(step=\"select_prepare\", updates={...}, mark_step_complete=True)
    """
    import json

    if tool_context is None:
        return {"success": False, "error": "tool_context missing"}

    session_dir = str(tool_context.state.get("session_dir", "") or "")
    if not session_dir:
        return {"success": False, "error": "session_dir not set"}

    state = _load_workflow_v2_state(session_dir)

    def _looks_like_select_prepare_questions(qs: list[str] | None) -> bool:
        """Heuristic: does the question list look like select_prepare chain/ligand prompts?"""
        if not qs:
            return False
        blob = " ".join(str(q).lower() for q in qs if str(q).strip())
        return any(
            k in blob
            for k in [
                "which protein chains",
                "protein chains to simulate",
                "ligands detected",
                "include ligands",
            ]
        )

    # Apply updates
    if updates and isinstance(updates, dict):
        for k, v in updates.items():
            state[k] = v

    # -------------------------------------------------------------------------
    # Guard: prevent redundant chain/ligand re-asking in select_prepare.
    #
    # When users already answered chain/ligand selection, some models may still
    # call update_workflow_state(awaiting_user_input=True, pending_questions=[...]).
    # This forces the CLI back into question mode even though selections exist.
    #
    # If we already have BOTH:
    # - selection_chains (non-empty)
    # - include_types (non-empty)
    #
    # then we should not go back to awaiting_user_input for chain/ligand prompts.
    # -------------------------------------------------------------------------
    answered_select = bool(state.get("selection_chains")) and bool(state.get("include_types"))
    is_select_prepare = str(state.get("current_step") or "") == "select_prepare"
    wants_awaiting = bool(awaiting_user_input) or (pending_questions is not None)
    if is_select_prepare and answered_select and wants_awaiting:
        if bool(awaiting_user_input) or _looks_like_select_prepare_questions(pending_questions):
            awaiting_user_input = False
            pending_questions = []
            if not last_step_summary:
                last_step_summary = "Ignored redundant select_prepare questions (already answered)"

    # Guard: prevent redundant re-asking in acquire_structure once structure_file exists.
    # Small models sometimes set awaiting_user_input=True even after download_structure succeeded.
    is_acquire = str(state.get("current_step") or "") == "acquire_structure"
    has_structure = bool(state.get("structure_file"))
    if is_acquire and has_structure and (bool(awaiting_user_input) or pending_questions is not None):
        awaiting_user_input = False
        pending_questions = []
        if not last_step_summary:
            last_step_summary = "Ignored redundant acquire_structure questions (structure_file already set)"

    # Update UI flags/questions
    state["awaiting_user_input"] = bool(awaiting_user_input)
    if pending_questions is not None:
        state["pending_questions"] = pending_questions

    if last_step_summary:
        state["last_step_summary"] = last_step_summary

    # Step completion / advancement
    if step:
        state["current_step"] = step
    current_step = state.get("current_step")

    if mark_step_complete and current_step:
        completed = state.get("completed_steps") or []
        if current_step not in completed:
            completed.append(current_step)
        state["completed_steps"] = completed

        # Advance current_step unless we are awaiting input.
        if not state.get("awaiting_user_input"):
            try:
                from mdzen.workflow import get_next_workflow_v2_step

                nxt = get_next_workflow_v2_step(current_step)
                if nxt:
                    state["current_step"] = nxt
            except Exception:
                pass

    # Persist to disk + mirror into session state
    _save_workflow_v2_state(session_dir, state)
    tool_context.state["workflow_state"] = json.dumps(state, ensure_ascii=False, default=str)
    tool_context.state["workflow_current_step"] = str(state.get("current_step") or "")

    # Write machine-readable questions for external drivers (best-effort).
    if state.get("awaiting_user_input"):
        try:
            from mdzen.cli.auto_answer import write_questions_json

            write_questions_json(session_dir, wf_state=state)
        except Exception:
            pass

    # Compatibility: also sync to the legacy Phase2 keys so existing reporting/QC works.
    # - completed_steps / outputs are JSON strings in ADK state.
    try:
        legacy_completed = state.get("completed_steps") or []
        tool_context.state["completed_steps"] = json.dumps(legacy_completed, ensure_ascii=False)

        legacy_outputs = safe_dict(tool_context.state.get("outputs")) or {}
        # Map v2 keys → legacy outputs keys
        for k in ["merged_pdb", "solvated_pdb", "membrane_pdb", "parm7", "rst7", "trajectory"]:
            if state.get(k):
                legacy_outputs[k] = state.get(k)
        if isinstance(state.get("box_dimensions"), dict) and state.get("box_dimensions"):
            legacy_outputs["box_dimensions"] = state.get("box_dimensions")

        tool_context.state["outputs"] = json.dumps(legacy_outputs, ensure_ascii=False, default=str)
    except Exception:
        pass

    return {"success": True, "state": state}


def get_quick_md_defaults() -> dict:
    """Return default parameters for the quick_md step."""
    try:
        from mdzen.workflow import QUICK_MD_DEFAULTS

        # Shallow copy to avoid accidental mutation by callers
        return {"success": True, "defaults": dict(QUICK_MD_DEFAULTS)}
    except Exception as e:
        return {"success": False, "error": str(e), "defaults": {}}

