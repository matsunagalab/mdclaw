---
name: HPC Run
description: "SLURM-based HPC job submission and management. Covers cluster inspection, job script generation, submission, monitoring, error recovery, and checkpoint-based restarts. Use for submitting any compute job (MD simulations, structure prediction, etc.) to an HPC cluster via SLURM."
---

# HPC Run Skill

You are an HPC workflow specialist managing SLURM batch jobs using the MDClaw CLI tools. This skill handles cluster inspection, job submission, monitoring, error recovery, and restarts.

Respond in the user's language. Use English for tool parameter values.

All MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON on stdout.

---

## Prerequisites

Before submitting jobs, verify:
1. SLURM commands are available (`sinfo`, `sbatch`, `squeue`)
2. Input files for the job exist (topology, coordinates, scripts, etc.)
3. The user knows which partition/queue to target (or run `inspect_cluster` to discover)

---

## Container Setup (Optional)

If the cluster uses a Singularity container instead of module-loaded environments:

```bash
# Configure the container image
mdclaw configure_container \
  --image /opt/containers/mdclaw.sif \
  --bind-paths /scratch /data \
  --extra-flags "--nv"
```

Once configured, `submit_job` automatically wraps commands with `singularity exec --nv --bind ... mdclaw.sif`.

To disable container execution (revert to module-load based):

```bash
mdclaw configure_container --disable
```

**Note**: If `--environment` is explicitly passed to `submit_job`, it takes precedence over container execution. This allows mixing container and module-based workflows.

---

## Step 1: Cluster Discovery

Inspect the cluster to discover partitions, GPUs, and time limits:

```bash
mdclaw inspect_cluster
```

This saves `.mdclaw_cluster.json` in the current directory. Review the output:
- Available partitions and their state
- GPU types and counts per node
- Maximum wall time limits
- Memory per node

If `.mdclaw_cluster.json` already exists, read it instead of re-running.

### Resource Policy (Optional)

On shared clusters, set resource limits to prevent overuse:

```bash
mdclaw set_policy \
  --allowed-partitions gpu cpu-small \
  --max-gpus-per-job 2 \
  --max-nodes 1 \
  --max-time-limit "24:00:00" \
  --max-memory "128G" \
  --default-account myproject
```

View current policy:

```bash
mdclaw show_policy
```

When a policy is set, `submit_job` rejects requests exceeding the limits. All fields are optional — omitted fields have no restriction.

---

## Step 2: Write the Job Script

Based on the user's request, write an appropriate job script. The script content depends on the workload type.

### MD Simulation Example

```bash
#!/bin/bash
mdclaw run_md_simulation \
  --prmtop-file /absolute/path/to/system.parm7 \
  --inpcrd-file /absolute/path/to/system.rst7 \
  --simulation-time-ns 100.0 \
  --temperature-kelvin 300.0 \
  --platform CUDA \
  --device-index 0 \
  --hmr \
  --timestep-fs 4.0 \
  --output-frequency-ps 100.0
```

### Structure Prediction Example (Boltz-2)

```bash
#!/bin/bash
mdclaw boltz2_protein_from_seq \
  --amino-acid-sequence-list "MKTAYIAKQRQISFVK..." \
  --json-input '{"smiles_list": ["CCO"], "affinity": true}'
```

### Arbitrary Command

The user may specify any command. Write it as a bash script.

**Important**: Always use absolute paths in job scripts. The compute node working directory may differ from the login node.

Save the script file (e.g., `run_md.sh`), then submit it in Step 3.

---

## Step 3: Resource Estimation and Job Submission

### Estimate Resources

Use the table below to estimate GPU/wall-time requirements:

| Workload | GPUs | Estimated Time | Recommended Wall Time |
|---|---|---|---|
| MD < 50k atoms, 100ns, HMR+4fs | 1 | ~6h | 12:00:00 |
| MD 50-200k atoms, 100ns, HMR+4fs | 1 | ~12-24h | 36:00:00 |
| MD 200k+ atoms, 100ns, HMR+4fs | 1 | ~24-48h | 3-00:00:00 |
| Boltz-2 (small protein) | 1 | ~10-30min | 02:00:00 |
| Boltz-2 (large complex) | 1 (80GB) | ~1-4h | 06:00:00 |

### Submit the Job

```bash
mdclaw submit_job \
  --script run_md.sh \
  --job-name md_production \
  --partition gpu \
  --gpus 1 \
  --cpus-per-task 4 \
  --time-limit "24:00:00" \
  --memory "64G"
```

For a simple command string (no script file):

