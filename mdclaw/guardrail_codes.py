"""Single source of truth for MDClaw guardrail ``code`` values.

MDClaw's weak-agent contract is: branch on stable ``code`` strings, never on
human-readable messages. ``GUARDRAIL_CODES`` maps every agent-facing ``code``
the package can emit to a one-line recommended action.

Invariants (enforced by tests):

- Every ``code`` literal emitted anywhere under ``mdclaw/`` must be a key here
  (``tests/test_guardrail_code_registry.py``).
- ``skills/common/guardrail-codes.md`` is generated from this registry
  (``scripts/gen_guardrail_codes_md.py``) and checked for drift.

When adding a new guardrail code, add it here with a concise, imperative,
agent-facing action, then regenerate the skill doc.
"""

from __future__ import annotations

GUARDRAIL_CODES: dict[str, str] = {
    # --- success / generic ---
    "ok": "Success; proceed to the next step.",
    "openmm_system_built": "System build succeeded; continue to min/eq/prod.",
    "unhandled_exception": "Report the structured error; do not retry blindly.",
    "unhandled_error": "Read the message and errors, fix the reported cause, then retry.",
    "invalid_json_input": "Fix the JSON string or pass valid JSON via --json-input.",
    "tool_renamed": "The tool was consolidated; call the replacement tool named in the message.",
    "file_not_found": "Verify the path and rerun only after the file exists.",
    "missing_local_file_path": "Provide a valid local file path for the input.",
    "tool_not_available": "Stop local execution and report the missing external tool.",
    "missing_required_arguments": "Add the listed required flags (see `mdclaw --list-json`).",
    "invalid_mdclaw_runtime": "Run inside a valid MDClaw runtime (container/SIF or mdclaw conda env).",
    "input_resolution_blocked": "Resolve inputs via the DAG or provide explicit paths.",

    # --- node / DAG context ---
    "node_context_required": "Create the node, then run it with both --job-dir and --node-id.",
    "missing_node_context": "Pass both --job-dir and --node-id for this workflow tool.",
    "node_id_requires_job_dir": "--node-id was passed without --job-dir; pass both together.",
    "node_mode_required": "Specify the node mode required by this tool.",
    "node_missing": "Node id does not exist; use IDs from inspect_job/explain_node.",
    "node_json_invalid": "node.json is corrupt; inspect the node directory and repair or recreate.",
    "node_terminal": "Node is terminal (completed/failed); branch a new node instead.",
    "node_wait_timeout": "Waiting on the node timed out; check the running job or lease.",
    "node_already_claimed": "Another worker holds the claim; wait, or override only if stale.",
    "invalid_node_type": "Use one of: source, prep, solv, topo, min, eq, prod, analyze.",
    "invalid_node_status": "Use a valid node status value.",
    "update_state_no_target": "Provide status (with node_id) and/or params to update_workflow_state.",
    "update_state_status_requires_node_id": "Pass node_id together with status to update_workflow_state.",
    "completed_node_sealed": "Completed nodes are immutable; branch a new node to change anything.",
    "parent_node_not_completed": "Complete or repair the parent node before running this node.",
    "referenced_node_missing": "A parent/dependency id does not exist; use IDs from inspect_job.",
    "study_context_missing": "Job is not under a study; run bootstrap_md_workflow and create the source node in the returned job_dir.",
    "progress_missing_or_invalid": "progress.json is missing/invalid; reinspect the job to rebuild.",
    "claim_owner_mismatch": "Claim is owned by another agent; do not force-release without cause.",
    "invalid_claim_expiry": "Provide a valid claim expiry timestamp.",
    "invalid_lease_seconds": "Provide a positive integer for lease seconds.",
    "agent_id_required": "Provide --agent-id when claiming or updating a node.",
    "invalid_agent_skills_dir": "Point to a valid agent skills directory.",

    # --- node needs ---
    "invalid_need": "Provide a well-formed node need payload.",
    "invalid_need_attempt": "Provide a valid need-attempt payload.",
    "need_index_out_of_range": "Use a need index that exists on the node.",
    "invalid_node_need_action": "Use manage_node_need --action one of: add, clear, record_attempt.",

    # --- source / bundle ---
    "invalid_source": "Provide a valid structural source bundle.",
    "invalid_source_node": "The referenced source node is invalid.",
    "source_already_exists": "One source per job; add to the existing bundle or use another job.",
    "source_cannot_have_parents": "Source nodes have no parents; omit --parent-node-ids.",
    "source_cannot_have_dependencies": "Source nodes have no dependencies; remove them.",
    "multiple_source_roots": "A job DAG must have exactly one source root.",
    "unsupported_assembly_source": "Use a supported assembly source type.",

    # --- fetch / structure acquisition ---
    "missing_pdb_id": "Provide a valid 4-character PDB ID.",
    "missing_pdb_file": "Use DAG auto-resolution or provide a valid PDB/mmCIF path.",
    "missing_structure_file": "Provide the structure file this tool requires.",
    "missing_uniprot_id": "Provide a valid UniProt accession.",
    "pdbfixer_missing_residues_out_of_scope": "Regenerate the source with MODELLER/Boltz-2 instead of PDBFixer repair.",

    # --- prep / cleaning / selection ---
    "invalid_prep_solvent_type": "Use a supported solvent type for prep.",
    "invalid_atom_count": "Structure atom count is invalid; inspect the input structure.",
    "invalid_coordinate_frame": "Provide valid coordinates/frame for this operation.",
    "invalid_protonation_state": "Use a valid protonation state specification.",
    "protonation_state_override_failed": "Protonation override failed; check residue/state spec.",
    "invalid_terminal_cap": "Use a supported terminal cap type.",
    "terminal_cap_missing": "Add the required terminal cap before proceeding.",
    "terminal_cap_hydrogen_completion_failed": "Terminal-cap hydrogen completion failed; inspect capped residues.",
    "terminal_cap_hydrogen_completion_unavailable": "Terminal-cap hydrogen completion tool is unavailable in this runtime.",
    "terminal_cap_hydrogen_completion_changed_noncap_hydrogens": "Hydrogen completion altered non-cap hydrogens; review before continuing.",

    # --- ligands ---
    "ligand_chain_auto_included": "A ligand chain was auto-included; confirm it is intended.",
    "ligand_resname_chain_auto_included": "A ligand resname's chain was auto-included; confirm intent.",
    "associated_ligand_chain_auto_included": "An associated ligand chain was auto-included; confirm intent.",
    "associated_ligands_require_selection": "Select ligands explicitly with --include-ligand-resnames or the recommended IDs.",
    "unparametrizable_ligand_selected": "Placeholder residue (UNX/UNL/UNK) has no chemistry; drop it or predict a real ligand.",
    "blocking_ligand_failure": "Ligand chemistry failed but a protein-only artifact exists; follow workflow_recommendation.options (provide SMILES, exclude the ligand, or stop). Do not blindly retry.",
    "empty_ligand_resname_selection": "Provide at least one ligand resname or drop ligand from --include-types.",
    "requested_ligand_ids_not_found": "Requested ligand IDs are absent; use IDs from ligand_selection.",
    "requested_ligand_resnames_not_found": "Requested ligand resnames are absent in the structure.",
    "requested_ligand_resnames_not_in_selected_scope": "Requested ligand resnames are outside the selected chains/scope.",
    "invalid_ligand_chemistry": "Ligand chemistry is invalid; check SMILES/formal charge.",
    "ligand_chemistry_load_failed": "Failed to load ligand chemistry; verify the ligand file/SMILES.",
    "ligand_molecule_load_failed": "Failed to load the ligand molecule; verify inputs.",
    "ligand_formal_charge_mismatch": "Fix the ligand formal charge to match its chemistry.",
    "ligand_id_resname_mismatch": "Ligand ID and resname disagree; align the selection.",
    "ligand_template_coverage_failed": "Ligand template coverage failed; provide parameters or SMILES.",
    "ambiguous_ligand_residue_repair": "Ligand residue repair is ambiguous; specify the intended chemistry.",

    # --- glycans ---
    "glycan_forcefield_disabled": "Enable the glycan forcefield path to model glycans.",
    "glycan_linkage_mapping_failed": "Glycan linkage mapping failed; check glycosidic connectivity.",
    "unsupported_glycan_residue": "Glycan residue is unsupported; use a supported residue set.",
    "glycam_bond_plan_apply_failed": "GLYCAM bond plan failed to apply; inspect the linkage plan.",
    "glycam_bond_plan_unit_index_drift": "GLYCAM unit indices drifted; regenerate the bond plan.",
    "glycam_bond_plan_unit_index_invalid_but_identity_resolved": "GLYCAM unit index invalid but identity resolved; verify the plan.",
    "glycam_bond_plan_unit_index_out_of_range_but_identity_resolved": "GLYCAM unit index out of range but identity resolved; verify the plan.",
    "glycam_hydrogen_completion_failed": "GLYCAM hydrogen completion failed; inspect glycan hydrogens.",
    "glycam_normalization_changed_protein_hydrogens": "GLYCAM normalization changed protein hydrogens; review before continuing.",

    # --- mutation / phosphorylation ---
    "mutation_input_invalid": "Provide a valid mutation input specification.",
    "mutation_spec_invalid": "Fix the mutation spec format (e.g. A:GLU123ALA).",
    "mutation_validation_failed": "Mutation validation failed; inspect the reported residues.",
    "phospho_detection_requires_gemmi": "Install gemmi to detect phosphorylation sites.",
    "phospho_forcefield_unsupported": "Phosphorylation is unsupported by the chosen forcefield.",
    "phospho_forcefield_atom_type_mismatch": "Phospho atom types mismatch the forcefield; pick a compatible ff.",
    "removed_unsupported_5prime_terminal_phosphate": "A 5-prime terminal phosphate was removed; review the cleaned nucleic acid.",

    # --- modified nucleic acids (modxna) ---
    "invalid_modxna_parameters": "Provide valid modxna parameters.",
    "invalid_modxna_fragment_spec": "Fix the modxna fragment specification.",
    "modxna_modifications_required": "Specify at least one nucleic modification.",
    "modxna_missing_parent_merged_pdb": "Parent merged PDB is missing; repair the upstream node.",
    "modxna_missing_residue_mapping": "Residue mapping is missing; regenerate it.",
    "modxna_residue_mapping_stale": "Residue mapping is stale; regenerate against current structure.",
    "modxna_target_residue_not_found": "Target residue not found; check chain/number/resname.",
    "modxna_terminal_residue_unsupported": "Terminal residue modification is unsupported here.",
    "modxna_openmm_xml_required": "Provide the OpenMM XML required for modxna.",
    "modxna_pdb_rename_changed_structure": "PDB rename changed the structure; review before continuing.",
    "modxna_execution_failed": "modxna execution failed; inspect the structured error.",
    "modxna_tool_unavailable": "modxna tooling is unavailable in this runtime.",
    "unsupported_modified_nucleic_residue": "Modified nucleic residue is unsupported.",

    # --- metals ---
    "metal_pdb_file_not_found": "Metal PDB file not found; verify the path.",

    # --- side-chain / hydrogen packing (HPacker / nucleic) ---
    "hpacker_not_available": "HPacker is unavailable; run in a runtime that ships it.",
    "hpacker_failed": "HPacker failed; inspect the structured error.",
    "hpacker_no_output": "HPacker produced no output; treat the step as failed.",
    "hpacker_no_protein_residues": "No protein residues for HPacker; check the selection.",
    "hpacker_hydrogen_rebuild_failed": "HPacker hydrogen rebuild failed; inspect residues.",
    "nucleic_hydrogen_rebuild_failed": "Nucleic hydrogen rebuild failed; inspect residues.",
    "nucleic_hydrogen_rebuild_unavailable": "Nucleic hydrogen rebuild tool is unavailable in this runtime.",

    # --- forcefield / water ---
    "forcefield_water_blocked": "Use a supported forcefield/water pair, usually ff19SB + opc.",
    "forcefield_water_not_preferred": "Water model is allowed but not preferred; prefer opc with ff19SB.",
    "forcefield_water_legacy_warning": "Legacy water model detected; prefer the modern pairing.",
    "forcefield_water_recommended_alternative": "Switch to the recommended water model for this forcefield.",
    "forcefield_obsolete_blocked": "Selected forcefield is obsolete; use a supported one.",
    "forcefield_extra_xml_used": "Extra forcefield XML was applied; confirm it is intended.",
    "unknown_water_model": "Use a known water model (e.g. opc, tip3p, tip4pew, spce).",
    "openmm_fallback_unsupported_water_model": "OpenMM fallback cannot make this water; install AmberTools or pick tip3p/tip4pew/spce.",

    # --- solvation / box / membrane ---
    "explicit_solvent_box_dimensions_missing": "Build topology from a completed explicit-solvent solv node.",
    "explicit_ions_in_implicit_solvent": "Remove explicit ions before an implicit build, or use explicit/vacuum.",
    "unsupported_ion_for_water_model": "Use a water model whose ion XML supports the retained bare ion.",
    "solvation_topology_water_model_mismatch": "Match the topology water model to the solvation step.",
    "packmol_packing_quality_failed": "Packmol packing quality failed; adjust box/tolerance and retry.",
    "packmol_imperfect_primary_output_candidate": "Packmol primary output is imperfect; inspect candidates.",
    "membrane_patch_tiles_used": "Patch-tile membrane was assembled from a cached lipid patch; confirm it is appropriate.",
    "membrane_patch_cache_miss": "No cached membrane patch found in read-only mode; enable build or warm the cache.",
    "membrane_patch_builder_unavailable": "packmol-memgen is required to build a membrane patch cache miss.",
    "membrane_patch_build_failed": "Membrane patch packmol build failed; inspect the structured error.",
    "membrane_patch_build_no_output": "Membrane patch packmol build produced no output PDB.",
    "membrane_patch_build_invalid_output": "Membrane patch packmol build output is missing requested lipids.",
    "membrane_embedding_geometry_failed": "Membrane embedding geometry failed; inspect protein/bilayer placement.",
    "membrane_patch_invalid_input": "Input protein PDB for membrane embedding has no atoms.",
    "net_charge_exception": "Exact net-charge evaluation raised; membrane written without protein-charge neutralization.",
    "membrane_patch_invalid_patch": "Cached membrane patch PDB has no atoms; refresh the cache.",
    "membrane_patch_lipid_missing_after_carve": "Tiled patch insertion removed all requested lipids; adjust carve padding.",
    "membrane_patch_state_missing_positions": "Membrane patch state export has no positions; rebuild the patch.",
    "membrane_patch_state_missing_box": "Membrane patch state export has no box vectors; rebuild the patch.",
    "membrane_patch_state_export_failed": "Membrane patch state export failed; inspect the patch state.",
    "memembed_unavailable": "memembed not found in PATH; pass a pre-oriented structure with --preoriented.",
    "memembed_timeout": "memembed orientation timed out.",
    "memembed_failed": "memembed orientation failed; inspect the structured error.",
    "memembed_no_output": "memembed did not write an oriented PDB.",
    "memembed_empty_output": "memembed output had no solute atoms after cleanup.",
    "lipid21_external_bond_patching_failed": "Lipid21 external bond patching failed; inspect the lipid topology.",

    # --- implicit solvent ---
    "implicit_solvent_force_missing": "Implicit-solvent force is missing from the system.",
    "implicit_solvent_model_unsupported": "Use a supported implicit-solvent model (e.g. GBn2).",
    "implicit_solvent_topology_metadata_invalid": "Implicit-solvent topology metadata is invalid.",
    "implicit_solvent_topology_mismatch": "Match the run-time implicit solvent to the topology build.",
    "implicit_solvent_xml_ambiguous": "Implicit-solvent XML is ambiguous; disambiguate the inputs.",
    "implicit_solvent_xml_missing": "Provide the implicit-solvent XML input.",
    "implicit_solvent_explicit_box_conflict": "Implicit solvent conflicts with an explicit box; choose one regime.",

    # --- topology / system build ---
    "missing_xml_topology_inputs": "Run or repair the topo node that emits the XML triple.",
    "topology_pdb_not_found": "topology.pdb not found; rebuild the topo node.",
    "topology_validation_failed": "Topology validation failed; inspect the structured error.",
    "invalid_openmm_bundle": "OpenMM bundle is invalid; rebuild the system triple.",
    "missing_forcefield_xml": "Supply at least one OpenMM ForceField XML in forcefield_xml.",
    "invalid_nonbonded_method": "Use a supported nonbonded_method (e.g. PME, NoCutoff, CutoffPeriodic).",
    "invalid_constraints": "Use a supported constraints value: HBonds, AllBonds, or None.",
    "unknown_forcefield": "Use a supported protein force field (e.g. ff19SB or ff14SB).",
    "modern_system_hmr_mismatch": "Use the HMR setting baked into system.xml.",
    "modern_system_implicit_solvent_unsupported": "Modern system build does not support this implicit solvent.",
    "openmm_forcefield_init_failed": "ForceField init failed; check the XML bundle and residue templates.",
    "openmm_create_system_failed": "createSystem failed; inspect the error for missing templates/parameters.",
    "openmm_minimization_failed": "Energy minimization failed; inspect geometry/parameters in the error.",
    "openmm_serialization_failed": "Serializing the system/state failed; inspect the structured error.",
    "openmmforcefields_build_timeout": "System build timed out; increase timeout or reduce system size.",
    "openmmforcefields_build_memory_error": "System build ran out of memory; use a larger-memory runtime.",
    "openmmforcefields_build_failed": "System build failed; inspect the structured error before retrying.",
    "glycam_prepareforleap_failed": "cpptraj prepareforleap failed; inspect the glycan linkage inputs.",
    "glycam_topology_normalization_failed": "GLYCAM topology normalization failed; inspect the structured error.",
    "node_execution_context_invalid": "Node context is invalid; fix node type/conditions or branch a new node.",
    "ligand_protonation_charge_unreachable": "Requested ligand protonation/charge is unreachable; adjust the target state.",

    # --- OpenMM runtime / platforms ---
    "openmm_import_failed": "OpenMM import failed; run in a runtime with OpenMM installed.",
    "openmm_version_too_old": "Upgrade OpenMM to the required minimum version.",
    "openmm_platform_inspection_failed": "Platform inspection failed; report the runtime/GPU state.",
    "unknown_gpu_type": "GPU type is unknown; report the platform for scheduling.",

    # --- state / restart ---
    "state_xml_not_found": "state.xml not found; run/repair the producing node.",
    "state_xml_missing_positions": "state.xml lacks positions; regenerate the state.",
    "state_pdb_export_failed": "Exporting PDB from state failed; inspect the structured error.",
    "state_topology_atom_count_mismatch": "state and topology atom counts differ; rebuild the triple.",
    "restart_from_unavailable": "Requested restart point is unavailable; pick a valid parent state.",
    "continue_from_invalid_node_type": "continue-from must reference a valid node type.",
    "continue_from_not_prod": "continue-from must reference a prod node.",
    "continue_from_parents_conflict": "continue-from conflicts with the given parents; resolve one.",
    "inconsistent_minimized_artifacts": "Minimized artifacts are inconsistent; rerun minimization.",

    # --- minimization / equilibration / timestep ---
    "minimization_iterations_invalid": "Use a valid (non-negative) minimization iteration count.",
    "minimization_restraint_atoms_invalid": "Provide a valid restraint atom selection.",
    "equilibration_time_step_conflict": "Resolve the equilibration time-step conflict (HMR vs dt).",
    "timestep_unsupported": "Use a supported integration time step for this system.",

    # --- analyze ---
    "analyze_requires_parent": "analyze needs a parent node; create it with a valid parent.",
    "analyze_parent_missing": "The analyze parent is missing; reference a real node.",
    "analyze_parent_invalid_type": "analyze parent must be a valid producing node type.",
    "analyze_parents_mixed": "analyze parents are mixed/incompatible; use one consistent set.",
    "analyze_conditions_invalid": "Provide valid analyze conditions.",
    "comparison_requires_two_analyze": "Comparison needs exactly two analyze nodes.",

    # --- study logging ---
    "invalid_study_record_type": "Use record_study_log --record-type one of: decision, question, token_usage.",
    "study_record_fields_missing": "Provide the fields required by the chosen study record_type.",

    # --- Boltz-2 ---
    "boltz_sequence_required": "Provide at least one amino-acid sequence before running Boltz-2.",
    "boltz_num_models_invalid": "Use a positive integer for --num-models.",
    "boltz_affinity_requires_ligand": "Provide a ligand SMILES or omit --affinity.",
    "boltz_msa_file_missing": "Verify the custom MSA path or omit --msa-path.",
    "boltz_custom_msa_multimer_unsupported": "Use the MSA server for multimers, or prepare a Boltz YAML manually.",
    "boltz_chain_count_exceeded": "Reduce protein/ligand chains or split the prediction.",
    "boltz_backend_not_installed": "Install the isolated Boltz-2 backend venv: `mdclaw setup_model_backend --model boltz --device cuda`.",
    "boltz_execution_failed": "Report the structured error; check sequence, SMILES, MSA, runtime.",
    "boltz_no_structure_output": "Prediction produced no structure; do not continue to prep.",
    "boltz_source_attach_failed": "Preserve Boltz outputs and repair source-bundle registration.",

    # --- MODELLER ---
    "modeller_not_installed": "MODELLER is not installed; run in a runtime that ships it.",
    "modeller_license_env_missing": "Set the MODELLER license key env var (KEY_MODELLER).",
    "modeller_target_sequence_required": "Provide the target sequence for comparative modeling.",
    "modeller_target_sequence_conflict": "Target sequence conflicts with the alignment; resolve one.",
    "modeller_chain_count_mismatch": "Target and template chain counts differ; align them.",
    "modeller_loop_models_invalid": "Use a valid loop-model count.",
    "modeller_execution_failed": "MODELLER execution failed; inspect the structured error.",

    # --- SLURM / HPC policy ---
    "invalid_slurm_job_id": "Provide a valid SLURM job id.",
    "slurm_node_unavailable": "SLURM node/tooling is unavailable; check the cluster runtime.",
    "slurm_node_already_submitted": "This node was already submitted; do not resubmit.",
    "slurm_node_submission_in_progress": "Submission is in progress; wait before resubmitting.",
    "slurm_completed_without_node_completion": "SLURM job completed but the node did not; inspect artifacts.",
    "sbatch_directive_injection": "Reject injected sbatch directives; sanitize the submission.",
    "policy_partition_denied": "Requested partition is denied by policy; choose an allowed one.",
    "policy_partition_not_allowed": "Partition is not on the allowlist; pick an allowed partition.",
    "policy_cpus_exceeded": "Requested CPUs exceed the policy limit; reduce the request.",
    "policy_gpus_exceeded": "Requested GPUs exceed the policy limit; reduce the request.",
    "policy_nodes_exceeded": "Requested nodes exceed the policy limit; reduce the request.",
    "policy_memory_exceeded": "Requested memory exceeds the policy limit; reduce the request.",
    "policy_memory_unparseable": "Memory request is unparseable; use a valid size (e.g. 8G).",
    "policy_time_exceeded": "Requested time exceeds the policy limit; reduce the walltime.",
    "policy_time_unparseable": "Time request is unparseable; use HH:MM:SS or a valid format.",

    # --- structure preview / visual review ---
    "preview_structure_file_required": "Provide a structure file to preview.",
    "preview_structure_artifact_missing": "Preview structure artifact is missing; rebuild it.",
    "preview_structure_format_unsupported": "Use a supported structure format for preview.",
    "preview_style_unsupported": "Use a supported preview style.",
    "preview_camera_preset_unsupported": "Use a supported camera preset.",
    "preview_node_context_incomplete": "Provide complete node context for the preview.",
    "pymol_not_available": "PyMOL is unavailable; run in a runtime that ships it.",
    "pymol_preview_missing_output": "PyMOL preview produced no output; treat as failed.",
    "pymol_render_failed": "PyMOL render failed; inspect the structured error.",
    "pymol_render_timeout": "PyMOL render timed out; simplify the scene or raise the timeout.",
    "visual_review_node_context_incomplete": "Provide complete node context for visual review.",
    "visual_review_payload_invalid": "Provide a valid visual-review payload.",
    "visual_review_recommendation_unsupported": "Use a supported visual-review recommendation value.",
    "visual_review_reviewer_type_unsupported": "Use a supported visual-review reviewer type.",
    "visual_review_severity_unsupported": "Use a supported visual-review severity value.",
}
"""Mapping of guardrail ``code`` -> one-line agent action."""


def guardrail_action(code: str) -> str:
    """Return the recommended action for ``code``.

    Falls back to a generic instruction for unknown codes so callers never
    KeyError on a code that has not yet been registered.
    """
    return GUARDRAIL_CODES.get(
        code,
        "Unknown code; report code, message, errors, warnings, and hints to the user.",
    )
