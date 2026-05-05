---
name: HPC Run
description: "SLURM-based HPC submission for MDClaw workflow nodes. Handles cluster inspection, single-node and job-array submission, status sync to the DAG, and production restart extensions."
---

# HPC Run Skill

Read `skills/common/preamble.md`, `skills/common/tool-output.md`, and
`skills/common/node-cli-patterns.md` before acting.

Use this skill when the user wants to run equilibration or production nodes on
SLURM, submit multiple replicates or systems, monitor/recover jobs, configure
cluster policy, or extend production runs.

Structure preparation remains a login-node or interactive step. HPC submission
starts after a `topo` node exists and the next `eq` or `prod` node can resolve
its inputs from the DAG.

## Route To The Right Runbook

- Cluster discovery, resource policy, or container setup:
  `skills/hpc-run/discovery-policy.md`
- One DAG node as one SLURM job:
  `skills/hpc-run/submit-single.md`
- Homogeneous batches or replicate arrays:
  `skills/hpc-run/submit-array.md`
- Monitoring, status sync, logs, and recovery:
  `skills/hpc-run/monitor-recover.md`
- Extending a completed production node:
  `skills/hpc-run/prod-extension.md`
- Multi-system study campaigns:
  `skills/hpc-run/study-campaigns.md`

## Critical Rules

- Always pass both `--job-dir` and `--node-id` when submitting or running a DAG
  workflow node.
- Do not pass `--prmtop-file`, `--inpcrd-file`, or `--restart-from` in normal
  DAG SLURM commands; resolver logic handles these.
- `COMPLETED` SLURM state alone does not mark a node complete. The MDClaw tool
  running inside the job owns the final `complete_node` call.
- Use arrays only for homogeneous, low-failure task sets. Use individual jobs
  with dependencies when failure isolation matters.
