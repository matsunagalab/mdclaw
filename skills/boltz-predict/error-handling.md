# Boltz Error Handling

Branch on the structured `code`; never parse stderr.

| Issue / `code` | Action |
|---|---|
| SMILES validation fails | Ask the user to check the chemical name or provide corrected SMILES |
| PubChem lookup fails | Ask the user to provide SMILES directly |
| `boltz_sequence_required` | Ask for at least one amino-acid sequence |
| `boltz_num_models_invalid` | Use `--num-models 1` or a larger positive integer |
| `boltz_affinity_requires_ligand` | Provide at least one valid ligand SMILES or omit `--affinity` |
| `boltz_msa_file_missing` | Verify the MSA path or omit `--msa-path` to use the MSA server |
| `boltz_custom_msa_multimer_unsupported` | Use the MSA server for multimers or prepare Boltz YAML manually |
| `boltz_chain_count_exceeded` | Split the prediction or reduce the number of protein/ligand chains |
| `boltz_executable_not_found` | Stop local execution and report that Boltz-2 is unavailable in the runtime |
| `boltz_execution_failed` | Report the structured error and check sequence/SMILES/MSA inputs |
| `boltz_no_structure_output` | Treat as a failed prediction; do not continue to prep without a source candidate |
| `boltz_source_attach_failed` | Preserve the Boltz output directory and repair source-bundle registration before continuing |
