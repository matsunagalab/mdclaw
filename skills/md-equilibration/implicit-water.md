# Equilibration: Implicit Solvent

## Equilibration Protocol

NVT only (no NPT — no periodic box in implicit solvent) with the standard
equilibration prelude used for every system: restrained staged minimization,
low-temperature NVT warmup, then normal-temperature NVT with CA positional
restraints. Uses 4 fs + HMR so the final checkpoint is compatible with
production settings.

### Run Equilibration

```bash
mdclaw --job-dir <job_dir> --node-id eq_001 run_equilibration \
  --temperature-kelvin <T> \
  --pressure-bar 0 \
  --implicit-solvent GBn2
```

`system_xml_file`, `topology_pdb_file`, and `state_xml_file` are auto-resolved from the `topo` ancestor.
Always pass `--implicit-solvent <model>` so OpenMM builds a GB system rather
than rejecting the non-periodic topology as vacuum. Pass `--pressure-bar 0`
to make the declared node conditions and restart signature explicit; implicit
solvent has no barostat and always equilibrates as NVT. To override inputs,
pass `--system-xml-file` / `--topology-pdb-file` / `--state-xml-file` explicitly.

The tool self-updates `node.json` and `progress.json` on success or failure.

### Domain Knowledge

- NVT only: implicit solvent has no periodic box, so no barostat
- `--implicit-solvent` is required for GB simulations; omitting it is vacuum,
  not implicit solvent
- NVT default length: 250000 steps at 4 fs (1 ns). Override with
  `--nvt-steps <N>` (e.g. `--nvt-steps 2500` for a 10 ps sanity run).
- Positional restraints prevent structural collapse during heating.
  `--restraint-atoms` accepts:
  - `CA` (default): alpha carbons only
  - `backbone`: protein backbone heavy atoms (N, CA, C, O)
  - `heavy`: all non-hydrogen solute atoms — strongest restraint
  Solute filtering is automatic (water/ions are excluded even under `heavy`,
  though implicit solvent has no explicit waters anyway).
- All restraints are removed in the production-matching checkpoint
- Standard staged minimization and low-temperature warmup run automatically
  before normal NVT heating. This is the same protocol used for explicit water.
- Ligand charge/clash diagnostics are recorded for interpretation; they do not
  switch to a different equilibration protocol.
- `equilibrated.xml` is the portable cross-node restart artifact (preferred);
  `equilibrated.chk` is the binary checkpoint kept for same-GPU bit-exact replay.
  Both are written with `currentStep=0` so `run_production --simulation-time-ns`
  is the full production length.
- The state is auto-resolved via the DAG when prod has eq as parent;
  `--restart-from` can also be passed explicitly. Multi-stage eq → eq chains
  also work (see `skills/md-equilibration/SKILL.md` "Multi-Stage Chaining").

---

## Verify Output

Read `nodes/eq_001/node.json`:

- `status` should be `"completed"`
- `artifacts.checkpoint` — path to `equilibrated.chk` (for production restart)
- `metadata` — platform, nvt_steps, restraint info (no npt for implicit)
