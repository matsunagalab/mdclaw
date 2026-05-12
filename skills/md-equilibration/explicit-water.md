# Equilibration: Explicit Water

## Equilibration Protocol

Restrained staged minimization and low-temperature NVT warmup, followed by
normal-temperature NVT heating and NPT density equilibration, with CA positional
restraints. The same staged minimization + warmup prelude is used for implicit
and explicit systems. Both stages use 4 fs + HMR so the final checkpoint is
compatible with production settings.

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
smoke-test protocol such as `--nvt-steps 2500 --npt-steps 2500`.

Platform policy: do not pass `--platform CPU` unless the user explicitly asks
for CPU-only debugging. Prefer the tool default `--platform auto`; if an
explicit platform is needed, choose `CUDA` when available, otherwise `OpenCL`.

```bash
mdclaw --job-dir <job_dir> --node-id eq_001 run_equilibration \
  --temperature-kelvin <T> --pressure-bar 1.0
```

`system_xml_file`, `topology_pdb_file`, and `state_xml_file` are auto-resolved from the `topo` ancestor.
To override, pass `--system-xml-file` / `--topology-pdb-file` / `--state-xml-file` explicitly.

The tool self-updates `node.json` and `progress.json` on success or failure.

### Domain Knowledge

- Equilibration starts with standard staged minimization and low-temperature
  warmup for all systems, then proceeds to normal NVT/NPT
- Equilibration uses positional restraints to prevent structural collapse.
  `--restraint-atoms` accepts:
  - `CA` (default): alpha carbons only — recommended for most workflows
  - `backbone`: protein backbone heavy atoms (N, CA, C, O)
  - `heavy`: all non-hydrogen solute atoms — strongest, useful for early-stage relaxation
  All three options automatically exclude water and ions (solute only),
  so OPC virtual sites and counterions are never restrained.
- NVT stage: 250000 steps at 4 fs (1 ns, default) -- heats from 0 to target temperature
- NPT stage: 250000 steps at 4 fs (1 ns, default) -- equilibrates density at target pressure
- Override: pass `--nvt-steps <N>` / `--npt-steps <N>` to shorten or
  lengthen either stage. Common choices: 2500 (10 ps) for fast sanity
  runs, 125000 (500 ps) for compromise, 500000+ (2 ns+) for difficult
  systems needing more relaxation.
- Both stages use HMR (hydrogenMass=4 amu), matching production's integrator
- `equilibrated.xml` is the portable cross-node restart artifact (preferred);
  `equilibrated.chk` is a binary checkpoint kept for same-GPU bit-exact replay.
  Both record `currentStep=0` so `run_production --simulation-time-ns` is the
  full production length.
- For finer control (e.g. NPT compress with `heavy` → NVT thermalize with `CA`
  → NPT relax with no restraints), chain multiple eq nodes — see the
  "Multi-Stage Chaining" section in `skills/md-equilibration/SKILL.md`.
- Energy should drop significantly during minimization (good sign)

---

## Verify Output

Read `nodes/eq_001/node.json`:
- `status` should be `"completed"`
- `artifacts.checkpoint` -- path to equilibrated.chk (for production restart)
- `metadata` -- platform, nvt_steps, npt_steps, restraint info
