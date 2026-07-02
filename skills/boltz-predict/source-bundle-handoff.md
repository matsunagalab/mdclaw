# Results, Source Bundle, And Handoff

## Result fields

`boltz2_protein_from_seq` returns:

- `success`: bool — True if prediction completed.
- `job_id`, `output_dir`: identifier and results directory.
- `predicted_pdb_files`: list of predicted PDB/mmCIF structures, sorted by Boltz
  model index when filenames contain `_model_N`; multiple files when
  `--num-models > 1`.
- `confidence_scores`: dict — confidence JSON content when Boltz writes it.
- `affinity_scores` (if `--affinity`): `affinity_probability_binary` (higher =
  more confident binding) and `affinity_pred_value` (lower = stronger binding;
  reported as `log10(IC50)` with IC50 in `uM`).
- `warnings`: list of non-critical warnings.

## Source-node metadata

When `job_dir` and `node_id` point to a `source` node, the output is normalized
into the standard source bundle:

```text
nodes/<source_node_id>/artifacts/source_bundle.json
nodes/<source_node_id>/artifacts/candidates/<candidate_id>.pdb
```

Per-candidate Boltz info belongs in `source_bundle.json`, not only in run-level
metadata: `origin.boltz_rank` (one-based rank), `origin.boltz_model_index`
(zero-based `_model_N`), `origin.boltz_output_file`, `origin.confidence_file`,
`metrics.confidence_score` (for quick ranking), and `metrics.confidence` (full
confidence JSON). Run-level details (`num_models_requested`, `boltz_output_dir`,
`input_yaml`, `sequences`, `smiles_list`, affinity scores) stay in the source
node metadata.

List candidates through the tool instead of asking the user to open JSON:

```bash
mdclaw list_source_candidates --job-dir <job_dir> --node-id <source_node_id>
```

## Handoff

Hand off to MD preparation using the canonical procedure in
`skills/common/md-handoff.md` (present candidates, create `prep`, run
`prepare_complex --source-candidate-id <candidate_id>`, then follow
`skills/md-prepare/SKILL.md`).
