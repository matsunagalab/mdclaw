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

`system_xml_file`, `inpcrd_file`, `restart_from`, and `pressure_bar` are
auto-resolved from DAG ancestors. Ensemble is inherited from the `eq`
ancestor, so NPT eq states load with a matching barostat by default. For
extension/retry details, read `skills/md-production/restart.md`.

### SLURM Execution (HPC)

For long runs, multi-replicate sweeps, or fan-out across many systems, hand off
to `/hpc-run`. Do not duplicate sbatch patterns here; use the focused runbooks:

- `skills/hpc-run/submit-single.md`
- `skills/hpc-run/submit-array.md`
- `skills/hpc-run/prod-extension.md`
- `skills/hpc-run/monitor-recover.md`

Inside the job script, omit `--system-xml-file`, `--topology-pdb-file`, `--state-xml-file`, and
`--restart-from` in normal DAG flows. DAG auto-resolution handles them.

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
| `Ensemble switch:` warning in `result["warnings"]` | NPT-saved eq state used in an NVT prod context, or vice versa | Safe to ignore — the loader transfers only positions/velocities/box, so barostat parameters are dropped (NPT → NVT) or the new barostat starts in its default state and re-equilibrates volume over the first few ps (NVT → NPT). Set `--pressure-bar 0` (NVT) or `--pressure-bar 1.0` (NPT) on prod; no eq re-run needed. |

---

## Verify Output

Read `nodes/prod_001/node.json`:
- `status`: `"completed"`
- `artifacts`: trajectory, final_structure, checkpoint, energy
- `metadata`: simulation_time_ns, platform, hmr, steps
