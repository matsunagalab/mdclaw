# Production MD: Explicit Water

## System Configuration

| Parameter | Value | Notes |
|---|---|---|
| Electrostatics | **PME** (periodic) | Cutoff 1.0 nm |
| Force field | ff19SB | `amber19-all.xml` |
| Water model | OPC (default) | Also: TIP3P-FB, SPC/E, TIP4P-Ew |
| Integrator | LangevinMiddleIntegrator | Friction 1/ps |
| Barostat | MonteCarloBarostat | Temperature must match integrator |
| Constraints | HBonds | Allows 4 fs timestep |
| Ensemble | NPT (300K, 1 bar) | |

### Timestep Guide

| Constraints | HMR | Max Timestep | Recommended |
|---|---|---|---|
| HBonds | No | 4 fs | 2 fs (conservative) or 4 fs |
| AllBonds | Yes (`hydrogenMass=4*amu`) | 4 fs | 4 fs |

---

## Production Run

### Local Execution

```bash
mdclaw --job-dir <job_dir> --node-id prod_001 run_production \
  --simulation-time-ns <user_specified> \
  --temperature-kelvin <T> \
  --output-frequency-ps 10.0
```

If the user does not specify a run length and `execution_mode=autonomous`,
use `--simulation-time-ns 0.1` as the default sanity check.

`prmtop_file`, `inpcrd_file`, `restart_from`, **and `pressure_bar`** are all
auto-resolved from DAG ancestors. Topology comes from the `topo` ancestor.
Ensemble is inherited from the `eq` ancestor — when the eq node's
`metadata.final_ensemble == "NPT"`, prod automatically adds a matching
`MonteCarloBarostat` at the eq's `pressure_bar` so the saved `state.xml`
loads cleanly. Pass `--pressure-bar` only to override the inherited value
(e.g. running NVT prod from an NPT-equilibrated state requires explicitly
producing a fresh NVT eq state — see Troubleshooting below).

The restart rule depends on how the prod node was created. Resolution
prefers the `state.xml` artifact (saveState, cross-node portable) and
falls back to `checkpoint.chk` (saveCheckpoint, GPU-architecture-
specific) only when state.xml is missing — typically a legacy DAG that
predates the saveState migration.

- **`--continue-from prod_N`** → restart from **exactly** `prod_N`'s
  saved state (`.xml` preferred, `.chk` legacy fallback). No fallback
  to a different ancestor; missing both artifacts is a hard error.
- **plain `--parent-node-ids prod_N`** → BFS picks the nearest prod
  ancestor with a state file, falling through to the `eq` ancestor.
- **fresh run (eq parent)** → restart from the `eq` ancestor's
  `state.xml` (or its `checkpoint.chk` for legacy DAGs).

To override any of this, pass the flags explicitly.

### SLURM Execution (HPC)

For long runs, multi-replicate sweeps, or fan-out across many systems,
hand off to the `/hpc-run` skill instead of writing the sbatch here. The
short version:

```bash
# Single node, linked to the DAG (note --job-dir / --node-id on submit_job
# — they stamp slurm_job_id onto nodes/prod_001/node.json and let
# check_job sync state back):
mdclaw submit_job \
  --job-dir <job_dir> --node-id prod_001 \
  --script "mdclaw --job-dir <job_dir> --node-id prod_001 run_production \
    --simulation-time-ns <user_specified> \
    --temperature-kelvin <T> --pressure-bar 1.0 --platform CUDA" \
  --partition gpu --gpus 1 --time-limit <estimated> --memory "32G"

# Many prod nodes in parallel (replicates or cross-system): one sbatch
# with --array=0-N-1 via submit_array_job — prefer this over a shell loop
# of submit_job calls. See /hpc-run for the full pattern.
mdclaw submit_array_job --tasks "$TASKS_JSON" --partition gpu --gpus 1 ...
```

Key properties:
- `--job-dir` / `--node-id` on `submit_job` and `submit_array_job` link the
  SLURM job id into `node.json.metadata` and track it in
  `.mdclaw_jobs.jsonl`. `check_job` then reflects SLURM state onto the DAG
  (RUNNING → `queued→running`, FAILED/TIMEOUT → `failed` + stderr tail).
- Inside the job script, omit `--prmtop-file` / `--inpcrd-file` /
  `--restart-from` — DAG auto-resolution takes care of all three.
- Full runbook (cluster inspection, policy, container config, monitoring
  with `/loop`, checkpoint extension): `/hpc-run`.

---

## Common Run Lengths

