# Equilibration: Implicit Solvent

## Equilibration Protocol

NVT only (no NPT — no periodic box in implicit solvent) with CA positional
restraints. Uses 4 fs + HMR so the final checkpoint is compatible with
production settings.

### Run Equilibration

```bash
mdclaw --job-dir <job_dir> --node-id eq_001 run_equilibration \
  --temperature-kelvin <T>
```

`prmtop_file` and `inpcrd_file` are auto-resolved from the `topo` ancestor.
No `--pressure-bar` is needed — `run_equilibration` auto-skips NPT when the
topology has no periodic box (implicit solvent) or when `implicit_solvent`
is set. To override, pass `--prmtop-file` / `--inpcrd-file` explicitly.

The tool self-updates `node.json` and `progress.json` on success or failure.

### Domain Knowledge

- NVT only: implicit solvent has no periodic box, so no barostat
- NVT default length: 250000 steps at 4 fs (1 ns). Override with
  `--nvt-steps <N>` (e.g. `--nvt-steps 2500` for a 10 ps sanity run).
- CA positional restraints prevent structural collapse during heating
- CA restraints are removed in the production-matching checkpoint
- Energy minimization runs automatically before NVT heating
- `equilibrated.chk` is written with `currentStep=0` by design, so
  `run_production --simulation-time-ns` is the full production length
- The checkpoint is directly usable by `run_production --restart-from`, or
  auto-resolved via the DAG when prod has eq as parent

---

## Verify Output

Read `nodes/eq_001/node.json`:

- `status` should be `"completed"`
- `artifacts.checkpoint` — path to `equilibrated.chk` (for production restart)
- `metadata` — platform, nvt_steps, restraint info (no npt for implicit)
