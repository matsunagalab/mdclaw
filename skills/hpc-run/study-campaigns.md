# Study Campaigns On HPC

Use the `study_dir` as the campaign entry point. A single-system campaign can
still have one job, while multi-system campaigns register multiple jobs. Do
not create multiple `source` nodes in one job; put source alternatives in that
job's source bundle or split scientifically distinct systems into separate
study jobs.

Loop:

1. Read `study.json`.
2. Resolve each registered `jobs[].job_dir` relative to the study directory.
3. Read each job's `progress.json` and identify ready `eq` or `prod` nodes.
4. Use `submit_array_job` for homogeneous, low-failure batches.
5. Use per-node `submit_job` plus dependencies for heterogeneous systems or
   expected upstream failures.
6. Monitor with `list_tracked_jobs --sync --job-dir <job_dir>` per job.
7. Record scientific or operational decisions with `record_study_log --record-type decision`.

Keep SLURM state in each job's node metadata. The study is an index, not a
replacement for per-system DAG state.
