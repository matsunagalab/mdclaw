---
name: hpc-run
description: "SLURM-based HPC submission for MDClaw workflow nodes. Handles cluster inspection, single-node and job-array submission, status sync to the DAG, and production restart extensions."
---

# HPC Run Skill

Follow `skills/common/preamble.md`, `skills/common/run-loop.md` (the canonical
node loop), and `skills/common/tool-output.md` for error handling.

Use this skill when the user wants to run minimization, equilibration, or
production nodes on SLURM, submit multiple replicates or systems, monitor/recover
jobs, configure cluster policy, or extend production runs.

Structure preparation remains a login-node or interactive step. HPC submission
starts after a `topo` node exists and the next `min`/`eq`/`prod` node can resolve
its inputs from the DAG.

## Step 0: Confirm

- `job_dir` and the `node_id` to submit (from `create_node`).
- Current DAG state via `mdclaw inspect_job --job-dir <job_dir>`: a completed
  `topo` (and, for `prod`, a completed `eq`) parent, and no conflicting running
  work.
- Cluster resources/policy if unknown: inspect with `mdclaw inspect_cluster`
  and `mdclaw show_policy`; when policy is missing or the user gives limits,
  set it explicitly with `mdclaw set_policy` (partitions, GPU/time/memory
  caps, default partition). For containerized compute nodes, run
  `mdclaw configure_container --image /abs/path/mdclaw.sif --extra-flags "--nv"`;
  submission tools then auto-bind each task's `job_dir`.

## Route To The Right Guidance

- One DAG node as one SLURM job:
  `skills/hpc-run/submit-single.md`
- Homogeneous batches or replicate arrays:
  `skills/hpc-run/submit-array.md`
- Monitoring, status sync, logs, and recovery:
  `skills/hpc-run/monitor-recover.md`
- Extending a completed production node:
  `skills/hpc-run/prod-extension.md`
- Multi-system study campaigns: resolve each registered `study.json`
  `jobs[].job_dir`, then submit and monitor per job with the pages above.
  The study is an index; keep SLURM state in each job's node metadata.

## Critical Rules

- Always pass both `--job-dir` and `--node-id` when submitting or running a DAG
  workflow node.
- Do not pass `--system-xml-file`, `--topology-pdb-file`, `--state-xml-file`, or `--restart-from` in normal
  DAG SLURM commands; resolver logic handles these.
- `COMPLETED` SLURM state alone does not mark a node complete. The MDClaw tool
  running inside the job owns the final `complete_node` call.
- Use arrays only for homogeneous, low-failure task sets. Use individual jobs
  with dependencies when failure isolation matters.
- GPU resources stay in sync with the OpenMM platform automatically. When a
  node's run command uses `--platform CUDA` (or `--platform OpenCL`) and you
  pass neither `--gpus` nor `--gres`, `submit_job` / `submit_array_job` auto-set
  `--gpus 1` and emit a warning, so a CUDA run never lands on a CPU-only node.
  On a GPU cluster, default `min`/`eq`/`prod` to `--platform CUDA` so they
  request a GPU. Pass `--gpus N` for multi-GPU, or `--gres gpu:<type>:N` on
  clusters that require GRES form (`--gres` also suppresses the autodetection).
  `--platform auto` does NOT trigger a GPU request on HPC: without an allocated
  GPU it falls back to CPU, so make GPU intent explicit with `--platform CUDA`.
