# Equilibration: Explicit Water

## Equilibration Protocol

NVT heating followed by NPT density equilibration, both with CA positional
restraints. Both stages use 4 fs + HMR so the final checkpoint is compatible
with production settings.

### Run Equilibration

```bash
mdclaw --job-dir <job_dir> --node-id eq_001 run_equilibration \
  --temperature-kelvin <T> --pressure-bar 1.0
```

`prmtop_file` and `inpcrd_file` are auto-resolved from the `topo` ancestor.
To override, pass `--prmtop-file` / `--inpcrd-file` explicitly.

The tool self-updates `node.json` and `progress.json` on success or failure.

### Domain Knowledge

- Equilibration uses positional restraints on CA atoms to prevent structural collapse
- NVT stage: 250000 steps at 4 fs (1 ns, default) -- heats from 0 to target temperature
- NPT stage: 250000 steps at 4 fs (1 ns, default) -- equilibrates density at target pressure
- Override: pass `--nvt-steps <N>` / `--npt-steps <N>` to shorten or
  lengthen either stage. Common choices: 2500 (10 ps) for fast sanity
  runs, 125000 (500 ps) for compromise, 500000+ (2 ns+) for difficult
  systems needing more relaxation.
- Both stages use HMR (hydrogenMass=4 amu), matching production's integrator
- `equilibrated.chk` is a binary checkpoint with currentStep=0 by construction,
  so `run_production --simulation-time-ns` is the full production length
- Energy should drop significantly during minimization (good sign)

---

## Verify Output

Read `nodes/eq_001/node.json`:
- `status` should be `"completed"`
- `artifacts.checkpoint` -- path to equilibrated.chk (for production restart)
- `metadata` -- platform, nvt_steps, npt_steps, restraint info
