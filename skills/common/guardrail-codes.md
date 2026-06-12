# Guardrail Codes

Branch on stable `code` values from tool JSON. Do not parse stderr or long
human-readable messages.

| Code | Action |
|------|--------|
| `invalid_json_input` | Fix the JSON string or use `--json-input` with valid JSON. |
| `file_not_found` | Verify the path and rerun only after the file exists. |
| `tool_not_available` | Stop local execution and report the missing external tool. |
| `missing_pdb_file` | Use DAG auto-resolution or provide a valid PDB/mmCIF path. |
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
| `referenced_node_missing` | A parent/dependency id does not exist. Use IDs from `inspect_job` / `plan_next`. |

If a code is unknown, report `code`, `message`, `errors`, `warnings`, and
`hints` to the user instead of inventing a workaround.
