# Single SLURM Job Submission

Use `submit_job` when one DAG node maps to one SLURM job.

```bash
JD=$(realpath job_4m3j_B)

mdclaw create_node --job-dir "$JD" --node-type eq --parent-node-ids topo_001 \
  --label 300K --conditions '{"temperature_kelvin":300,"pressure_bar":1.0}'

mdclaw submit_job \
  --job-dir "$JD" --node-id eq_001 \
  --script "mdclaw --job-dir $JD --node-id eq_001 run_equilibration \
    --temperature-kelvin 300 --pressure-bar 1.0 --platform CUDA" \
  --job-name eq_4m3j_B \
  --partition gpu --gpus 1 --cpus-per-task 4 \
  --time-limit "00:30:00" --memory "32G"
```

Do not pass `--prmtop-file`, `--inpcrd-file`, or `--restart-from` in normal DAG
commands. The compute-node CLI resolves topology and restart inputs from the DAG.

For `eq -> prod`, create both nodes on the login node and submit prod with an
`afterok:<eq_slurm_id>` dependency.
