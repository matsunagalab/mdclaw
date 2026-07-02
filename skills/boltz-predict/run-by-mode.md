# Run Boltz-2 By Mode

For normal MDClaw DAG work, run Boltz-2 in node mode so the prediction becomes
the job's `source` bundle. Create the node first:

```bash
mdclaw create_node --job-dir <job_dir> --node-type source
mdclaw --job-dir <job_dir> --node-id <source_node_id> boltz2_protein_from_seq \
  <mode flags from the table below>
```

`--amino-acid-sequence-list` takes one or more single-letter sequences; two or
more sequences make a complex (dimer, trimer, ...). Use sequences exactly as the
user provided.

| Mode | Required flags | Optional |
|---|---|---|
| Single protein | `--amino-acid-sequence-list "SEQ"` | `--num-models N` |
| Protein-protein complex | `--amino-acid-sequence-list "SEQ1" "SEQ2" ...` | `--num-models N` |
| Protein-ligand complex | `--amino-acid-sequence-list "SEQ"` and `--smiles-list "CCO"` (pre-validated) | `--affinity`, `--msa-path`, `--num-models N` |

Notes:

- `--smiles-list`: omit for protein-only predictions; validate first (see
  `skills/boltz-predict/ligand-prep.md`).
- `--msa-path`: omit to use the Boltz MSA server.
- `--affinity`: protein-ligand only; omit or use `--no-affinity` to disable.
- `--num-models`: default `1`; see `skills/boltz-predict/prediction-options.md`.

Example (protein-ligand, ensemble of 3, affinity on):

```bash
mdclaw --job-dir <job_dir> --node-id <source_node_id> boltz2_protein_from_seq \
  --amino-acid-sequence-list "MVLSPADKTNVKAAW..." \
  --smiles-list "CCO" \
  --affinity \
  --num-models 3
```
