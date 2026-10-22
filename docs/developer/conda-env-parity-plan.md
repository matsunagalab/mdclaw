# Plan: make the local conda environment the same packaging as the SIF

Status: plan, 2026-09-11. Nothing below is implemented yet.

## Problem

`environment.yml` is the shared base that both Dockerfiles rewrite with `sed`
before adding the pieces that actually define the runtime. A conda environment
created from the file alone is therefore missing seven things the SIF has:

| piece | environment.yml | amd64 SIF | arm64 SIF |
|---|---|---|---|
| OpenMM | conda `>=8.5.1` | 8.5.1 source (CUDA 11.8) | 8.5.1 source (CUDA 13.0) |
| PyTorch | conda `>=2.7,<2.8` | pip `2.7.1+cu118` | conda 2.10 cuda130 |
| openmm-torch | conda `>=1.5` (no `PythonTorchForce`) | source, commit `b653290` | same |
| PLUMED 2.9.4 + openmm-plumed 2.1 | comment only | `build-plumed.sh` | same |
| MODELLER 10.8 | absent | salilab conda | generic tarball |
| dev extras (pytest, ruff) | `-e .` | `pip install ".[dev]"` | same |
| packmol-memgen fixes (`v3numpy`, PPM3 `immers`) | absent | applied | applied |

The audit of 2026-09-11 (`docs/memo.md`) found the Mac env matches the SIF only
because every one of these was done by hand on 2026-09-07; a fresh
`conda env create` would lose all of them. Separately, `hole2` was added to
`environment.yml` on 2026-09-09 and conda-forge has no osx-arm64 build, so the
file no longer solves on Apple Silicon.

## Goal

The same *packaging shape* everywhere: identical package set, the same
source-built plugins at the same pinned commits, the same patches, the same
manifests, and the same smoke check. Versions may differ (conda OpenMM 8.6 vs
the source-built 8.5.1; CPU/MPS torch vs cu118; Reference/CPU/OpenCL platforms
on macOS). The way to get that is not a longer `environment.yml` but one set
of post-install scripts that the local setup and both Dockerfiles run
verbatim.

## What conda can and cannot hold (checked on conda-forge, 2026-09-11)

The question "why not put everything in `environment.yml`" has a per-piece
answer. Expressible in the file: the dev extras (`-e .[dev]`), the runtime
environment variables (an `environment.yml` `variables:` section is exported
on activation), and gfortran (`fortran-compiler`). MODELLER is a salilab conda
package on osx-arm64 and linux-64 but not linux-aarch64, and `hole2` is on
linux-64, linux-aarch64 and osx-64 but not osx-arm64; conda env files have no
platform selectors, so neither can live in the shared file without a second,
platform-specific file. Three pieces have no conda package at all:

- openmm-torch: conda-forge's newest is 1.5.1, without `PythonTorchForce`,
  and every build pins `openmm <8.5`. With `openmm>=8.5.1` the current
  `openmm-torch>=1.5` line is unsolvable (verified with `conda create
  --dry-run`); the 2026-09-07 local update dropped it to solve at all.
- openmm-plumed: the conda 2.1 builds pin `openmm <8.5` and `plumed <2.10`,
  and the box-pointer-lifetime patch exists only in the source build.
- packmol-memgen fixes: post-install file edits plus a Fortran rebuild.

So a post-install script layer is unavoidable; the design below keeps it as
small as conda allows and moves everything conda can express into the file.

## Design

### 1. `environment.yml` stays the shared base, and only that

- Change the pip line `- -e .` to `- -e .[dev]`. The amd64 Dockerfile's
  `sed '/- -e \./d'` still strips it; the arm64 one drops the whole pip
  section; both already install `.[dev]` themselves.
- Remove `openmm-torch`. The line is unsolvable next to `openmm>=8.5.1`
  (see above), both images strip it, and the conda package ships only the
  deprecated `TorchForce` and collides with the source build (same
  `openmmtorch.py` path). The `openmm` + `pytorch` conda pins stay, they
  are what the local source builds link against.
- Keep `hole2=2.3.1`. Both images inherit it from this file today, so removing
  it would silently drop HOLE from the next image build. The osx-arm64 case is
  the exception, and the exception does the stripping: the local orchestrator
  applies the same kind of `sed` the Dockerfiles use, then `install-hole2.sh`
  builds the executables from source.
