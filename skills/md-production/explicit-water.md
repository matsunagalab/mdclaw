# Production MD: Explicit Water

Force field / water / integrator / HMR / PME constant defaults and the
local-run platform preflight live in `skills/common/solvent-regimes.md`.
Production runs NPT (300 K, 1 bar) with a `MonteCarloBarostat` whose temperature
must match the integrator. HMR is baked into `system.xml` at build time; a
run-side mismatch raises `modern_system_hmr_mismatch` (use `--no-hmr
--timestep-fs 2.0` only when the system was built without HMR).

---

## Production Run

### Local Execution

Run the local-execution / platform preflight from
`skills/common/solvent-regimes.md` first; a `slow_on_cpu` / `not_recommended`
system should go to `/hpc-run` or an explicit short smoke test.

```bash
mdclaw --job-dir <job_dir> --node-id <prod_node_id> run_production \
  --simulation-time-ns <ns> \
  --temperature-kelvin <T> \
  --output-frequency-ps 10.0
```

Choose the run length with the Default Decision Rule in `SKILL.md`.
`system_xml_file`, `topology_pdb_file`, `state_xml_file`, `restart_from`, and
`pressure_bar` are auto-resolved from DAG ancestors. Ensemble is inherited from
the `eq` ancestor, so NPT eq states load with a matching barostat by default.
For extension (`--continue-from`) and retry details, read
`skills/md-production/restart.md`.

### SLURM Execution (HPC)

For long runs, multi-replicate sweeps, or fan-out across many systems, hand off
to HPC execution instead of duplicating sbatch patterns here: follow
`skills/hpc-run/SKILL.md` and its submit/monitor/extension pages.

---

## GPU / HMR

```bash
# Usually omit --platform and let MDClaw/OpenMM choose the fastest available.

# Explicit GPU selection, only when needed
--platform CUDA --device-index "0"
--platform OpenCL --device-index "0"

# Disable HMR (not recommended)
--no-hmr --timestep-fs 2.0
```

To bias production with a custom force / CV, read and follow
`skills/md-production/custom-force.md`.

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
| Slow performance | GPU not detected, or explicit-water PME running on CPU | Check `inspect_openmm_platforms`; omit `--platform`, use `--platform CUDA` / `--platform OpenCL` when available, or hand off to `/hpc-run` |
| Barostat instability | Temperature mismatch | Match barostat and integrator T |
| `Ensemble switch:` warning in `result["warnings"]` | NPT-saved eq state used in an NVT prod context, or vice versa | Safe to ignore — the loader transfers only positions/velocities/box, so barostat parameters are dropped (NPT → NVT) or the new barostat starts in its default state and re-equilibrates volume over the first few ps (NVT → NPT). Set `--pressure-bar 0` (NVT) or `--pressure-bar 1.0` (NPT) on prod; no eq re-run needed. |

---

## Verify Output

Read `nodes/<prod_node_id>/node.json`:
- `status`: `"completed"`
- `artifacts`: trajectory, final_structure, checkpoint, energy
- `metadata`: simulation_time_ns, platform, hmr, steps
