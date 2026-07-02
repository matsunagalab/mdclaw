# Model Backends (Adding And Swapping Models)

Heavy AI models (BioEmu, Boltz-2, and future predictors/samplers) run in
isolated venvs, never in the conda `mdclaw` environment, because they ship
their own Torch/CUDA stacks that conflict with the OpenMM `cu118` pin. This
page is the contract for adding or swapping a model backend.

The registry lives in `mdclaw/surrogate_server.py`:

- `VenvBackend`: base class owning the venv lifecycle (`setup`, `check`,
  `entry_point`) and capability declaration.
- `MODEL_BACKENDS`: `name -> backend instance` registry.
  `SURROGATE_BACKENDS` is a backward-compatible alias.

## Capabilities, Not Names

Callers dispatch on what a backend *can do*, not on its name, so models stay
swappable:

- `supports_sampling = True`: conformational ensemble generation, consumed by
  `generate_surrogate_candidates`.
- `supports_prediction = True`: structure prediction from sequence, consumed by
  `genesis_server.boltz2_protein_from_seq` via `resolve_prediction_backend`.
- `entry_script`: the console script inside the venv that prediction callers
  invoke (e.g. `"boltz"`).

Helpers:

- `models_with_capability("sampling" | "prediction")` lists capable backends.
- `resolve_prediction_backend(model=...)` returns `(entry_point_path, check)`
  for an installed predictor; the predictor can be swapped (boltz ->
  alphafold3 -> ...) without touching the caller.

## Add A New Backend

1. Subclass `VenvBackend` in `mdclaw/surrogate_server.py`:

```python
@dataclass
class Chai1Backend(VenvBackend):
    name: str = "chai1"
    supports_prediction = True   # or supports_sampling = True
    entry_script = "chai"        # only for prediction callers

    def install_spec(self, device: str) -> list[str]:
        # pip requirement strings; pin the version for reproducibility
        return ["chai_lab==0.6.1"]

    def import_check_code(self) -> str:
        # must print one JSON line with at least {"version": ...}
        return (
            "import json\n"
            "from importlib import metadata\n"
            "print(json.dumps({'version': metadata.version('chai_lab')}))\n"
        )
```

2. Register it:

```python
MODEL_BACKENDS = {
    "bioemu": BioEmuBackend(),
    "boltz": BoltzBackend(),
    "chai1": Chai1Backend(),
}
```

That is the whole change for a prediction backend: `setup_model_backend
--model chai1`, `check_model_backend --model chai1`, and any
prediction-capable caller resolve it automatically.

3. For a **sampling** backend, also implement `sample(...)` (see
   `BioEmuBackend.sample`) returning a `subprocess.CompletedProcess` whose
   output `generate_surrogate_candidates` can normalize into candidate PDBs.

4. Add unit tests mirroring `tests/test_surrogate_server.py`
   (`setup_model_backend` command construction with mocked install,
   `check_model_backend` missing-venv message, capability dispatch).

5. If the backend emits a new agent-facing failure code, register it in
   `mdclaw/guardrail_codes.py` and regenerate goldens
   (`scripts/gen_guardrail_codes.py`, `scripts/gen_guardrail_codes_md.py`).

## Swap A Predictor

`boltz2_protein_from_seq` currently pins `model="boltz"` in
`_resolve_boltz_backend`. To swap the default predictor, change that single
`model=` argument (or thread it through as a parameter). No other caller code
changes, because resolution is capability-based.

## Non-Pip Installs

`install_spec` returns pip requirement strings. If a future backend needs a
conda-only dependency, a git install, or explicit model-weight fetching,
override `setup(...)` in the subclass rather than forcing everything through
`install_spec`. Keep weight caches on the shared filesystem
(`MDCLAW_SURROGATE_DIR`) so they download once.

## Runtime And Distribution

Backends are installed at runtime, not baked into the container image. On a
read-only SIF, point `MDCLAW_SURROGATE_DIR` at a writable, ideally shared,
filesystem and bind-mount it. See `docs/developer/container.md` and
`docs/developer/configuration.md`.
