# Deploy v2: Pixi-Locked Runtime And Standard Skills Distribution

Status: implemented (2026-10-23) except the gates listed under "Release
gates". Decisions taken with the maintainer: pixi unification for the dev
environment and the amd64 container (1A), Agent Skills standard distribution
via the skills CLI (2A), no one-liner installer (3B).

This supersedes the environment layer of
`docs/developer/conda-env-parity-plan.md` (§1 and §3: `environment.yml` as the
shared base plus a conda orchestrator). Its post-install script layer (§2, §4,
§5: openmm-torch / PLUMED source builds, packmol-memgen fixes, parity smoke
checks) is unchanged and still planned — no conda or PyPI package can provide
those pieces, so they stay source-built regardless of the package manager.

## Motivation (what the investigation found, 2026-10-22)

- The committed `environment.yml` was **unsolvable as written**: every
  conda-forge `openmm-torch` build pins `openmm <8.5`, conflicting with the
  `openmm>=8.5.1` floor (already documented in the parity plan; the line is
  now removed). It also cannot express platform differences: `hole2` has no
  osx-arm64 build, so the file did not solve on Apple Silicon either. The
  working Mac env was hand-built (2026-09-07 audit).
- Dependency definitions were spread over `environment.yml` and two
  Dockerfiles (an SST2 bump touched five places), with **no lockfile**: a
  fresh env never reproduced an old one, and the dev env and the image were
  resolved independently at different times.
- `uv` alone cannot replace conda here: AmberTools (cpptraj), `hole2`,
  `pymol-open-source`, and `openmm-torch` are conda-only (the PyPI
  `ambertools-unofficial` repackaging is unofficial and unmaintained enough
  to be an unacceptable dependency). OpenMM itself is pip-installable since
  8.2, but that does not carry the rest of the stack.
- Skills distribution had a parallel standard: the
  [skills CLI](https://github.com/vercel-labs/skills) (`npx skills add`)
  installs Agent Skills into the same mirror directories
  `scripts/install-agent-skills.sh` manages by hand, for ~17 harnesses.

## What changed

### Pixi manifest and lock (R1)

- `pixi.toml` + committed `pixi.lock`: conda-forge stack, salilab MODELLER
  (linux-64 / osx-arm64 feature), per-platform `hole2` and `cuda-version`
  targets, PyPI layer (editable `mdclaw`, git-pinned hpacker via
  pyproject.toml, openff-pablo v0.2.2, SST2 ba48461), dev feature (pytest,
  pytest-asyncio, ruff). Platforms: linux-64, linux-aarch64, osx-arm64, with
  `__cuda=12.9` declared on the linux entries so the lock selects the CUDA
  12.9 conda builds.
- `biopython = "1.81.*"` is pinned in conda because hpacker pins
  `biopython==1.81` exactly; under conda+pip that was satisfied by pip
  silently clobbering conda's 1.88. The locked solve surfaced the conflict
  and now makes the version explicit.
- The lock resolves ambertools 24.8 (floor is `>=24.0`, same as before);
  bumping to 25/26 is a deliberate follow-up, not a side effect.
- Validated on osx-arm64: `pixi install` ~17 s, `--list` tool set identical
  to the hand-built conda env, standard test set + wrapper/deployment/docs
  tests 387 passed, ruff clean.
- `bin/mdclaw` auto-detects the checkout env (`.pixi/envs/default`) first,
  then conda, then SIF/Docker; `MDCLAW_RUNTIME=pixi` overrides.
  `setup-container.sh` skips the pull when the pixi env exists;
  `mdclaw-doctor.sh` reports it.

### Container from the same lock (R2)

- `container/Dockerfile` Stage 1 is now `ghcr.io/prefix-dev/pixi:latest` +
  `pixi install --locked` (fails the build if lock and manifest drifted),
  then the cu118 torch force-reinstall, a real (non-editable) `mdclaw`
  install, the packmol-memgen patch, and `conda-pack -p` on the pixi env.
  Stages 2 (OpenMM 8.5.1 cu118 source build, openmm-torch pinned commit,
  PLUMED) and 3 (slim runtime) are unchanged.
- MODELLER and SST2/hpacker/openff-pablo install steps disappeared from the
  Dockerfile: they come from the lock.

### Skills via the Agent Skills standard (2A)

- `skills/common/SKILL.md` added, so the shared pages install as a sibling
  skill through any Agent Skills-compliant installer and the layout matches
  the existing mirrors. `npx skills add matsunagalab/mdclaw` is the
  documented path for Codex / Cursor / OpenCode and other harnesses;
  `scripts/install-agent-skills.sh` stays for Node-less and checkout-local
  setups. Claude Code plugin and Pi paths are unchanged.

## Release gates (not yet validated)

The amd64 image rewrite cannot be built on this Mac (no Docker daemon). At
the next release, on a Docker host:

1. `docker build -f container/Dockerfile .` — watch two specific risks:
   `CONDA_OVERRIDE_CUDA` satisfying the lock on a GPU-less builder, and
   `conda-pack -p` accepting the pixi prefix (pixi writes standard
   conda-meta, but this combination is unproven here).
2. `container/scripts/test-container.sh` (CPU and `--gpus all`).
3. Only then replace GHCR `latest` / the shared floyd SIF, per the protocol
   in `conda-env-parity-plan.md`.

If conda-pack refuses the pixi prefix, the fallbacks in order: `pixi-pack`
(needs a check that git PyPI deps are supported), or keeping the env path
identical across stages instead of packing.

## Migration state and follow-ups

- `environment.yml` remains for two consumers: existing conda users and
  `Dockerfile.rikyu-arm64`. It is no longer the authoritative definition;
  pixi.toml is. Remove it after the arm64 image migrates to a pixi env
  (`linux-aarch64` is already in the lock; MODELLER on aarch64 still needs
  the tarball route) and after the lab conda users have moved.
- The parity plan's post-install scripts (`build-openmm-torch.sh`,
  `install-hole2.sh` for osx-arm64, `patch-packmol-memgen.sh` locally) are
  still the open work for full dev/SIF parity; pixi's task runner can host
  them (`pixi run setup-local`) when they land.
- Dependency updates now go through `pixi update` + a committed `pixi.lock`
  diff; treat the diff as the environment's change history.
- skills.sh listing appears automatically once installs happen through the
  CLI; no registration step exists.

## Related

- `docs/agents/deployment.md` — user-facing runtime matrix (pixi section).
- `docs/developer/conda-env-parity-plan.md` — superseded env layer, live
  script layer.
- `docs/developer/container.md` — build docs for the pixi-locked Stage 1.
- `docs/memo.md` 2026-10-23 entry — investigation numbers and decisions.
