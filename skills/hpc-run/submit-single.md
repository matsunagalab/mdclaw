# Single SLURM Job Submission

Use `submit_job` when one DAG node maps to one SLURM job.

```bash
JD=$(realpath job_4m3j_B)

mdclaw create_node --job-dir "$JD" --node-type min --parent-node-ids <topo_node_id> \
  --label minimized --conditions '{"max_iterations":5000}'

mdclaw submit_job \
  --job-dir "$JD" --node-id <min_node_id> \
  --script "mdclaw --job-dir $JD --node-id <min_node_id> run_minimization \
    --max-iterations 5000 --platform CUDA" \
  --job-name min_4m3j_B \
  --partition gpu --gpus 1 --cpus-per-task 4 \
  --time-limit "00:30:00" --memory "32G"

mdclaw create_node --job-dir "$JD" --node-type eq --parent-node-ids <min_node_id> \
  --label 300K --conditions '{"temperature_kelvin":300,"pressure_bar":1.0}'

mdclaw submit_job \
  --job-dir "$JD" --node-id <eq_node_id> \
  --script "mdclaw --job-dir $JD --node-id <eq_node_id> run_equilibration \
    --temperature-kelvin 300 --pressure-bar 1.0 --platform CUDA" \
  --job-name eq_4m3j_B \
  --partition gpu --gpus 1 --cpus-per-task 4 \
  --time-limit "00:30:00" --memory "32G"
```

The `--platform CUDA` in each run command is what makes these GPU jobs; the
`--partition gpu --gpus 1` above are explicit-and-recommended, not required. See
the GPU rule in `skills/hpc-run/SKILL.md` "Critical Rules" for the
auto-detection behavior and multi-GPU / GRES forms.

Do not pass `--system-xml-file`, `--topology-pdb-file`, `--state-xml-file`, or `--restart-from` in normal DAG
commands. The compute-node CLI resolves topology and restart inputs from the DAG.

For `min -> eq -> prod`, create downstream nodes on the login node and submit
them with `afterok:<upstream_slurm_id>` dependencies.