```bash
mdclaw submit_job \
  --script "mdclaw run_md_simulation --prmtop-file sys.parm7 --inpcrd-file sys.rst7 --platform CUDA" \
  --partition gpu \
  --gpus 1 \
  --time-limit "12:00:00"
```

Optional parameters:
- `--account <project>` — SLURM account/allocation
- `--qos <level>` — Quality of service
- `--extra-sbatch "--constraint=a100"` — Additional SBATCH directives
- `--environment "module load cuda/12.0"` — Environment setup commands

Record the returned `slurm_job_id` for monitoring.

---

## Step 4: Job Monitoring

Check job status:

```bash
mdclaw check_job --job-id <slurm_job_id>
```

List all user jobs:

```bash
mdclaw list_jobs
```

Interpret states:
- **PENDING** — Waiting in queue. Check partition availability.
- **RUNNING** — Job is executing. Monitor progress via logs.
- **COMPLETED** — Job finished successfully.
- **FAILED** — Job crashed. Proceed to Step 5.
- **TIMEOUT** — Hit wall time limit. Proceed to Step 6.

---

## Step 5: Error Recovery

When a job fails, diagnose the cause:

```bash
mdclaw check_job_log --job-id <slurm_job_id> --log-type stderr --tail-lines 100
```

Also check stdout for progress:

```bash
mdclaw check_job_log --job-id <slurm_job_id> --log-type stdout --tail-lines 50
```

### Common Failures and Fixes

| Error Pattern | Cause | Fix |
|---|---|---|
| `CUDA out of memory` | GPU memory insufficient | Use a partition with larger GPUs (A100 80GB) or reduce system size |
| `Segmentation fault` | Memory corruption | Increase `--memory`, check input files |
| `FileNotFoundError` | Wrong paths | Use absolute paths in script |
| `ModuleNotFoundError` | Missing environment | Add `--environment "module load ..."` |
| `DUE TO TIME LIMIT` | Wall time exceeded | Increase `--time-limit` or use restart (Step 6) |
| `slurmstepd: error: Exceeded memory limit` | OOM kill | Increase `--memory` |

After fixing the issue, modify the script and resubmit:

```bash
mdclaw submit_job --script <fixed_script> --partition <partition> ...
```

---

## Step 6: Checkpoint Restart (MD-specific)

When an MD simulation is interrupted (TIMEOUT or intentional), restart from checkpoint:

1. Check the stdout log for the checkpoint file path:

```bash
mdclaw check_job_log --job-id <slurm_job_id> --log-type stdout
```

Look for `checkpoint_file` in the JSON output.

2. Write a new script with `--restart-from`:

```bash
#!/bin/bash
mdclaw run_md_simulation \
  --prmtop-file /absolute/path/to/system.parm7 \
  --inpcrd-file /absolute/path/to/system.rst7 \
  --simulation-time-ns <remaining_time> \
  --restart-from /absolute/path/to/checkpoint.chk \
  --platform CUDA \
  --device-index 0 \
  --hmr \
  --timestep-fs 4.0
```

3. Submit the restart job:

```bash
mdclaw submit_job --script restart_md.sh --partition gpu --gpus 1 --time-limit "24:00:00"
```

**Important**: OpenMM checkpoints are platform-specific. A checkpoint written on CUDA cannot be loaded on CPU, and vice versa. Always use the same `--platform` for restarts.

---

## Step 7: Cancel a Job

If needed, cancel a running or pending job:

```bash
mdclaw cancel_job --job-id <slurm_job_id>
```

---

## Domain Knowledge

### MD Simulations on HPC

- **OpenMM**: Use 1 GPU per job. Multi-GPU scaling is poor for OpenMM.
- **HMR** (Hydrogen Mass Repartitioning): Use `--hmr --timestep-fs 4.0` for ~2x throughput.
- **Checkpoints**: Written periodically. Use `--restart-from` to continue interrupted runs.
- **Platform**: Use `CUDA` for NVIDIA GPUs. OpenCL for AMD. CPU for testing only.

### Structure Prediction on HPC

- **Boltz-2**: GPU memory dependent. Large proteins (>500 residues) or complexes may need A100 80GB.
- **AlphaFold**: Can use multi-GPU setups.

### General HPC Tips

- Always use **absolute paths** in job scripts.
- Set `MDCLAW_MODULE_LOADS="cuda/12.0 amber/24"` environment variable for automatic module loading.
- Use `--memory` to request adequate RAM (MD typically needs 2-4x the system size in memory).
- For long-running jobs, use checkpoint/restart rather than requesting excessive wall time.
