# Equilibration: Implicit Solvent

Read `skills/common/implicit-solvent-contract.md` first for the supported models
(HCT, OBC1, OBC2, GBn, GBn2; GBn2 recommended) and the build/run validation
contract. `min` and `eq` inherit the model baked into the topology.

## Equilibration Protocol

NVT only (no NPT — no periodic box in implicit solvent) with the standard
`min -> eq` prelude used for every system: standalone restrained minimization
in a `min` node, low-temperature NVT warmup, then normal-temperature NVT with
solute-heavy positional restraints. Uses 4 fs + HMR so the final checkpoint is compatible
with production settings.

### Run Equilibration

```bash
mdclaw --job-dir <job_dir> --node-id <min_node_id> run_minimization \
  --max-iterations 5000 \
  --restraint-atoms solute_heavy \
  --restraint-force-constant 100.0

mdclaw --job-dir <job_dir> --node-id <eq_node_id> run_equilibration \
  --temperature-kelvin <T> \
  --pressure-bar 0 \
  --nvt-time-ns <NVT_NS>
```

`run_minimization` auto-resolves `system_xml_file`, `topology_pdb_file`, and
`state_xml_file` from the `topo` ancestor. `run_equilibration` auto-resolves
the same topology bundle plus the parent `min` node's portable `state`.
The GB model and HMR setting inherit from the topology. Pass `--pressure-bar 0`
to make the declared node conditions and restart signature explicit; implicit
solvent has no barostat and always equilibrates as NVT. To override inputs,
pass `--system-xml-file` / `--topology-pdb-file` / `--state-xml-file` explicitly.

The tool self-updates `node.json` and `progress.json` on success or failure.

### Domain Knowledge

- NVT only: implicit solvent has no periodic box, so no barostat
- The topology build selects the GB model; min/eq inherit it.
- NVT default length: 1 ns. If the user gives an equilibration duration,
  pass it as `--nvt-time-ns <ns>` and keep `--pressure-bar 0`.
- Do not convert ns/ps to steps in the agent. The tool converts time to
  steps using the active `timestep_fs` (default 4 fs with HMR).
- Low-level override: use `--nvt-steps <N>` only when the user explicitly
  asks for step counts. Do not pass both `--nvt-time-ns` and `--nvt-steps`.
- Do not request a positive `--npt-time-ns` for implicit solvent; NPT is not
  applicable when `--pressure-bar 0`.
- Positional restraints prevent structural collapse during heating.
  `--restraint-atoms` options:

  | Value | Restrains | Notes |
  |---|---|---|
  | `solute_heavy` (default) | prep-derived solute heavy atoms | includes structural ions; excludes solvent and added ions |
  | `CA` | alpha carbons | protein-only legacy selection |
  | `backbone` | protein backbone heavy atoms (N, CA, C, O) | |
  | `heavy` | all non-hydrogen solute atoms | strongest restraint |
- All restraints are removed in the production-matching checkpoint
- New DAGs use `topo -> min -> eq`. `run_minimization` writes
  `minimized_structure.pdb`, `minimized.xml`, and `minimization_report.json`.
  `run_equilibration` starts from the `min` node's `state`, skips coordinate
  minimization, then runs low-temperature warmup before normal NVT heating.
- Ligand charge/clash diagnostics are recorded for interpretation; they do not
  switch to a different equilibration protocol.
- `equilibrated.xml` is the portable cross-node restart artifact (preferred);
  `equilibrated.chk` is the binary checkpoint kept for same-GPU bit-exact replay.
  Both are written with `currentStep=0` so `run_production --simulation-time-ns`
  is the full production length.
- The state is auto-resolved via the DAG when prod has eq as parent;
  `--restart-from` can also be passed explicitly. Multi-stage eq → eq chains
  also work (see `skills/md-equilibration/multi-stage-eq.md`).

---

## Verify Output

Read `nodes/<eq_node_id>/node.json`:

- upstream `nodes/<min_node_id>/node.json` should be `"completed"` with
  `artifacts.state`, `artifacts.minimized_structure`, and
  `artifacts.minimization_report`
- `status` should be `"completed"`
- `artifacts.checkpoint` — path to `equilibrated.chk` (for production restart)
- `metadata` — platform, nvt_steps, restraint info (no npt for implicit)
