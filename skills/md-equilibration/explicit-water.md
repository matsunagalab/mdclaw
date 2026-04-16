# Equilibration: Explicit Water

## Equilibration Protocol

NVT heating followed by NPT density equilibration, both with CA positional
restraints. Both stages use 4 fs + HMR so the final checkpoint is compatible
with production settings.

### Run Equilibration

```bash
mdclaw run_equilibration \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7> \
  --output-dir <run_dir> \
  --temperature-kelvin <T> \
  --pressure-bar 1.0
```

- `--pressure-bar 1.0` triggers the NPT density stage after NVT heating
- CA positional restraints (default 100 kJ/mol/nm^2) prevent structural collapse
- Writes `equilibrated.chk` from a production-matching System (no restraints,
  currentStep=0) — ready for direct handoff to `run_production`
- Also writes `equilibration.xml` as an audit/reproducibility backup

### Domain Knowledge

- Equilibration uses positional restraints on CA atoms to prevent structural collapse
- NVT stage: 2500 steps at 4 fs (10 ps) — heats from 0 to target temperature
- NPT stage: 5000 steps at 4 fs (20 ps) — equilibrates density at target pressure
- Both stages use HMR (hydrogenMass=4 amu), matching production's integrator
- `equilibrated.chk` is a binary checkpoint with currentStep=0 by construction,
  so `run_production --simulation-time-ns` is the full production length
- `equilibration.xml` is for audit only — use `.chk` for restart
- Energy should drop significantly during minimization (good sign)

---

## Update run.json

After equilibration completes, update `run.json` with metadata from the tool output:

- **stages.equilibration**:
  - `status`: `"completed"`
  - `checkpoint`: path to `equilibrated.chk`
  - `state_file`: path to `equilibration.xml`
  - `final_structure`: path to `equilibrated.pdb`
  - `platform`: from tool output (e.g., `"CUDA"`, `"OpenCL"`)
  - `nvt_steps`, `npt_steps`, `restraint_atoms`, `restraint_count`
  - `stages_completed`: from tool output (e.g., `["NVT", "NPT"]`)
