# Deploy Simplification And Model-Backend Clarification Plan

Status: implemented (2026-07-02). Boltz pinned to 2.2.1 in
`mdclaw/surrogate_server.py` (`BOLTZ_VERSION`). Follow-up: backends now declare
capabilities (`supports_sampling` / `supports_prediction`) and callers dispatch
via `models_with_capability` / `resolve_prediction_backend`, so models are
swappable without touching callers. See `docs/developer/model-backends.md`.

This is a design memo for three related deployment changes agreed with the
maintainer:

1. Simplify and clarify deployment (highest priority) while keeping every
   existing entry path.
2. Clarify Boltz-2 (CUDA) handling by moving it out of the core runtime into
   the same isolated-backend pattern as BioEmu.
3. Clarify BioEmu / Boltz GPU distribution: do not bake the CUDA stack into the
   container image; install it into an isolated venv on first run and cache
   weights on a shared filesystem.

## Motivation: What Is Wrong Today

### Boltz-2 is promised but not installed

- `README.md` and `docs/developer/container.md` state the container "contains
  Boltz-2".
- No file installs it: `environment.yml`, `container/Dockerfile`, and
  `pyproject.toml` never reference `boltz`.
- `mdclaw/genesis_server.py` resolves it with
  `BaseToolWrapper("boltz", conda_env="mdclaw")` and returns
  `boltz_executable_not_found` when it is absent.
- Boltz-2 ships its own Torch/CUDA stack. Installing it into the main `mdclaw`
  env would conflict with the deliberate OpenMM `cu118` pin — the exact reason
  BioEmu was isolated into a venv.

Conclusion: Boltz is a hidden, uninstalled dependency, and the docs overpromise.

### BioEmu GPU distribution is ambiguous

- Local path is clean: `setup_surrogate_backend --model bioemu --device cuda`
  creates `~/.cache/mdclaw/surrogates/bioemu/venv`.
- Container path bakes the venv at build via `BIOEMU_DEVICE`, but the default
  is `cpu`, and the release build command (`release.md`, `container.md`) does
  not pass `--build-arg BIOEMU_DEVICE=cuda`.
- Net effect: the published GHCR image (and the SIF derived from it) most
  likely ships CPU-only BioEmu, so GPU HPC gets no GPU acceleration.
- A SIF is read-only, so `setup_surrogate_backend` cannot install into
  `/opt/mdclaw/surrogates` at runtime. Runtime install requires a writable,
  ideally shared, cache directory.

### Deployment docs are duplicated and have implicit policy

- The deployment matrix exists in both `README.md` and
  `docs/agents/deployment.md`.
- `bin/mdclaw` and `scripts/setup-container.sh` silently prefer an existing
  `mdclaw` conda env over the container, and `setup-container.sh` no-ops when
  the env exists. This policy is correct but undocumented in one obvious place.
- Version sync spans five files (`release.md`); there is a checker
  (`mdclaw-doctor.sh`) but no single writer.

## Decisions (agreed)

- Keep all four deploy entry paths (Claude plugin, Pi, repo-local agents,
  direct/conda). Remove duplication and implicit behavior only.
- Unify Boltz-2 into the isolated-backend pattern used by BioEmu; remove it
  from the core runtime and from docs that claim it is bundled.
- Canonical GPU distribution: install the CUDA backend venv on first run and
  cache on a shared filesystem; do not bake CUDA model stacks into the image.

Decisions resolved on review (2026-07-02):

- CLI naming: add generic `setup_model_backend` / `check_model_backend`; keep
  `setup_surrogate_backend` / `check_surrogate_backend` as aliases
  (`model=bioemu`). No hard rename.
- Image baking: drop baked backend venvs entirely. Runtime-install is the only
  path; the Dockerfile no longer builds a BioEmu venv or takes `BIOEMU_DEVICE`.
- MSA egress: keep the current default of the Boltz MSA server
  (`--use_msa_server`) when no `--msa-path` is given. Network egress from the
  cluster is acceptable by default.
- Boltz version: pin to an explicit version (reproducibility). The maintainer
  will supply the concrete version before implementation; the code should carry
  a single pinned constant, not "latest".

## Design

### Workstream A: Deploy simplification and clarification

- A1. Make `docs/agents/deployment.md` the single source of truth for the
  deployment matrix. Shrink the `README.md` "Install / Deploy" section to a
  short overview plus a link. Do not remove any entry path.
- A2. Document the runtime-selection policy in one place (the conda-preferred
  order in `bin/mdclaw`, and `setup-container.sh` no-op when the conda env
  exists). Make the `MDCLAW_RUNTIME` override prominent. Behavior unchanged.
