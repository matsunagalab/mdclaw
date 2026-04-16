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

## Production Run

### Local Execution

```bash
mdclaw run_production \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7> \
  --output-dir <run_dir>/md_simulation \
  --simulation-time-ns <user_specified> \
  --temperature-kelvin <T> \
  --pressure-bar 1.0 \
  --output-frequency-ps 10.0 \
  --restart-from <run_dir>/equilibration/equilibrated.chk
```

- `--restart-from equilibrated.chk` loads equilibrated positions, velocities,
  and NPT-adjusted box. currentStep is 0 so the full `simulation_time_ns` runs.
- Minimization and velocity re-randomization are skipped when restarting.
- Use the `checkpoint_file` path from `run_equilibration`'s JSON output (or
  read from `run.json`'s `stages.equilibration.checkpoint`).

### SLURM Execution (HPC)

```bash
mdclaw submit_job \
  --script "mdclaw run_production \
    --prmtop-file <ABSOLUTE_PARM7> \
    --inpcrd-file <ABSOLUTE_RST7> \
    --simulation-time-ns <user_specified> \
    --temperature-kelvin <T> \
    --pressure-bar 1.0 \
    --platform CUDA \
    --output-dir <ABSOLUTE_RUN_DIR>/md_simulation \
    --restart-from <ABSOLUTE_RUN_DIR>/equilibration/equilibrated.chk" \
  --job-name md_<name> \
  --partition <partition> --nodelist <node> --gpus 1 \
  --time-limit <estimated> --memory "32G"
```

SLURM compute nodes do not inherit the login node's working directory,
so all paths in `--script` need to be absolute. Use `realpath` to convert.

For long runs on HPC, use `/hpc-run` skill to submit `run_production` as a
SLURM job. `run_production` is the primary mdclaw tool that benefits from
SLURM submission (GPU-bound, long-running).

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
mdclaw run_production --platform CUDA --device-index "0" \
  --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --simulation-time-ns 100.0 \
  --restart-from equilibrated.chk
```

### HMR (default: enabled)

HMR (hydrogenMass=4 amu) and 4 fs timestep are the defaults. To disable:
```bash
mdclaw run_production --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --no-hmr --timestep-fs 2.0 --simulation-time-ns 100.0 \
  --restart-from equilibrated.chk
```

### Checkpoint / Restart
```bash
# Initial run (checkpoint.chk saved automatically)
mdclaw run_production --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --simulation-time-ns 100.0 --platform CUDA \
  --restart-from equilibrated.chk

# Restart from mid-run checkpoint (appends to DCD, runs only remaining steps)
mdclaw run_production --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --simulation-time-ns 100.0 --platform CUDA \
  --restart-from /path/to/runs/<run_id>/md_simulation/checkpoint.chk
```

**Checkpoint notes:**
- Binary format: platform-specific (CUDA checkpoint cannot load on CPU)
- Restarted simulations append to the existing DCD
- For portable saves, use State (XML) — but mdclaw currently uses checkpoint

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

## Update run.json

After production completes, update `run.json` with metadata from the tool output:

- **stages.production**:
  - `status`: `"completed"`
  - `trajectory`: path to `trajectory.dcd`
  - `final_structure`: path to `final_structure.pdb`
  - `checkpoint_file`: path to `checkpoint.chk`
  - `energy_file`: path to `energy.dat`
  - `ensemble`, `simulation_time_ns`, `num_steps`, `timestep_fs`
  - `hmr`, `platform`, `device_index`
  - `initial_energy_kj_mol`, `final_energy_kj_mol`
  - `restarted_from`: path to the equilibrated checkpoint used

- `stages.production.platform`: from tool output (e.g., `"CUDA"`, `"OpenCL"`)
- `stages.production.device_index`: from tool output if specified
