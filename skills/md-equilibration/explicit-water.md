# Equilibration: Explicit Water

## Equilibration Protocol

Standalone restrained minimization in a `min` node, followed by low-temperature
NVT warmup, normal-temperature NVT heating, and NPT density equilibration in an
`eq` node, with CA positional restraints. The same `min -> eq` prelude is used
for implicit and explicit systems. Both equilibration stages use 4 fs + HMR so
the final checkpoint is compatible with production settings.

### Run Equilibration

Before local execution of an explicit-water system, verify the post-solvation
atom count and available OpenMM platforms:

```bash
mdclaw inspect_openmm_platforms \
  --atom-count <solv.statistics.total_atoms> \
  --solvent-type explicit
```

If no CUDA/OpenCL platform is available and the system is classified
`not_recommended` or `slow_on_cpu`, do not start the default local
equilibration automatically. Use `/hpc-run`, or deliberately choose a shorter
smoke-test protocol such as `--nvt-time-ns 0.01 --npt-time-ns 0.01`.

Platform policy: do not pass `--platform CPU` unless the user explicitly asks
for CPU-only debugging. Prefer the tool default `--platform auto`; if an
explicit platform is needed, choose `CUDA` when available, otherwise `OpenCL`.

```bash
mdclaw --job-dir <job_dir> --node-id <min_node_id> run_minimization \
  --max-iterations 5000 \
  --restraint-atoms CA \
  --restraint-force-constant 100.0

mdclaw --job-dir <job_dir> --node-id <eq_node_id> run_equilibration \
  --temperature-kelvin <T> --pressure-bar 1.0 \
  --nvt-time-ns <NVT_NS> --npt-time-ns <NPT_NS>
```

`run_minimization` auto-resolves `system_xml_file`, `topology_pdb_file`, and
`state_xml_file` from the `topo` ancestor. `run_equilibration` auto-resolves
the same topology bundle plus the parent `min` node's portable `state`.
To override, pass `--system-xml-file` / `--topology-pdb-file` / `--state-xml-file` explicitly.

The tool self-updates `node.json` and `progress.json` on success or failure.

### Domain Knowledge

- New DAGs use `topo -> min -> eq`. `run_minimization` writes
  `minimized_structure.pdb`, `minimized.xml`, and `minimization_report.json`.
  `run_equilibration` starts from the `min` node's `state`, skips coordinate
  minimization, then runs low-temperature warmup before normal NVT/NPT.
- Equilibration uses positional restraints to prevent structural collapse.
  `--restraint-atoms` accepts:
  - `CA` (default): alpha carbons only — recommended for most workflows
  - `backbone`: protein backbone heavy atoms (N, CA, C, O)
  - `heavy`: all non-hydrogen solute atoms — strongest, useful for early-stage relaxation
  All three options automatically exclude water and ions (solute only),
  so OPC virtual sites and counterions are never restrained.
- NVT default length: 1 ns. Prefer `--nvt-time-ns <ns>` for user-facing
  duration requests.
- NPT default length: 1 ns. Prefer `--npt-time-ns <ns>` for user-facing
  duration requests.
- Do not convert ns/ps to steps in the agent. The tool converts time to
  steps using the active `timestep_fs` (default 4 fs with HMR).
- Reference only: 0.1 ns at 4 fs = 25,000 steps; 0.1 ns at 2 fs =
  50,000 steps. Use the time flags anyway.
- Low-level override: pass `--nvt-steps <N>` / `--npt-steps <N>` only when
  the user explicitly asks for step counts. Do not pass a time flag and a
  steps flag for the same stage.
- Both stages use HMR (hydrogenMass=4 amu), matching production's integrator
- `equilibrated.xml` is the portable cross-node restart artifact (preferred);
  `equilibrated.chk` is a binary checkpoint kept for same-GPU bit-exact replay.
  Both record `currentStep=0` so `run_production --simulation-time-ns` is the
  full production length.
- For finer control (e.g. NPT compress with `heavy` → NVT thermalize with `CA`
  → NPT relax with no restraints), chain multiple eq nodes — see the
  "Multi-Stage Chaining" section in `skills/md-equilibration/SKILL.md`.
- Energy should drop during the `min` node minimization (good sign)

---

## Verify Output

Read `nodes/<eq_node_id>/node.json`:
- upstream `nodes/<min_node_id>/node.json` should be `"completed"` with
  `artifacts.state`, `artifacts.minimized_structure`, and
  `artifacts.minimization_report`
- `status` should be `"completed"`
- `artifacts.checkpoint` -- path to equilibrated.chk (for production restart)
- `metadata` -- platform, nvt_steps, npt_steps, restraint info
