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
      "node_id": "prod_001",
      "command": "mdclaw --job-dir /abs/job_rep1 --node-id prod_001 run_production --simulation-time-ns 100 --platform CUDA"
    }
  ]'
```

All array tasks share one `--gpus`/`--gres` value. As with `submit_job`, if any
task command uses `--platform CUDA` (or `OpenCL`) and you pass neither `--gpus`
nor `--gres`, the whole array is auto-set to `--gpus 1` with a warning. Keep
`--gpus 1` explicit for clarity, or use `--gres gpu:<type>:1` on GRES-only
clusters.

Use arrays for small replicate sets with similar runtime and failure likelihood.
For large campaigns with heterogeneous systems, prefer individual `submit_job`
calls so one failed upstream node does not invalidate the whole batch shape.
