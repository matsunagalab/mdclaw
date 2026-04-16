# Equilibration: Implicit Solvent

## Equilibration Protocol

NVT only (no NPT — no periodic box in implicit solvent) with CA positional
restraints. Uses 4 fs + HMR so the final checkpoint is compatible with
production settings.

### Run Equilibration

```bash
mdclaw run_equilibration \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7> \
  --output-dir <run_dir>/equilibration \
  --temperature-kelvin <T>
```

- No `--pressure-bar` needed — `run_equilibration` auto-skips NPT for implicit solvent
- CA positional restraints prevent structural collapse during heating
- Writes `equilibrated.chk` (production-matching System, currentStep=0)

### Domain Knowledge

- NVT only: implicit solvent has no periodic box, so no barostat
- CA restraints are removed in the production-matching checkpoint
- Energy minimization runs automatically before NVT heating
- `equilibrated.chk` is directly usable by `run_production --restart-from`

---

## Update run.json

After equilibration completes, update `run.json` with metadata from the tool output:

- **stages.equilibration**:
  - `status`: `"completed"`
  - `checkpoint`: path to `equilibrated.chk`
  - `state_file`: path to `equilibration.xml`
  - `final_structure`: path to `equilibrated.pdb`
  - `platform`: from tool output
  - `nvt_steps`, `restraint_atoms`, `restraint_count`
  - `stages_completed`: from tool output (e.g., `["NVT"]`)
