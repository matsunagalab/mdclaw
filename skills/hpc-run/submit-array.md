# SLURM Array Submission

Use `submit_array_job` for homogeneous, low-failure batches where each task maps
to one DAG node.

```bash
mdclaw submit_array_job \
  --job-name prod_replicates \
  --partition gpu \
  --gpus 1 \
  --cpus-per-task 4 \
  --time-limit "24:00:00" \
  --memory "32G" \
  --max-concurrent 4 \
  --tasks '[
    {
      "job_dir": "/abs/job_rep1",
      "node_id": "<prod_node_id>",
      "command": "mdclaw --job-dir /abs/job_rep1 --node-id <prod_node_id> run_production --simulation-time-ns 100 --platform CUDA"
    }
  ]'
```

All array tasks share one `--gpus`/`--gres` value; the same GPU auto-detection
as `submit_job` applies (see `skills/hpc-run/SKILL.md` "Critical Rules").

Use arrays for small replicate sets with similar runtime and failure likelihood.
For large campaigns with heterogeneous systems, prefer individual `submit_job`
calls so one failed upstream node does not invalidate the whole batch shape.
