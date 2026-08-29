# Equilibration: Run min + eq

Standalone restrained minimization in a `min` node, followed by low-temperature
NVT warmup, normal-temperature NVT heating, and (explicit water) NPT density
equilibration in an `eq` node, with solute-heavy positional restraints. The
same `min -> eq` prelude is used for explicit and implicit systems. By default,
k=100 restraints remain active through both NVT and NPT; both stages use 4 fs +
HMR so the final checkpoint is compatible with production settings.

## Run Equilibration

For a local explicit-water run, first run the local-execution / platform
preflight from `skills/common/solvent-regimes.md` (a `slow_on_cpu` /
`not_recommended` system should go to `/hpc-run` or a short smoke test such as
`--nvt-time-ns 0.01 --npt-time-ns 0.01`).

```bash
mdclaw --job-dir <job_dir> --node-id <min_node_id> run_minimization \
  --max-iterations 5000 \
  --restraint-atoms solute_heavy \
  --restraint-force-constant 100.0

mdclaw --job-dir <job_dir> --node-id <eq_node_id> run_equilibration \
  --temperature-kelvin <T> --pressure-bar 1.0 \
  --nvt-time-ns <NVT_NS> --npt-time-ns <NPT_NS>
```

`run_minimization` auto-resolves `system_xml_file`, `topology_pdb_file`, and
`state_xml_file` from the `topo` ancestor. `run_equilibration` auto-resolves
the same topology bundle plus the parent `min` node's portable `state`.
To override, pass `--system-xml-file` / `--topology-pdb-file` /
`--state-xml-file` explicitly. The tool self-updates `node.json` and
`progress.json` on success or failure.

### Implicit-Solvent Delta

Read `skills/common/implicit-solvent-contract.md` for the supported models;
`min` and `eq` inherit the GB model and HMR setting baked into the topology.
Implicit solvent has no periodic box, so equilibration is NVT only: pass
`--pressure-bar 0` (making the declared conditions and restart signature
explicit), do not request a positive `--npt-time-ns`, and keep everything else
identical to the explicit commands above.

### Domain Knowledge

- New DAGs use `topo -> min -> eq`. `run_minimization` writes
  `minimized_structure.pdb`, `minimized.xml`, and `minimization_report.json`.
  `run_equilibration` starts from the `min` node's `state`, skips coordinate
  minimization, then runs low-temperature warmup before normal NVT/NPT.
- Equilibration uses positional restraints to prevent structural collapse.
  `--restraint-atoms` options:

  | Value | Restrains | Notes |
  |---|---|---|
  | `solute_heavy` (default) | prep-derived solute heavy atoms | includes structural ions; excludes water, added ions, lipids, and virtual sites |
  | `CA` | alpha carbons | protein-only legacy selection |
  | `backbone` | protein backbone heavy atoms (N, CA, C, O) | |
  | `heavy` | all non-hydrogen solute atoms | strongest; useful for early-stage relaxation |
- The restrained NVT/NPT end state is transferred to a restraint-free
  production checkpoint; no unrestrained equilibration stage runs.
- NVT and NPT default lengths: 1 ns each. Prefer `--nvt-time-ns` /
  `--npt-time-ns` for user-facing duration requests.
- Do not convert ns/ps to steps in the agent. The tool converts time to
  steps using the active `timestep_fs` (default 4 fs with HMR).
- Low-level override: pass `--nvt-steps <N>` / `--npt-steps <N>` only when
  the user explicitly asks for step counts. Do not pass a time flag and a
  steps flag for the same stage.
- Ligand charge/clash diagnostics are recorded for interpretation; they do not
  switch to a different equilibration protocol.
- `equilibrated.xml` is the portable cross-node restart artifact (preferred);
  `equilibrated.chk` is a binary checkpoint kept for same-GPU bit-exact replay.
  Both record `currentStep=0` so `run_production --simulation-time-ns` is the
  full production length. Production auto-resolves the state via the DAG.
- Energy should drop during the `min` node minimization (good sign).
- Use the optional `multi-stage-eq.md` chain only for an explicit final
  unrestrained NPT request; do not add that stage to the default protocol.

---

## Verify Output

Read `nodes/<eq_node_id>/node.json`:

- upstream `nodes/<min_node_id>/node.json` should be `"completed"` with
  `artifacts.state`, `artifacts.minimized_structure`, and
  `artifacts.minimization_report`
- `status` should be `"completed"`
- `artifacts.checkpoint` — path to `equilibrated.chk` (for production restart)
- `metadata` — platform, nvt_steps, npt_steps (explicit only), restraint info
