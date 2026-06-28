# Production MD: Custom Force / CV Bias (PythonTorchForce)

Attach an arbitrary biasing potential (positional restraint, distance/angle
bias, domain-distance bias, a candidate collective variable, ...) to a
production run. This is the foundation for CV exploration and, later, pymbar
reweighting.

You write one Python function `energy(positions, ctx)` and pass it with
`--custom-force-script`. **You write only the potential energy; MDClaw
computes the forces by autograd** and wraps your function in an
`openmmtorch.PythonTorchForce`. A pre-trained model is simply loaded inside
the same function (e.g. `torch.load(ctx.params["model_path"])`).

Requires an `openmm-torch` build that provides `PythonTorchForce` (code
`custom_force_dependency_missing` otherwise).

> **Availability note**: upstream openmm-torch deprecated `TorchForce` (it
> relies on TorchScript, which PyTorch no longer maintains) and recommends
> `PythonTorchForce` for all cases. `PythonTorchForce` landed on master on
> 2026-06-24 (#179) and is **not in any tagged release yet** (v1.5.1 ships
> only the deprecated `TorchForce`). The MDClaw container source-builds the
> pinned commit, so this works there; a plain `conda install openmm-torch`
> does not yet provide it and the custom-force path reports
> `custom_force_dependency_missing`.

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

Return either a scalar tensor, or `(energy, {cv_name: scalar})` to log CVs.

---

## Template (a): positional restraint on selected atoms

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

## Template (b): harmonic bias on an inter-residue distance (logs the CV)

```python
import torch

def energy(positions, ctx):
    i = ctx.select("name CA and resid 10")
    j = ctx.select("name CA and resid 50")
    d = torch.linalg.norm(positions[i][0] - positions[j][0])
    k = ctx.params["k"]; d0 = ctx.params["d0"]
    bias = 0.5 * k * (d - d0) ** 2
    return bias, {"ca_distance_nm": d}
```

```bash
mdclaw --job-dir <job_dir> --node-id <prod_node_id> run_production \
  --simulation-time-ns 0.1 --temperature-kelvin 300 \
  --custom-force-script dist_bias.py \
  --custom-force-parameters '{"k": 2000.0, "d0": 1.2}'
```

## Template (c): harmonic bias on a domain–domain distance (centroids)

```python
import torch

def energy(positions, ctx):
    a = ctx.select("resid 1 to 60 and name CA")
    b = ctx.select("resid 120 to 180 and name CA")
    ca = positions[a].mean(0)
    cb = positions[b].mean(0)
    d = torch.linalg.norm(ca - cb)
    k = ctx.params["k"]; d0 = ctx.params["d0"]
    return 0.5 * k * (d - d0) ** 2, {"domain_distance_nm": d}
```

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
