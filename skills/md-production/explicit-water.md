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
  --temperature-kelvin <T> --pressure-bar 1.0 \
  --output-frequency-ps 10.0
```

`prmtop_file`, `inpcrd_file`, and `restart_from` are all auto-resolved from DAG ancestors
(`topo` for topology, `eq` for checkpoint). To override, pass explicitly.

### SLURM Execution (HPC)

```bash
mdclaw submit_job \
  --script "mdclaw --job-dir <job_dir> --node-id prod_001 run_production \
    --simulation-time-ns <user_specified> \
    --temperature-kelvin <T> --pressure-bar 1.0 \
    --platform CUDA" \
  --job-name md_<name> \
  --partition <partition> --gpus 1 \
  --time-limit <estimated> --memory "32G"
```

`--job-dir` is auto-resolved to absolute path by the CLI, so SLURM compute nodes
can find all files without manual `realpath` conversion.

---

## Common Run Lengths

| Purpose | Time | Notes |
|---|---|---|
| Sanity check | 0.1 ns | Quick validation |
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

## Checkpoint / Restart

Reuse the same `--node-id` for restarts. The tool detects existing trajectory
and appends new frames.

```bash
# Restart from mid-run checkpoint (same node)
mdclaw --job-dir <job_dir> --node-id prod_001 run_production \
  --simulation-time-ns 100.0 --platform CUDA \
  --restart-from <job_dir>/nodes/prod_001/artifacts/checkpoint.chk
```

- `--simulation-time-ns` is the **total** target time, not additional
- Binary checkpoint is platform-specific (CUDA checkpoint cannot load on CPU)

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

---

## Verify Output

Read `nodes/prod_001/node.json`:
- `status`: `"completed"`
- `artifacts`: trajectory, final_structure, checkpoint, energy
- `metadata`: simulation_time_ns, platform, hmr, steps