- A3. Add `scripts/bump-version.sh` that writes all five version locations at
  once (`mdclaw/__init__.py`, `pyproject.toml`, `.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json` x2, `package.json`). `mdclaw-doctor.sh`
  already verifies; this adds the writer.

### Workstream B: Boltz-2 as an isolated model backend

Generalize the backend registry so both BioEmu and Boltz are isolated venvs
resolved through one mechanism.

- B1. Introduce a generic model-backend registry and CLI:
  - `setup_model_backend --model {bioemu,boltz} --device {cpu,cuda}`
  - `check_model_backend --model {bioemu,boltz}`
  - Keep `setup_surrogate_backend` / `check_surrogate_backend` as thin aliases
    that call the generic path with `model=bioemu`, so existing skills, docs,
    README, and tests do not break.
  - Backends live under `$MDCLAW_SURROGATE_DIR/<model>/venv` (rename the env
    var conceptually to "model backends", but keep `MDCLAW_SURROGATE_DIR` as the
    accepted name for back-compat; optionally accept a new
    `MDCLAW_MODEL_BACKEND_DIR` alias).
  - `BoltzBackend.install_package(device)` returns `boltz` (cpu) or the
    appropriate CUDA extra; pin the Boltz version explicitly.
- B2. Rework `boltz2_protein_from_seq` in `mdclaw/genesis_server.py`:
  - Remove `BaseToolWrapper("boltz", conda_env="mdclaw")`.
  - Resolve the boltz backend venv Python and invoke `python -m boltz ...`
    (or the venv `boltz` entry point) as a subprocess, mirroring the surrogate
    backend runner.
  - On missing backend, return a structured `boltz_backend_not_installed` code
    (add to `guardrail_codes.py`) with the reproducible
    `setup_model_backend --model boltz --device cuda` command. Keep the
    existing `boltz_execution_failed` path for run failures.
  - Do not change the public tool signature (`--amino-acid-sequence-list`,
    `--smiles-list`, `--affinity`, `--msa-path`, `--num-models`). Skills stay
    unchanged.
- B3. Docs:
  - Remove "contains Boltz-2" from `README.md` and
    `docs/developer/container.md`.
  - Add a Boltz backend setup section to `skills/boltz-predict/setup.md` and
    `skills/boltz-predict/error-handling.md` (handle
    `boltz_backend_not_installed`).
  - Update `docs/developer/tool-reference.md` and
    `docs/developer/configuration.md`.
- B4. Tests:
  - Update `tests/test_genesis_server.py` stubs so the boltz subprocess is
    mocked at the new venv-resolution path.
  - Add a unit test for `setup_model_backend --model boltz` command
    construction (mock the install), mirroring the surrogate tests.
  - Verify CLI discovery lists `setup_model_backend` / `check_model_backend`.

### Workstream C: GPU distribution via runtime install + shared cache

- C1. Stop baking model backends into the image (no opt-in bake kept):
  - Remove the BioEmu venv build step and the `BIOEMU_DEVICE` build-arg from
    `container/Dockerfile` entirely. Runtime-install is the only path.
  - Document that on a read-only SIF, `setup_model_backend` must target a
    writable `MDCLAW_SURROGATE_DIR` on a shared filesystem, bind-mounted into
    the container. Provide the exact `singularity exec --nv --bind ...` and
    `docker run -v ...` examples.
  - Keep model weight caches (BioEmu / ColabFold / Boltz) on the shared FS via
    the upstream cache env vars, documented in `configuration.md`.
- C2. Add a CUDA note to `container/docs`: these backend venvs pull their own
  Torch and are intentionally separate from the OpenMM `cu118` pin, so their
  CUDA/driver requirements are independent and must be checked with
  `check_model_backend`.

## Non-Goals

- No change to the OpenMM / AmberTools core runtime or the `cu118` build.
- No removal of any deploy entry path.
- No change to `boltz2_protein_from_seq` or `generate_surrogate_candidates`
  tool signatures.
- No auto-install on first sampling run; setup stays an explicit command to
  avoid surprise network access on HPC/proxy environments.

## Suggested Implementation Order

1. A (docs dedupe + policy note + `bump-version.sh`): low risk, no code paths.
2. B (backend registry generalization + `genesis_server` rework + tests):
   the core behavioral change.
3. C (container debake + shared-cache docs): depends on B being in place.

## Open Questions For Review

All review questions are resolved (see "Decisions resolved on review" above).
The only remaining input needed is the concrete Boltz version string, which the
maintainer will provide before implementation.

## Related

- `docs/developer/bioemu-integration-plan.md`
- `docs/developer/container.md`
- `docs/developer/release.md`
- `docs/agents/deployment.md`
- `wiki/projects/mdclaw.md`
