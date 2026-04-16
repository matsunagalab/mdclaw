# Production MD: Explicit Water

## System Configuration

| Parameter | Value | Notes |
|---|---|---|
| Electrostatics | **PME** (periodic) | Cutoff 1.0 nm, ewaldErrorTolerance=0.0005 |
| Force field | ff19SB | `amber19-all.xml` |
| Water model | OPC (default) | Also: TIP3P-FB, SPC/E, TIP4P-Ew |
| Integrator | LangevinMiddleIntegrator | More accurate configurational sampling than standard Langevin |
| Thermostat | Langevin (built into integrator) | Friction 1/ps |
| Barostat | MonteCarloBarostat | Temperature should match integrator (OpenMM requirement) |
| Constraints | HBonds | Allows up to 4 fs timestep with LangevinMiddle |
| Ensemble | NPT (300K, 1 bar) | |

### Timestep Guide

| Constraints | HMR | Max Timestep | Recommended |
|---|---|---|---|
| HBonds | No | 4 fs | 2 fs (conservative) or 4 fs |
| AllBonds | Yes (`hydrogenMass=4*amu`) | 4 fs | 4 fs |
| None | No | 1 fs | Not recommended |

> With LangevinMiddleIntegrator + HBonds constraints, 4 fs is safe even without HMR. HMR with AllBonds provides additional stability at 4 fs.

---

## Production Run (Schema v3 -- Node-Based)

### Local Execution

```bash
# Resolve parm7/rst7 from topo node (walk ancestors from eq_001)
# Read nodes/topo_001/node.json -> artifacts.parm7, artifacts.rst7
# Read nodes/eq_001/node.json -> artifacts.checkpoint

mdclaw --job-dir <job_dir> --node-id prod_001 run_production \
  --prmtop-file <job_dir>/nodes/topo_001/artifacts/system.parm7 \
  --inpcrd-file <job_dir>/nodes/topo_001/artifacts/system.rst7 \
  --simulation-time-ns <user_specified> \
  --temperature-kelvin <T> \
  --pressure-bar 1.0 \
  --output-frequency-ps 10.0 \
  --restart-from <job_dir>/nodes/eq_001/artifacts/equilibrated.chk
```

The tool self-updates `nodes/prod_001/node.json` and `progress.json` automatically.

### SLURM Execution (HPC)

```bash
mdclaw submit_job \
  --script "mdclaw --job-dir <ABS_JOB_DIR> --node-id prod_001 run_production \
    --prmtop-file <ABS_PARM7> \
    --inpcrd-file <ABS_RST7> \
    --simulation-time-ns <user_specified> \
    --temperature-kelvin <T> \
    --pressure-bar 1.0 \
    --platform CUDA \
    --restart-from <ABS_JOB_DIR>/nodes/eq_001/artifacts/equilibrated.chk" \
  --job-name md_<name> \
  --partition <partition> --nodelist <node> --gpus 1 \
  --time-limit <estimated> --memory "32G"
```

SLURM compute nodes do not inherit the login node's working directory,
so all paths in `--script` need to be absolute. Use `realpath` to convert.

### Production Run (Schema v2 -- Legacy)

```bash
mdclaw run_production \
  --prmtop-file <parm7> --inpcrd-file <rst7> \
  --output-dir <run_dir> \
  --simulation-time-ns <user_specified> \
  --restart-from <run_dir>/equilibration/equilibrated.chk
```

---

## Common Run Lengths

| Purpose | Time | Notes |
|---|---|---|
| Sanity check | 0.1 ns | Quick validation |
| Short equilibration | 1-10 ns | Good for initial testing |
| Production | 50-500 ns | Standard for conformational sampling |
| Extended | 1+ us | For slow processes (folding, binding) |

---

## HPC / GPU Usage

### GPU Selection
```bash
mdclaw --job-dir <job_dir> --node-id prod_001 run_production \
  --platform CUDA --device-index "0" \
  --prmtop-file <parm7> --inpcrd-file <rst7> \
  --simulation-time-ns 100.0 \
  --restart-from <eq_checkpoint>
```

### HMR (default: enabled)

HMR (hydrogenMass=4 amu) and 4 fs timestep are the defaults. To disable:
```bash
--no-hmr --timestep-fs 2.0
```

### Checkpoint / Restart

For node-based workflow, restarts use the same `--node-id`. The tool detects
existing trajectory/checkpoint in the node's artifacts directory and appends.

```bash
# Initial run
mdclaw --job-dir <job_dir> --node-id prod_001 run_production \
  --prmtop-file <parm7> --inpcrd-file <rst7> \
  --simulation-time-ns 100.0 --platform CUDA \
  --restart-from <eq_checkpoint>

# Restart from mid-run checkpoint (same node)
mdclaw --job-dir <job_dir> --node-id prod_001 run_production \
  --prmtop-file <parm7> --inpcrd-file <rst7> \
  --simulation-time-ns 100.0 --platform CUDA \
  --restart-from <job_dir>/nodes/prod_001/artifacts/checkpoint.chk
```

**Important:**
- Use the same `--node-id` for restarts -- ensures DCD append
- `--simulation-time-ns` is the **total** target time, not additional time
- Binary checkpoint is platform-specific (CUDA checkpoint cannot load on CPU)

---

## Membrane Systems

- Uses MonteCarloMembraneBarostat (XYIsotropic + ZFree)
- Longer equilibration recommended (0.5-1 ns NVT + 1 ns NPT)
- Monitor membrane area and lipid order parameters

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| SHAKE constraint failure | Bad geometry or too large timestep | Reduce to 2 fs, or re-prepare structure |
| NaN energies | Clashes in input | Re-equilibrate or re-prepare structure |
| Slow performance | GPU not detected | Check `--platform CUDA` and `nvidia-smi` |
| Out of memory | System too large | Reduce buffer or use implicit solvent |
| Barostat instability | Temperature mismatch | Ensure barostat and integrator use same temperature |

---

## Verify Output

After production, read `nodes/prod_001/node.json`:
- `status` should be `"completed"`
- `artifacts`: trajectory, final_structure, checkpoint, energy
- `metadata`: simulation_time_ns, platform, hmr, steps
