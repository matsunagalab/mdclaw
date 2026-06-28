# Analyze: Collective Variables & Bias Energy

A production run launched with a custom force (`skills/md-production/custom-force.md`)
emits a per-frame log under the prod node's artifacts.

## Artifacts

| Artifact key | File | Contents |
|---|---|---|
| `collective_variables` | `artifacts/collective_variables.csv` | `step,time_ps,bias_energy_kj_mol[,<cv...>]` per report frame. |
| `collective_variables_meta` | `artifacts/collective_variables.meta.json` | `temperature_kelvin`, `parameters`, custom-force `signature`, `cv_names`, `bias_energy_unit`. |
| `custom_force_script` / `custom_force_module` | the exact bias used | provenance. |

`bias_energy_kj_mol` is always present (read from the dedicated custom-force
group, isolated from the force-field energy). CV columns appear only when the
script returned a `cv_dict`.

## Reading the CV log

```python
import json, pandas as pd
df = pd.read_csv("nodes/<prod_id>/artifacts/collective_variables.csv")
meta = json.load(open("nodes/<prod_id>/artifacts/collective_variables.meta.json"))
# df["bias_energy_kj_mol"], df["<cv_name>"], meta["temperature_kelvin"], meta["parameters"]
```

Use the CV columns to score a candidate collective variable (separation
between states, free-energy profile along the CV, autocorrelation) and to drive
an autoresearch loop: branch a new biased prod node with adjusted parameters or
a refined `energy(positions, ctx)` script and compare. Each trial is a distinct
node keyed by its custom-force signature.

## If no CV columns were logged

When the script returned only the energy (no `cv_dict`), recompute the CV from
the trajectory with the metric tools (`skills/md-analyze/metrics.md`) or mdtraj
directly, using the same atom selections the bias used.

## Toward pymbar (MBAR) reweighting

The CV log is designed as the MBAR input substrate: each frame carries its CV
value(s) and the applied `bias_energy_kj_mol`, and `meta.json` carries the
temperature and bias parameters. Collecting these across multiple biased nodes
(umbrella windows or different bias parameters) gives the per-sample reduced
potentials MBAR needs to estimate the **unbiased** PMF / free-energy surface
along the CV. `pymbar>=4.2` is already in the environment; the reweighting
helper itself is future work — preserve these artifacts so it can consume them.
