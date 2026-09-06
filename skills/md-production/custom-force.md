# Production MD: Custom Force / CV Bias (PythonTorchForce)

Attach an arbitrary biasing potential that MDClaw's declarative harmonic
distance restraint cannot express (for example angle/dihedral bias or an ML
potential). For harmonic atom/center-of-mass distances, use
`skills/md-production/distance-restraints.md`; it stays inside native OpenMM
kernels and avoids per-step Python/autograd overhead.

You write one Python function `energy(positions, ctx)` and pass it with
`--custom-force-script`. **You write only the potential energy; MDClaw
computes the forces by autograd** and wraps your function in an
`openmmtorch.PythonTorchForce`. A pre-trained model is simply loaded inside
the same function (e.g. `torch.load(ctx.params["model_path"])`).

Requires an `openmm-torch` build that provides `PythonTorchForce` (code
`custom_force_dependency_missing` otherwise); the MDClaw container ships it.

---

## The iron rules (read before writing a script)

1. **Do not compute forces.** Return only the scalar potential energy (kJ/mol).
   MDClaw differentiates it: `forces = -dE/dx`.
2. **Write CVs directly** on `positions` using `ctx.select(...)` and `torch`.
   There is no CV library — full freedom, one function.
3. **Use `ctx.reference` / `ctx.params`**, never hard-coded coordinates or
   magic numbers. Tunables come from `--custom-force-parameters` (JSON).
4. **Differentiable torch ops only.** No `.item()`, no `int()`/`float()` casts
   of `positions`, no in-place writes, no numpy. Violations surface as
   `custom_force_contract_error`.
5. Units: `positions` and `ctx.reference` are **nm**; energy is **kJ/mol**.

`ctx` provides:

| Attribute | Meaning |
|---|---|
| `ctx.select("name CA and resid 10")` | mdtraj VMD-style DSL → atom-index tensor (matches the System; rows of `positions`). |
| `ctx.reference` | (N,3) reference coords in nm, fixed across restarts, same device/dtype as `positions`. |
| `ctx.params` | dict from `--custom-force-parameters`. |
| `ctx.atomic_numbers` | list of atomic numbers per particle. |
| `ctx.box` | box tensor when `params["pbc"]` is set (else `None`). |
| `ctx.steering` | `None` for ordinary scripts; read-only steering information for a managed ramp or its fixed continuation (below). |

Return either a scalar tensor, or `(energy, {cv_name: scalar})` to log CVs.

---

## Template: positional restraint on selected atoms

```python
import torch

def energy(positions, ctx):
    sel = ctx.select(ctx.params.get("selection", "name CA"))
    k = ctx.params.get("k", 1000.0)  # kJ/mol/nm^2
    disp = positions[sel] - ctx.reference[sel]
    return 0.5 * k * (disp ** 2).sum()
```

```bash
mdclaw --job-dir <job_dir> --node-id <prod_node_id> run_production \
  --simulation-time-ns 0.1 --temperature-kelvin 300 \
  --custom-force-script restraint_ca.py \
  --custom-force-parameters '{"selection": "name CA", "k": 1000.0}'
```

For another CV bias, compute the scalar CV from `positions` with torch ops and return
`(bias_energy, {"<cv_name>": cv_value})` so the CV is logged.

## Steer an arbitrary CV, then hold it for umbrella sampling

Use the same function and `--custom-force-parameters`; add
`--steering-time-ns` and optionally `--steering-update-interval-ps` (default 1 ps).
MDClaw manages the schedule and DAG; your function defines the CV, potential,
and how the target or strength depends on progress. Existing scripts and the
meaning of `ctx.reference` are unchanged.

`ctx.steering` supplies:

| Attribute | Meaning |
|---|---|
| `progress` | Applied 0–1 progress; right-endpoint updates on a fixed step grid, then held at 1. |
| `initial_positions` | Actual steering input coordinates in nm, frozen across restarts and fixed continuations, on the current device/dtype. |
| `initial_box` | Box at that same starting point (nm), or `None` for a non-periodic system. |

Do not mutate these values or advance a counter inside `energy`: the function
is evaluated for forces, validation and logging, not once per MD step. Define
the potential using positions, fixed parameters, the frozen input and progress.
Nonlinear schedules (e.g. `progress**2`), multiple CVs and ML potentials remain
ordinary Python/torch code. History-dependent biases or online model training
need additional state management and are not covered by this restart contract.
Keep external model weights/dependencies immutable; they are not bundled by it.

Example `angle_bias.py` (three single-atom selections; angle in radians):

```python
import torch

def angle(pos, box, ctx):
    a, b, c = (pos[ctx.select(ctx.params[key])][0] for key in ("a", "b", "c"))
    u, v = a - b, c - b
    if box is not None:
        # Orthorhombic box example; use suitable imaging for other cell shapes.
        lengths = torch.diag(box)
        u = u - torch.round(u / lengths) * lengths
        v = v - torch.round(v / lengths) * lengths
    return torch.atan2(torch.linalg.vector_norm(torch.linalg.cross(u, v)), torch.dot(u, v))

def energy(positions, ctx):
    s = ctx.steering
    if s is None:
        raise ValueError("Start with --steering-time-ns or continue a managed steering branch")
    start = angle(s.initial_positions, s.initial_box, ctx)
    center = start + s.progress * (ctx.params["target_rad"] - start)
    theta = angle(positions, ctx.box, ctx)
    return 0.5 * ctx.params["k"] * (theta - center)**2, {"angle_rad": theta, "center_rad": center}
```

