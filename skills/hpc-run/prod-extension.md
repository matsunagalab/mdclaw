# Production Extension On HPC

Use `continue_from` when extending a completed production node.

```bash
JD=$(realpath job_4m3j_B)

mdclaw create_node --job-dir "$JD" --node-type prod --continue-from prod_001 \
  --label extend_100ns --conditions '{"simulation_time_ns":100}'

mdclaw submit_job \
  --job-dir "$JD" --node-id prod_002 \
  --script "mdclaw --job-dir $JD --node-id prod_002 run_production \
    --simulation-time-ns 100 --platform CUDA" \
  --job-name prod_4m3j_B_ext \
  --partition gpu --gpus 1 --cpus-per-task 4 \
  --time-limit "24:00:00" --memory "32G"
```

Runtime resolution restarts only from the named `prod` node. If that node has no
`state` or `checkpoint` artifact, `run_production` fails before touching OpenMM.

`simulation_time_ns` is the additional time for this call, not the cumulative
timeline length.
