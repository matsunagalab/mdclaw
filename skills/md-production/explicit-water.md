# Production MD: Explicit Water

## System Configuration

| Parameter | Value | Notes |
|---|---|---|
| Electrostatics | **PME** (periodic) | Cutoff 1.0 nm |
| Force field | ff19SB | `amber19-all.xml` |
| Water model | OPC (default) | Also: TIP3P-FB, SPC/E, TIP4P-Ew |
| Integrator | LangevinMiddleIntegrator | Friction 1/ps |
| Barostat | MonteCarloBarostat | Temperature must match integrator |
| Constraints | HBonds | Allows 4 fs timestep |
| Ensemble | NPT (300K, 1 bar) | |

### Timestep Guide

| Constraints | HMR | Max Timestep | Recommended |
|---|---|---|---|
| HBonds | No | 4 fs | 2 fs (conservative) or 4 fs |
| AllBonds | Yes (`hydrogenMass=4*amu`) | 4 fs | 4 fs |

---

## Production Run

### Local Execution

```bash
mdclaw --job-dir <job_dir> --node-id prod_001 run_production \
  --simulation-time-ns <user_specified> \
  --temperature-kelvin <T> \
  --output-frequency-ps 10.0
```

If the user does not specify a run length and `execution_mode=autonomous`,
use `--simulation-time-ns 0.1` as the default sanity check.

`prmtop_file`, `inpcrd_file`, `restart_from`, and `pressure_bar` are
auto-resolved from DAG ancestors. Ensemble is inherited from the `eq`
ancestor, so NPT eq states load with a matching barostat by default. For
extension/retry details, read `skills/md-production/restart.md`.

### SLURM Execution (HPC)

For long runs, multi-replicate sweeps, or fan-out across many systems,
hand off to the `/hpc-run` skill instead of writing the sbatch here. The
short version:

```bash
# Single node, linked to the DAG (note --job-dir / --node-id on submit_job
# — they stamp slurm_job_id onto nodes/prod_001/node.json and let
# check_job sync state back):
mdclaw submit_job \
  --job-dir <job_dir> --node-id prod_001 \
  --script "mdclaw --job-dir <job_dir> --node-id prod_001 run_production \
    --simulation-time-ns <user_specified> \
    --temperature-kelvin <T> --pressure-bar 1.0 --platform CUDA" \
  --partition gpu --gpus 1 --time-limit <estimated> --memory "32G"

# Many prod nodes in parallel (replicates or cross-system): one sbatch
# with --array=0-N-1 via submit_array_job — prefer this over a shell loop
# of submit_job calls. See /hpc-run for the full pattern.
mdclaw submit_array_job --tasks "$TASKS_JSON" --partition gpu --gpus 1 ...
```

Key properties:
- `--job-dir` / `--node-id` on `submit_job` and `submit_array_job` link the
  SLURM job id into `node.json.metadata` and track it in
  `.mdclaw_jobs.jsonl`. `check_job` then reflects SLURM state onto the DAG
  (RUNNING → `queued→running`, FAILED/TIMEOUT → `failed` + stderr tail).
- Inside the job script, omit `--prmtop-file` / `--inpcrd-file` /
  `--restart-from` — DAG auto-resolution takes care of all three.
- Full runbook (cluster inspection, policy, container config, monitoring
  with `/loop`, checkpoint extension): `/hpc-run`.

---

## Common Run Lengths

| Purpose | Time | Notes |
|---|---|---|
| Sanity check | 0.1 ns | Quick validation; default when autonomous and omitted |
| Short | 1-10 ns | Initial testing |
| Production | 50-500 ns | Conformational sampling |
| Extended | 1+ us | Slow processes (folding, binding) |

---

## GPU / HMR

```bash
# GPU selection
--platform CUDA --device-index "0"

# Disable HMR (not recommended)
--no-hmr --timestep-fs 2.0
```

## Restart / Extension

For planned extensions, create a new prod node with `--continue-from`. For
state-vs-checkpoint behavior, same-node retries, and stale-artifact handling,
read `skills/md-production/restart.md`.

---

## Membrane Systems

- Uses MonteCarloMembraneBarostat (XYIsotropic + ZFree)
- Longer equilibration recommended (0.5-1 ns NVT + 1 ns NPT)

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| SHAKE constraint failure | Bad geometry | Reduce to 2 fs, or re-prepare |
| NaN energies | Clashes | Re-equilibrate or re-prepare |
| Slow performance | GPU not detected | Check `--platform CUDA` |
| Barostat instability | Temperature mismatch | Match barostat and integrator T |
| `Ensemble mismatch: ... MonteCarloPressure ...` (structured error) | NPT-equilibrated state.xml loaded into an NVT prod context | Either let prod auto-inherit ensemble (omit `--pressure-bar`) or pass the eq's pressure explicitly. To run NVT prod intentionally, rerun the eq node with `--pressure-bar 0` to produce a barostat-free state. |

---

## Verify Output

Read `nodes/prod_001/node.json`:
- `status`: `"completed"`
- `artifacts`: trajectory, final_structure, checkpoint, energy
- `metadata`: simulation_time_ns, platform, hmr, steps