- Add a `variables:` section with `PYTHONUTF8=1`, `MDCLAW_PLUMED_VERSION`,
  `MDCLAW_MODELLER_VERSION`, `MDCLAW_PPM3_PATCHED`, matching the Dockerfile
  `ENV` lines. This replaces the `write-env-vars.sh` script.
- Do not add `fortran-compiler` here. The PPM3 rebuild and the osx-arm64
  `hole2` build need gfortran, but both images take it from apt and putting it
  in the shared file would only grow the packed `/opt/mdclaw` by a toolchain
  the runtime never uses. The local orchestrator installs it into the env.
- Keep the build toolchain lines (cmake, swig, patch, make, cxx-compiler,
  git): they are what make the source builds possible in a conda prefix.

### 2. One post-install step per piece, in `container/scripts/`

Each script takes a prefix as its first argument, is idempotent, and writes a
manifest under `$PREFIX/share/mdclaw/` (the pattern `build-plumed.sh` already
follows with `plumed-build.json`; the Mac env also carries an ad-hoc
`openmm-torch-build.json` from the 09-07 hand build, which is the template).

| script | does | platform branches |
|---|---|---|
| `build-openmm-torch.sh PREFIX BUILD_DIR [JOBS]` | fetch commit `b6532900…`, cmake against `$PREFIX` OpenMM and the installed torch, `PythonInstall`, assert `PythonTorchForce`, write `openmm-torch-build.json` | CUDA on when `nvcc` + `torch.version.cuda` exist, else off; OpenCL on when the prefix OpenMM has it (macOS conda build does) |
| `build-plumed.sh` | unchanged | none |
| `install-modeller.sh PREFIX [VERSION]` | salilab conda package where it exists, generic tarball layout otherwise; no key baked in | salilab has linux-64 and osx-arm64; linux-aarch64 uses the tarball route now inlined in `Dockerfile.rikyu-arm64` |
| `install-hole2.sh PREFIX` | no-op where conda-forge `hole2` solved from `environment.yml`; on osx-arm64 build `hole`, `sph_process`, `sos_triangle` from the upstream source (osmart/hole2, plain Fortran) into `$PREFIX/bin` and copy the radius tables + LICENSE to `$PREFIX/share` | osx-arm64 only takes the source route |
| `patch-packmol-memgen.sh PREFIX` | the `v3numpy` sed and the PPM3 `opm.f` comma fix + `immers` rebuild, with the same post-checks the Dockerfiles run | none |

### 3. One orchestrator for the local case: `scripts/setup-conda-env.sh`

```
scripts/setup-conda-env.sh [-n ENV] [--update] [--jobs N]
```

1. `mamba env create` (or `update --prune` with `--update`) from
   `environment.yml`, after stripping the `hole2` line on osx-arm64, then
   `mamba install fortran-compiler` into the env.
2. Run the §2 scripts in order: patch-packmol-memgen, build-openmm-torch,
   build-plumed, install-modeller, install-hole2. Each is
   skipped when its manifest already records the pinned commit/version, so
   re-running after a `git pull` only redoes what changed.
3. Run the smoke check (§4) and print a one-screen summary of what is
   installed, with the manifests.

