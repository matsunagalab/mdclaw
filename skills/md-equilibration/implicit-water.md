# Equilibration: Implicit Solvent

Officially supported implicit-water models: **HCT, OBC1, OBC2, GBn, GBn2**.

Standard recipe: `build_amber_system --implicit-solvent <MODEL>` on the
topo node, then `run_equilibration --implicit-solvent <MODEL>` here.
`build_amber_system` bakes the GB force (matching `implicit/*.xml`)
into `system.xml`, stamps the canonical model name on
`metadata.implicit_solvent`, and the run side validates both halves
before any System is built:

- topology guard (resolver): `implicit_solvent_topology_mismatch` if
  `metadata.implicit_solvent` and the runtime `--implicit-solvent`
  disagree after canonicalization.
- shim contract (deserialize): `modern_system_implicit_solvent_unsupported`
  if `system.xml` carries no GB force at all.
- runtime model lookup: `implicit_solvent_model_unsupported` for
  unknown / typo'd GB names — no silent OBC2 fallback.

Research-mode shipped XML path: `build_openmm_system --forcefield-xml …
implicit/<model>.xml --implicit-solvent <MODEL>`. Same metadata
contract, but the user owns the bundle (missing or duplicate
`implicit/*.xml` returns `implicit_solvent_xml_missing` /
`implicit_solvent_xml_ambiguous`).

External GB XML (third-party, e.g. the Greener group's `GB99dms.xml`)
is an advanced escape hatch through `build_openmm_system`. mdclaw
cannot canonicalize a non-catalog GB XML, so the topo node's
`metadata.implicit_solvent` stays `None` and the run-side topology
guard cannot validate the build/runtime match — the user must manage
XML correctness, GB-force presence, and consistency between build and
run themselves.

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
