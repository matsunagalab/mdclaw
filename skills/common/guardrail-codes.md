# Guardrail Codes

Branch on stable `code` values from tool JSON. Do not parse stderr or long
human-readable messages.

| Code | Action |
|------|--------|
| `invalid_json_input` | Fix the JSON string or use `--json-input` with valid JSON. |
| `file_not_found` | Verify the path and rerun only after the file exists. |
| `tool_not_available` | Stop local execution and report the missing external tool. |
| `missing_pdb_file` | Use DAG auto-resolution or provide a valid PDB/mmCIF path. |
| `pdbfixer_missing_residues_out_of_scope` | Regenerate the source structure with MODELLER or Boltz-2 instead of retrying PDBFixer repair. |
| `boltz_sequence_required` | Ask for at least one amino-acid sequence before running Boltz-2. |
| `boltz_num_models_invalid` | Use a positive integer for `--num-models`. |
| `boltz_affinity_requires_ligand` | Provide at least one ligand SMILES or omit `--affinity`. |
| `boltz_msa_file_missing` | Verify the custom MSA file path or omit `--msa-path` to use the MSA server. |
| `boltz_custom_msa_multimer_unsupported` | Use the MSA server for multimers, or prepare a Boltz YAML manually. |
| `boltz_chain_count_exceeded` | Reduce the number of protein/ligand chains or split the prediction. |
| `boltz_executable_not_found` | Stop local execution and report that Boltz-2 is unavailable in the runtime. |
| `boltz_execution_failed` | Report the structured error and inspect sequence, SMILES, MSA, and runtime availability. |
| `boltz_no_structure_output` | Treat the prediction as failed; do not continue to prep without a source candidate. |
| `boltz_source_attach_failed` | Preserve Boltz outputs and repair source-bundle registration before continuing. |
| `missing_xml_topology_inputs` | Run or repair the topo node that should emit the XML triple. |
| `forcefield_water_blocked` | Use a supported forcefield/water pair, usually `ff19SB + opc`. |
| `explicit_solvent_box_dimensions_missing` | Build topology from a completed explicit-solvent `solv` node. |
| `explicit_ions_in_implicit_solvent` | Remove explicit ions before an implicit build, or choose explicit solvent / deliberate vacuum. |
| `implicit_solvent_topology_mismatch` | Match the run-time implicit solvent to the topology build. |
| `modern_system_hmr_mismatch` | Use the HMR setting baked into `system.xml`. |
| `parent_not_completed` | Complete or repair the parent node before running this node. |
| `parent_type_invalid` | Create a new node with a legal parent type for the target stage. |
| `condition_missing` | Pass actual tool parameters that cover every declared node condition. |
| `condition_mismatch` | Recreate the node or rerun with parameters matching its conditions. |
| `node_context_required` | Workflow tool ran without node context. Create the node, then run it with both `--job-dir` and `--node-id`. |
| `node_id_requires_job_dir` | `--node-id` was passed without `--job-dir`. Pass both together. |
| `missing_required_arguments` | Add the listed required flags (see `mdclaw --list-json`). |
| `invalid_node_type` | Use one of: source, prep, solv, topo, min, eq, prod, analyze. |
| `source_already_exists` | One source per job. Add structures to the existing source bundle or use another job. |
| `referenced_node_missing` | A parent/dependency id does not exist. Use IDs from `inspect_job`, `explain_node`, or `create_node`. |

If a code is unknown, report `code`, `message`, `errors`, `warnings`, and
`hints` to the user instead of inventing a workaround.