Choose non-collinear atoms and targets away from 0 and pi; angle gradients
are singular at collinearity. A dihedral requires a periodic angular difference,
not an unwrapped subtraction across -pi/pi. For this example `k` is in
kJ/mol/rad² and `a`, `b`, `c` must each select exactly one atom.

For **each target**, create `steered_X` from the **same completed eq**, then
create its own `umbrella_X` using `--continue-from <steered_id>`. Never seed X
from Y. Example after selecting the atoms and setting `PARAMS`:

```bash
# PARAMS is one JSON object, e.g. {"a":"index 0","b":"index 4","c":"index 8",
# "k":100,"target_rad":1.8,"pbc":true}; verify the selections and box first.
mdclaw create_node --job-dir <job_dir> --node-type prod \
  --parent-node-ids <common_eq_id> --label steered_X \
  --conditions '{"simulation_time_ns":0.5,"steering_time_ns":0.5}'
mdclaw explain_node --job-dir <job_dir> --node-id <steered_id>
mdclaw --job-dir <job_dir> --node-id <steered_id> run_production \
  --simulation-time-ns 0.5 --steering-time-ns 0.5 \
  --custom-force-script angle_bias.py --custom-force-parameters "$PARAMS"

mdclaw create_node --job-dir <job_dir> --node-type prod \
  --continue-from <steered_id> --label umbrella_X \
  --conditions '{"simulation_time_ns":100}'
mdclaw explain_node --job-dir <job_dir> --node-id <umbrella_id>
mdclaw --job-dir <job_dir> --node-id <umbrella_id> run_production --simulation-time-ns 100
```

Omitting the steering duration after completion keeps the inherited script,
parameters and initial geometry, with progress fixed at 1. Further umbrella
continuations keep that same state. An unfinished ramp refuses this handoff:
resume with the original total steering duration and update interval instead;
`simulation_time_ns` is only the additional segment length. Within this managed
lineage, changing the script, parameters, timestep or active ramp schedule is
refused. For a new protocol, create a new explicit branch from the intended seed.

Steered nodes have `sampling_role=steered`; fixed continuations have
`sampling_role=fixed_bias` and are not excluded as initialization. The CV CSV
adds reserved column `steering_progress`; your `cv_dict` records actual CVs and
targets. Keep the XML state, `steering.json` and `steering_initial.npz` together
for portable recovery in a new node. The script hash, parameters and initial
geometry hash are checked; Python globals are not checkpointed. Existing
static custom-force continuations are unaffected by these steering checks.

Schedule completion does not guarantee actual target attainment or equilibrium.
Inspect CV/target traces, allow fixed-center relaxation and exclude burn-in
before assessing overlap or a PMF. Automatic DAG concatenation stops at the
steered/fixed boundary. The example durations are illustrative, not convergence
criteria. Do not combine this route with `--distance-restraints`.

## Using a pre-trained model

There is no separate module route. Load the model inside `energy`:

```python
import torch

_MODEL = None

def energy(positions, ctx):
    global _MODEL
    if _MODEL is None:
        _MODEL = torch.jit.load(ctx.params["model_path"]).eval()
    e = _MODEL(positions)  # model returns a scalar energy in kJ/mol
    return e
```

```bash
mdclaw --job-dir <job_dir> --node-id <prod_node_id> run_production \
  --simulation-time-ns 0.1 --temperature-kelvin 300 \
  --custom-force-script ml_potential.py \
  --custom-force-parameters '{"model_path": "model.pt"}'
```

---

## Outputs (CV / bias log)

When a custom force runs, production writes per-report-frame:

- `artifacts/collective_variables.csv` — columns
  `step,time_ps,bias_energy_kj_mol[,<cv...>]`. `bias_energy_kj_mol` always
  present (read from the dedicated force group); CV columns appear only when
  the script returned a `cv_dict`.
- `artifacts/collective_variables.meta.json` — temperature, parameters, the
  custom-force signature, and CV names (the reconstruction info pymbar/MBAR
  reweighting needs later).

These are recorded on the node as `collective_variables` /
`collective_variables_meta` artifacts, and the script is copied to
`artifacts/custom_force_script.py` for provenance. See `skills/md-analyze`
for consuming the CV log.

---

## Node declaration & continuation

Do **not** declare `custom_force` in `create_node --conditions`: the bias
signature (including its content `sha256`) is recorded automatically into the
prod node's `metadata.custom_force` and `artifacts` when the run completes, and
a declared condition is validated by *exact* match — a partial
`{"kind": ...}` would always fail with `condition_mismatch`. Use a normal label
to keep the biased branch distinct:

```bash
mdclaw create_node --job-dir <dir> --node-type prod --parent-node-ids <eq_id> \
  --label "dist_bias" --conditions '{"simulation_time_ns": 1}'
```

`--continue-from` a biased prod inherits the same script and parameters
automatically (override with explicit flags to change the bias).

---

## Codes

| Code | Meaning / fix |
|---|---|
| `custom_force_dependency_missing` | `openmm-torch` with `PythonTorchForce` not installed; use a runtime that ships it (the MDClaw container). |
| `custom_force_script_error` | Script failed to import or has no `energy(positions, ctx)`. |
| `custom_force_contract_error` | `energy` returned a non-scalar / non-finite value, a bad tuple, or a non-differentiable result. |
| `custom_force_topology_mismatch` | `topology.pdb` atom count ≠ System particle count; rebuild the topo node. |
| `custom_force_selection_empty` | `ctx.select(...)` matched 0 atoms; fix the selection string. |
