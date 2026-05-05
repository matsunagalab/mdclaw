# Study Campaigns On HPC

Use an optional `study_dir` for multi-system campaigns. Do not create multiple
`source` roots in one job.

Loop:

1. Read `study.json`.
2. Resolve each registered `jobs[].job_dir` relative to the study directory.
3. Read each job's `progress.json` and identify ready `eq` or `prod` nodes.
4. Use `submit_array_job` for homogeneous, low-failure batches.
5. Use per-node `submit_job` plus dependencies for heterogeneous systems or
   expected upstream failures.
6. Monitor with `list_tracked_jobs --sync --job-dir <job_dir>` per job.
7. Record scientific or operational decisions with `record_study_decision`.

Keep SLURM state in each job's node metadata. The study is an index, not a
replacement for per-system DAG state.
