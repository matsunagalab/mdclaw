# Equilibration: Explicit Water

## Equilibration Protocol

NVT heating followed by NPT density equilibration, both with CA positional
restraints. Both stages use 4 fs + HMR so the final checkpoint is compatible
with production settings.

### Run Equilibration (Schema v3 — Node-Based)

```bash
# Resolve parm7/rst7 from topo node
# Read nodes/topo_001/node.json -> artifacts.parm7, artifacts.rst7

mdclaw --job-dir <job_dir> --node-id eq_001 run_equilibration \
  --prmtop-file <job_dir>/nodes/topo_001/artifacts/system.parm7 \
  --inpcrd-file <job_dir>/nodes/topo_001/artifacts/system.rst7 \
  --temperature-kelvin <T> \
  --pressure-bar 1.0
```

The tool self-updates `nodes/eq_001/node.json` and `progress.json` automatically:
- On success: status -> `completed`, artifacts (checkpoint, final_structure), metadata
- On failure: status -> `failed`, errors recorded

No manual `run.json` or `progress.json` updates needed.

### Run Equilibration (Schema v2 — Legacy)

```bash
mdclaw run_equilibration \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7> \
  --output-dir <run_dir> \
  --temperature-kelvin <T> \
  --pressure-bar 1.0
```

### Domain Knowledge

- Equilibration uses positional restraints on CA atoms to prevent structural collapse
- NVT stage: 2500 steps at 4 fs (10 ps) -- heats from 0 to target temperature
- NPT stage: 5000 steps at 4 fs (20 ps) -- equilibrates density at target pressure
- Both stages use HMR (hydrogenMass=4 amu), matching production's integrator
- `equilibrated.chk` is a binary checkpoint with currentStep=0 by construction,
  so `run_production --simulation-time-ns` is the full production length
- Energy should drop significantly during minimization (good sign)

---

## Verify Output

After equilibration, read `nodes/eq_001/node.json`:
- `status` should be `"completed"`
- `artifacts.checkpoint` — path to equilibrated.chk (for production restart)
- `metadata` — platform, nvt_steps, npt_steps, restraint info