This is the only documented way to create or refresh the local env. The
`README.md` conda section, `docs/developer/configuration.md` ("authoritative
local development environment" is rewritten), `plumed.md` §conda, and
`CLAUDE.md` Development Defaults all point at it.

### 4. The Dockerfiles call the same scripts

Replace the inlined blocks in `container/Dockerfile` and
`Dockerfile.rikyu-arm64` (packmol patch, PPM3 rebuild, openmm-torch build,
MODELLER install, `.[dev]` install) with `COPY container/scripts/… && RUN
bash …`. The plumed step is already done this way, so the change is
mechanical. The version pins (`OPENMM_TORCH_COMMIT`, `MODELLER_VERSION`,
`PLUMED` commits, `hole2` version) then live in the scripts, and the Dockerfile
`ARG`s either disappear or default to the script values.

This is what makes "same packaging" true by construction rather than by
review. It is also the only part that costs a SIF rebuild to validate, so it is
staged last (see Order).

### 5. One smoke check for both

`container/scripts/test-container.sh` already gates its extra checks on
`MDCLAW_MODELLER_VERSION`, `MDCLAW_PLUMED_VERSION`, `MDCLAW_PPM3_PATCHED` and
runs the HOLE synthetic-pore check unconditionally. Make it runnable against
an activated conda prefix as well as inside the image (it mostly is: it calls
`python`, `mdclaw`, `tleap` from `PATH`). Add three checks it does not have
today: `openmmtorch.PythonTorchForce` present, `openmmplumed` importable, and
the manifests under `share/mdclaw/` matching the pinned commits. The manifest
check is gated on the manifest file existing until step 3 lands, because the
images built today write `plumed-build.json` but no openmm-torch manifest. The NVRTC and
GPU checks stay gated on CUDA being present.

`scripts/mdclaw-doctor.sh` gets a short "runtime parity" section that prints
the manifests, so the next audit is one command.

## Effect on the images before step 3

Checked against the two Dockerfiles as they are now:

- `-e .[dev]`: the amd64 `sed '/- -e \./d'` still matches and strips it; the
  arm64 file drops the whole pip section. No change.
- removing `openmm-torch`: both images already strip that line. No change.
- `variables:`: passes through both `sed` filters into the `mamba env create`
  input. The runtime never `conda activate`s and both Dockerfiles set the
  same values with `ENV`, so at worst the section is ignored. Confirm in the
  amd64 build log that the miniforge `mamba` does not reject it.
- new files under `container/scripts/`: both Dockerfiles `COPY` only
  `build-plumed.sh` and `entrypoint.sh` by name, so new scripts are inert
  until a Dockerfile references them.
- `test-container.sh`: the two new import checks pass on today's images (the
  amd64 build already asserts `PythonTorchForce`); the manifest check is
  gated as described above.
- nothing else reads `environment.yml`: no CI workflow exists, `bin/`,
  `hooks/` and the plugin wrapper do not run `conda env create`, and
  `mdclaw-doctor.sh` only prints the command in a hint.
- the SIF-overlay loop (`PYTHONPATH=$PWD singularity exec …`) is unaffected:
  nothing under `mdclaw/` changes.

Step 3 is the only step that changes what an image contains, and it is
validated by a full amd64 build plus `test-container.sh` before the GHCR
`latest` or the shared floyd SIF is replaced; the arm64 image follows at its
next scheduled rebuild.

## Order and validation

1. **Scripts first, no Dockerfile change.** Write the §2 scripts and the
   orchestrator; adjust `environment.yml` (§1). Validate on this Mac by
   building a *fresh* env (`-n mdclaw-fresh`) from scratch, then:
   `ruff check`, the default test set, `tests/test_plumed.py`, the custom
   force tests, the HOLE smoke, `mdclaw --list` compared to the SIF's list.
   Only then replace the hand-built `mdclaw` env with the scripted one.
2. **Docs** (README, configuration.md, plumed.md, container.md HOLE section,
   CLAUDE.md/AGENTS.md).
3. **Dockerfiles** switched to the scripts, validated with the amd64 image
   build + `test-container.sh`, then the arm64 image via
   `build-rikyu-arm64.sh` at the next scheduled SIF rebuild. No shared SIF is
   replaced until that passes.
4. `docs/memo.md` entry with the fresh-env numbers and any deviation found.

## Risks and decisions taken

- **hole2 from source on osx-arm64.** Small Fortran programs; the risk is
  build flags on Apple clang + gfortran. Fallback if it fights back: keep the
  script's source route but document `/usr/local/bin` binaries as an
  acceptable PATH-provided alternative (that is what the Mac uses today and
  the smoke check passes with it).
- **openmm-torch on macOS** links against conda OpenMM 8.6 and CPU torch
  2.7.1. It already works this way (manifest `openmm-torch-build.json`,
  platforms Reference/CPU/OpenCL). A future conda OpenMM bump changes the ABI
  and the script must rebuild; the manifest records the OpenMM version so the
  orchestrator can detect that.
- **pip stage side effects.** `pip install -e .[dev]` replaces conda's rdkit
  and numpy with wheels on every platform, locally and in the images alike.
  Left as is: it is the same on both sides, which is the goal. Revisit only if
  a wheel-vs-conda mismatch bites.
- **Dockerfile refactor cost.** Hours of build time on the arm64 side. That
  is why it is step 3, not step 1; steps 1-2 alone already remove the
  hand-maintained drift on the Mac.