| Purpose | Time | Notes |
|---|---|---|
| Sanity check | 0.1 ns | Quick validation; default when autonomous and omitted |
| Short | 1-10 ns | Initial testing |
| Production | 50-500 ns | Conformational sampling |
| Extended | 1+ us | Slow processes (folding, binding) |

---

## GPU / HMR

```bash
# GPU selection
--platform CUDA --device-index "0"

# Disable HMR (not recommended)
--no-hmr --timestep-fs 2.0
```

## Restart (state file)

Both `eq` and `prod` nodes write two restart artifacts at end-of-run:
`state.xml` (saveState; positions, velocities, box, context parameters)
and `checkpoint.chk` (saveCheckpoint; binary, includes integrator
state). The resolver prefers `state.xml` because it is **cross-node
portable** — the binary checkpoint encodes GPU-specific context layout
and silently corrupts when loaded on a different GPU architecture (or
even CPU↔GPU). `checkpoint.chk` is retained for bit-identical
reproduction on identical hardware (committor / sensitivity studies)
and as a legacy fallback for DAGs that predate the saveState migration.

### Extension: create a new prod node (recommended)

Each production extension gets its own node and its own trajectory file.
Use `--continue-from` when creating the node to make the intent explicit:

```bash
# Create the extension node (sugar for --parent-node-ids prod_001 with a
# type check that prod_001 really is a prod node)
mdclaw create_node --job-dir <job_dir> --node-type prod \
  --continue-from prod_001 --label "+50ns" \
  --conditions '{"simulation_time_ns": 50}'

# Run it — restart_from resolves via the DAG to prod_001's state.xml
# (or its checkpoint.chk if state.xml is absent in a legacy DAG)
mdclaw --job-dir <job_dir> --node-id prod_002 run_production \
  --simulation-time-ns 50.0 --platform CUDA
```

- `--simulation-time-ns` is the **additional** time to run in this node
  (the `eq→prod` case still behaves as the full production duration
  because the eq state is written with `final_step=0` /
  `currentStep=0` by design).
- With `--continue-from`, `restart_from` resolves to **exactly that
  prod's state file** (`.xml` preferred, `.chk` legacy fallback). No
  silent fallback to a different ancestor: if the named prod has
  neither artifact (still running or failed), the run fails cleanly.
- Without `--continue-from` (plain `--parent-node-ids prod_001`),
  the resolver does a BFS through prod ancestors (state.xml first,
  checkpoint.chk fallback per node), skipping ancestors that have
  neither, and finally falls through to the `eq` ancestor. Only use
  this form if you don't care which prod in the chain is the source.
- Each prod node keeps its own `trajectory.dcd` under `artifacts/`; there
  is **no cross-node DCD append**. Concatenate with mdtraj / `analyze`
  tools when a continuous trajectory is required.
- The binary `.chk` is platform-specific (a CUDA checkpoint cannot load
  on CPU, and vice versa) — this is exactly why `.xml` is preferred.

### Mid-run restart into the same node (advanced / rare)

The tool will append to an existing `trajectory.dcd` **only** if you
re-run against the same `--node-id` AND an existing trajectory is already
present under that node's `artifacts/`. In this (legacy) mode
`--simulation-time-ns` is interpreted as the time to run **in this call**
(added on top of whatever step count the checkpoint carries), so repeated
same-node restarts can still over- or under-run if you lose track.
Prefer the extension-node workflow above.

---

## Membrane Systems

- Uses MonteCarloMembraneBarostat (XYIsotropic + ZFree)
- Longer equilibration recommended (0.5-1 ns NVT + 1 ns NPT)

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| SHAKE constraint failure | Bad geometry | Reduce to 2 fs, or re-prepare |
| NaN energies | Clashes | Re-equilibrate or re-prepare |
| Slow performance | GPU not detected | Check `--platform CUDA` |
| Barostat instability | Temperature mismatch | Match barostat and integrator T |
| `Ensemble mismatch: ... MonteCarloPressure ...` (structured error) | NPT-equilibrated state.xml loaded into an NVT prod context | Either let prod auto-inherit ensemble (omit `--pressure-bar`) or pass the eq's pressure explicitly. To run NVT prod intentionally, rerun the eq node with `--pressure-bar 0` to produce a barostat-free state. |

---

## Verify Output

Read `nodes/prod_001/node.json`:
- `status`: `"completed"`
- `artifacts`: trajectory, final_structure, checkpoint, energy
- `metadata`: simulation_time_ns, platform, hmr, steps
