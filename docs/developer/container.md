# Container Runtime Build And Distribution

The container is MDClaw's packaged scientific runtime. It contains the `mdclaw`
CLI plus CUDA runtime, PyTorch, AmberTools, OpenMM, PyMOL
(`pymol-open-source`, for headless structure previews), MDTraj, and MDAnalysis.

MODELLER **is** baked in, unlike those backends, because it is a small conda
package with no competing CUDA stack. It ships unlicensed: `config.py` keeps the
`XXXX` placeholder, and `mdclaw/genesis/modeller.py` injects a synthetic
`modeller.config` built from a `KEY_MODELLER*` environment variable before
importing MODELLER, so each user supplies their own key per run and no key is
ever in the image. Verified against MODELLER 10.8: installing without a key
succeeds, and an injected key is what MODELLER actually validates.

The two images install it by different routes because the `salilab` conda
channel supplies no Linux ARM64 build. The amd64 image installs the conda package
from `container/Dockerfile` — not from `environment.yml`, which
`Dockerfile.rikyu-arm64` shares and which would then fail to solve on arm64.
The arm64 image takes MODELLER from the generic tarball instead, which ships an
aarch64 build (`lib/armv8-gnu`, gfortran-linked) beside the x86_64 one; its
`python3.3` extension is a stable-ABI (abi3) build that Python 3.12 loads, and
it is laid out to match the conda package so nothing downstream can tell the
images apart. Both declare `MDCLAW_MODELLER_VERSION`, so both run the smoke
check.

Heavy AI model backends (BioEmu, Boltz-2) are intentionally **not** baked into
the image. They ship their own Torch/CUDA stacks, so they install into isolated venvs via
`mdclaw setup_model_backend --model <bioemu|boltz>`. See "Model backends" below.

It is not a separate skill distribution. Agent-facing skill text stays in
`skills/`; Docker and Singularity/Apptainer only provide the execution
environment behind `mdclaw <tool>`.

## Build And Test

The standard x86-64 image retains CUDA 11.8 for floyd/n1–n5. Stage 1 starts
from `pixi.toml` + `pixi.lock` (`pixi install --locked`), then removes the
Conda-owned OpenMM, PyTorch/libtorch, JAX GPU build, Triton and CUDA dependency
files **and metadata** before installing `container/cuda118-requirements.txt`.
The removal follows the locked dependency graph and aborts on unexpected
scientific dependents; files also owned by retained packages are preserved.
A pip overwrite alone is insufficient because it leaves Conda libraries behind.

The pinned PyTorch 2.7.1+cu118 wheel supplies its exact `nvidia-*-cu11`
dependencies. JAX uses a pinned CPU jaxlib for pymbar analysis. Stage 2 builds
OpenMM 8.6.1 with CUDA 11.8, then PythonTorchForce and PLUMED against that
OpenMM and Torch's C++ ABI. Runtime NVRTC and math libraries are linked to the
wheel-owned copies; no CUDA 12 Conda stack or second NVRTC is copied in.
The build checks NVRTC 11.8, CPU JAX calculation, absence of the old Conda GPU
records, and PyTorch's Pascal/Turing architecture support without needing a GPU.

Thus the final image has explicit CUDA 11.8 build overrides, while local Linux
Pixi remains CUDA 12.9. MODELLER and other scientific/dev packages still come
from the lock. RIKYU continues using `environment.yml`, NVRTC 13.0, CUDA 13.1
math libraries, its sm_100 plugin, FUSE workaround and existing MODELLER install.

The environment is relocated directly to `/opt/mdclaw` with conda-pack's
`no-archive` format and `--dest-prefix`. This avoids conda-pack 0.9.2's invalid
tar headers for rewritten hardlinked files, as well as intermediate archive
copies. Installation, relocation, and cache cleanup share one Docker layer so
deleted caches do not remain in earlier layers. Relocation uses the installed
files, deduplicates overlapping package paths, and preserves the binary prefix
metadata needed after cache removal. The build runs
`container/scripts/test-env-relocation.py` against the actual Dockerfile block
to check hardlinks, duplicate paths, symlinks, text/binary prefix replacement,
executable permissions, and source-file integrity.

Actions writes the Docker image to a temporary file, releases the BuildKit
cache, requires at least 40 GiB free, and only then loads the image for the CPU
smoke test. This avoids retaining the build snapshots and Docker's expanded
image simultaneously. The registry cache keeps final-image layers only
(`mode=min`), trading intermediate-stage reuse for lower peak disk usage.
Actions records free disk space every 15 seconds and stops its builder if less
than 10 GiB remains.

The amd64 image ships the membrane-patch cache that lives in the package
(`mdclaw/data/membrane_patches`, OPC and TIP3P) and asserts at build time that
both water models are present; it no longer runs a build-time warm-up. The
image built 2026-08-29 had shipped an empty warm-up directory because that step
hid its own failure behind `|| echo WARNING`, so every membrane run without a
checkout overlay paid a 30-40 minute cold build. Regenerate the packaged
patches with `scripts/warmup_membrane_cache.py --out mdclaw/data/membrane_patches`
(defaults to TIP3P; pass `--water-model opc` for the OPC set) and commit them.

```bash
docker build -f container/Dockerfile -t mdclaw:latest .
docker run --rm -v "$(pwd)/container/scripts/test-container.sh:/work/test.sh:ro" \
  mdclaw:latest bash /work/test.sh
docker run --rm --gpus all -v "$(pwd)/container/scripts/test-container.sh:/work/test.sh:ro" \
  mdclaw:latest bash /work/test.sh --gpu
```

CUDA 11.8's conservative driver baseline for OpenMM's runtime-generated PTX
is **520.61.05**, not an unconditional claim that all R450+ drivers work.
Newer CUDA 12/13-capable drivers can run compatible CUDA 11.8 applications.
Do not infer support from the host's installed Toolkit version. Before
replacing the existing shared SIF, run the common GPU test under a scheduler
allocation on **each of n1, n2, n3, n4 and n5**, recording the driver, GPU and
SIF checksum. A successful floyd or build-runner check does not cover the other
nodes. These nodes have not yet been tested with the cleaned image.

`container/scripts/test-gpu.py` is the common real-computation test:

```bash
# Linux Pixi: core GPU tests; reports a missing optional Torch-force plugin.
pixi run python container/scripts/test-gpu.py
# Final SIF: require the plugin as well, with no checkout needed.
apptainer exec --nv mdclaw.sif python /opt/mdclaw/share/mdclaw/test-gpu.py --require-torch-force
# RIKYU: preserve its additional architecture/NVRTC/cuFFT/FUSE checks.
apptainer exec --nv mdclaw-rikyu.sif bash /path/to/container/scripts/test-rikyu-gpu.sh
```

The test requires CUDA, checks analytic forces and energy, integrates, compares
PME energy to OpenMM Reference, checks Torch matrix multiplication and an FFT
round trip, and checks PythonTorchForce's energy/autograd force transfer when
installed (mandatory with `--require-torch-force`). It prints the loaded library
paths. `test-container.sh --gpu` always runs this gate, including when GPU
initialization fails; omitting `--gpu` is only a CPU/build smoke check. The
RIKYU test adds its existing contracts then runs the same common test. Run
both Docker and the final SIF because the latter may load libraries via FUSE
or host `--nv` bindings. These commands are acceptance procedures, not a claim
that GPU execution has been verified on the development Mac.

## Pixi-Built Image (`Dockerfile.pixi`)

`container/Dockerfile.pixi` builds the runtime from the pixi `cuda118`
environment, so the workstation environment and the image come from the same
`pixi.toml` / `pixi.lock`. OpenMM 8.6.1 (CUDA 11.8), openmm-torch with
PythonTorchForce and PLUMED 2.9.4 + openmm-plumed come from `recipes/`;
PyTorch 2.5.1 is conda-forge's CUDA 11.8 GPU build. Only the packmol-memgen
fixes (v3numpy patch, PPM3 `immers` rebuild) remain outside pixi.

```bash
docker build -f container/Dockerfile.pixi -t mdclaw:pixi-cuda118 .
singularity build mdclaw.sif docker-daemon://mdclaw:pixi-cuda118
```

The build stage runs `pixi install -e cuda118 --locked` with
`CONDA_OVERRIDE_CUDA=11.8` (no GPU on the builder) and builds the recipes once,
about 30 minutes. The environment stays at `/opt/mdclaw-src/.pixi/envs/cuda118`
(it is not relocatable) and `/opt/mdclaw` links to it, so paths match the
standard image. The runtime stage fixes the environment's activation as `ENV`
(`AMBERHOME`, `MKL_INTERFACE_LAYER`, `OCL_ICD_VENDORS`, `PYTHONNOUSERSITE`,
...), because `singularity exec` does not run an activation script. Driver
floor R520, as for the standard image.

## Publish To GHCR

Publishing is normally automated: a `vX.Y.Z` tag triggers
`.github/workflows/container.yml`, which builds from the lock, runs the CPU
smoke test, and pushes the versioned and `latest` tags (GPU smoke stays a
manual gate, `docs/developer/release.md`). The commands below are the manual
fallback when Actions is unavailable.

```bash
gh auth refresh --hostname github.com --scopes write:packages
gh auth token | docker login ghcr.io -u <github-username> --password-stdin

docker tag mdclaw:latest ghcr.io/matsunagalab/mdclaw:latest
docker push ghcr.io/matsunagalab/mdclaw:latest
```

The GHCR package must be public for unauthenticated Singularity pulls.

## Arm64 / CUDA 13 Development Image

The arm64/CUDA 13 development image is kept separate from the generic
`linux/amd64` / CUDA 11.8 image. `container/scripts/build-rikyu-arm64.sh` covers
the whole flow — build, non-GPU verification, `--push` to GHCR, and `--sif PATH`
to convert and install the SIF (verified first, previous file kept as
`PATH.bak`). It takes docker or podman, whichever works. The underlying commands
are:

```bash
revision=$(git rev-parse --short=12 HEAD)
image="ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-${revision}"

docker build --platform linux/arm64 \
  --build-arg GIT_REVISION="$(git rev-parse HEAD)" \
  --build-arg BUILD_JOBS=4 \
  -f container/Dockerfile.rikyu-arm64 \
  -t "$image" .
docker push "$image"
```

### Where To Build It

Any arm64 Linux host with Docker; nothing in the image is specific to rikyu.
The build needs **no GPU** — `CONDA_OVERRIDE_CUDA` covers the CUDA solve, and
OpenMM compiles its kernels at Context creation rather than at build time. A GPU
is needed only for `test-rikyu-gpu.sh`, which must run from the SIF.

Building on an x86_64 host is possible but rarely worth it: it needs qemu binfmt
registered as root (`docker run --privileged tonistiigi/binfmt --install arm64`)
and then emulates the entire OpenMM and `openmm-torch` compile. Measured
qemu-user overhead on this codebase is ~8.7x (a MODELLER comparative model:
13.5 s native, 116.6 s emulated), so a build that takes ~20 minutes natively
runs for hours. The build script refuses to run on a non-arm64 host for this
reason.

A login node is fine as long as it matches the compute nodes and can run a
container engine. Two things commonly bite there: `docker` may need group
membership (the script falls back to rootless podman), and `/tmp` is often too
small — the image is ~15 GB and the SIF conversion unpacks it again, so point
`TMPDIR`, `APPTAINER_TMPDIR` and `APPTAINER_CACHEDIR` at a filesystem with ~60 GB
free. The script warns when the scratch filesystem looks too small.

The Dockerfile uses the arm64 manifests of CUDA 13.0.2 on Ubuntu 24.04, builds
OpenMM 8.5.1 against CUDA 13.0, and compiles `openmm-torch` for compute
capability 10.0 (`sm_100`). PyTorch is the CUDA 13.0 arm64 conda-forge build.
Keep the OpenMM compiler and NVRTC toolkit at or below the maximum CUDA version
supported by the host driver: OpenMM compiles kernels at Context creation, and
a driver can reject PTX emitted by a newer minor toolkit with
`CUDA_ERROR_UNSUPPORTED_PTX_VERSION`.

The dynamically loaded conda CUDA math libraries are resolved from the CUDA
13.1 family, with `libcufft>=12.1.0.78`. This split keeps NVRTC at 13.0 for PTX
compatibility while retaining a consistent CUDA math-library stack. Build and
smoke tests assert both version contracts. The packed runtime is copied into
several OCI layers so no single application layer must carry the complete
multi-gigabyte conda environment during an Apptainer pull.

Some rootless Apptainer installations mount SIF files through FUSE. On affected
driver/runtime combinations, OpenMM PME and `torch.fft` can fail when CUDA
faults in a page from the FUSE-backed cuFFT mapping even though the library is
intact. The arm64 image therefore preloads `libmdclaw_fusefix.so`, a small shim
that applies `MADV_POPULATE_READ` to cuFFT's private writable mappings after
they are loaded. It leaves the pages file-backed and shared. The generic
container smoke verifies that the shim is present and loaded; the GPU smoke
must be run directly from the SIF and exercises both OpenMM PME and
`torch.fft`. An unpacked directory is not an equivalent acceptance test for
this specific failure mode.

### One Smoke Test, Two Images

`container/scripts/test-container.sh` runs against both images, so a contract
that only one image makes is gated on the environment variable that declares
it. `check_declared <VAR> <description> <command>` skips when `<VAR>` is unset
and reports `SKIP` rather than `FAIL`:

| variable | declared by | gates |
| --- | --- | --- |
| `MDCLAW_CUDA_TOOLKIT_VERSION` | standard (`11.8`), arm64 (`13.0`) | exact NVRTC version; the check still runs everywhere and only asserts a version when the variable is set |
| `MDCLAW_CUFFT_MIN_VERSION` | arm64 image (`12.1.0.78`) | cuFFT floor and API level |
| `MDCLAW_FUSEFIX_LIB` | arm64 image | the FUSE preload shim being present and mapped |
| `MDCLAW_PPM3_PATCHED` | both images | `immers` being the rebuilt binary, not the one the conda package ships |
| `PYTHONUTF8` | both images | interpreter UTF-8 mode, and text I/O staying UTF-8 after a driver resets the locale |

The amd64 / CUDA 11.8 image declares NVRTC 11.8 and uses cuFFT from the
pinned cu11 wheel set.
It does not declare the RIKYU cuFFT floor or FUSE shim, so those checks skip. Adding a contract
that belongs to one image means declaring a variable in that image's Dockerfile
and gating the check on it — not forking the script. `MDCLAW_FUSEFIX_LIB` is
also the single definition of the shim path: the runtime assertions in the
Dockerfile and in `test-rikyu-gpu.sh` read it instead of repeating the literal.

Do not publish this image under `ghcr.io/matsunagalab/mdclaw:latest`. After
push, record the registry digest and test the artifact pulled by digest rather
than only the local Docker image:

```bash
apptainer pull mdclaw-rikyu-arm64-cuda13-dev.sif \
  "docker://ghcr.io/matsunagalab/mdclaw-rikyu@sha256:<digest>"
apptainer exec --nv mdclaw-rikyu-arm64-cuda13-dev.sif \
  bash /path/to/container/scripts/test-container.sh
apptainer exec --nv mdclaw-rikyu-arm64-cuda13-dev.sif \
  bash /path/to/container/scripts/test-rikyu-gpu.sh
```

The MDClaw package already contains the representative bundled membrane-patch
cache. The development build skips revalidating or regenerating that cache by
default. Pass `--build-arg WARM_MEMBRANE_CACHE=1` to make cache validation and
regeneration a build-time gate for a release candidate.

Some institutional network paths interrupt long single-blob OCI downloads. If
an Apptainer pull repeatedly fails on the same large layer, do not loop the
same transfer indefinitely. Convert the already-verified Docker image to SIF
on a trusted arm64 Linux system, then transfer the SIF with a resumable tool
and verify its checksum:

```bash
docker save -o mdclaw-arm64.tar "$image"
singularity build mdclaw-arm64.sif docker-archive:mdclaw-arm64.tar
sha256sum mdclaw-arm64.sif

rsync --partial --append --progress mdclaw-arm64.sif user@host:
ssh user@host sha256sum mdclaw-arm64.sif
```

The two SHA-256 values must match before running the transferred SIF.

## Singularity

The Docker image published to GHCR is also the source for the HPC SIF:

```bash
singularity pull mdclaw.sif docker://ghcr.io/matsunagalab/mdclaw:latest
singularity exec --nv mdclaw.sif mdclaw --list
singularity exec --nv \
  --bind "$(pwd)/container/scripts/test-container.sh:/work/test.sh" \
  mdclaw.sif bash /work/test.sh
```


`singularity pull` unpacks the whole ~15 GB image before writing the SIF, and it
does that under `/tmp`. On a host whose root filesystem is small or full the
pull dies mid-layer with `no space left on device`. Point both temp locations at
a filesystem with room to spare rather than clearing space under `/`:

```bash
export SINGULARITY_TMPDIR=/path/with/room/tmp
export SINGULARITY_CACHEDIR=/path/with/room/cache
singularity pull mdclaw.sif docker://ghcr.io/matsunagalab/mdclaw:latest
```

### Singularity Inside A User Namespace

Do not wrap `singularity` in `unshare -Ur`, `unshare -U`, or any other user
namespace. Singularity mounts a SIF through its setuid starter, and the kernel
ignores the setuid bit whenever the file's owner is unmapped in the current user
namespace. Both `starter-suid` and `fusermount3` therefore become unprivileged,
the squashfuse mount fails with `fusermount3: mount failed: Operation not
permitted`, and Singularity falls back to extracting the entire SIF into a
temporary sandbox on *every* invocation. Measured on floyd with a 5.1 GB SIF:

| invocation | elapsed |
| --- | --- |
| `singularity exec mdclaw.sif …` | 0.80 s |
| `singularity exec --no-home --bind "$PWD:/work" --pwd /work …` | 0.36 s |
| `unshare -Ur singularity exec …` | 65.7 s, plus 5.1 GB of scratch churn |

This is host-dependent. Apptainer 1.4.5 on RIKYU runs the SIF unprivileged
through squashfuse inside a user namespace (the `fuseapps` image driver, no
temporary sandbox), measured 2026-09-26 with the 7.4 GB shared image:

| invocation (RIKYU login node) | `mdclaw --version` |
| --- | --- |
| `apptainer exec …` (setuid) | 4.2 s |
| `apptainer exec --userns …` | 3.7 s |
| `setpriv --no-new-privs apptainer exec …` | 3.7 s |
| `unshare --user --map-current-user --mount apptainer exec …` | 3.8 s |

`unshare -r apptainer exec … /bin/true` took 136 ms, and MDDataBench's agent
sandbox (user, mount and PID namespaces) depends on this path. Before relying on
it elsewhere, check that `apptainer -d exec` reports the `fuseapps` driver
rather than a conversion to a temporary sandbox.

The trap is that a `unknown userid` / `Could not lookup the current user's
information: user: lookup userid <uid>: bad address` warning invites exactly this
workaround. On hosts whose accounts come from NIS or LDAP rather than
`/etc/passwd`, that message is a warning, not a failure. The fix is `--no-home`
plus a neutral bind path, which avoids resolving the account's home directory
while keeping the privileged mount path:

```bash
singularity exec --no-home --bind "$PWD:/work" --pwd /work \
  mdclaw.sif python -m mdclaw._cli --list
```

With `--no-home` the account's real home is still what `$HOME` points at, and
it is read-only inside the container, so anything that writes a cache there
(openmmforcefields' template cache during `build_amber_system`, ruff, JAX) fails
with `Read-only file system: '/home/<user>/.cache'`. Pass a writable cache and
home for tool runs, not only for lint:

```bash
singularity exec --no-home --bind "$PWD:/work" --pwd /work \
  --env XDG_CACHE_HOME=/tmp/mdclaw-cache --env HOME=/tmp/mdclaw-home \
  mdclaw.sif python -m mdclaw._cli <tool> ...
```

If that still fails outright, set `SINGULARITY_HOME` explicitly. A user namespace
is not the answer. `bin/mdclaw` warns when it is about to launch Singularity from
inside one. Its `MDCLAW_UID_MAP_FILE` environment variable exists only as a test
seam for that warning; it is not a supported configuration knob.

## Runtime Notes

### HOLE pore analysis

The runtime includes the `mdahole2` Python interface (0.5 series) and conda-forge
`hole2` 2.3.1, including `hole`, `sph_process`, `sos_triangle` and the radius
tables. Both container architectures inherit `hole2` from `environment.yml`;
`pyproject.toml` installs the Python interface in their pip stage. Installing
MDClaw with pip alone cannot install the Fortran executables: install `hole2`
from conda-forge as well. See the upstream
[installation](https://www.mdanalysis.org/mdahole2/getting_started.html) and
[usage](https://www.mdanalysis.org/mdahole2/usage.html) guides.

Use `from mdahole2.analysis import HoleAnalysis` rather than the deprecated
`MDAnalysis.analysis.hole2` import. For an already prepared, consistently
imaged trajectory, the interface is:

```python
import MDAnalysis as mda
from mdahole2.analysis import HoleAnalysis

u = mda.Universe("topology.pdb", "trajectory.dcd")
# Choose the pore-lining selection, starting point and direction for the
# structure being analyzed; do not reuse an arbitrary fixed axis across systems.
with HoleAnalysis(u, select=pore_selection, cpoint=pore_start,
                  cvect=pore_direction) as analysis:
    analysis.run(step=10, random_seed=31415)
    profiles = analysis.results.profiles
    analysis.create_vmd_surface("pore.vmd")
```

Coordinates and returned radii use Å. Check periodic imaging, alignment and
the resulting pathway before interpreting a protein cavity as a conducting
pore. HOLE optimizes sphere positions from the supplied starting geometry;
an executable success is not evidence of channel function.

`container/scripts/test-container.sh` verifies actual two-frame pore analysis
on synthetic cylindrical walls with expected central radii 3.15 and 1.15 Å,
then generates a VMD surface through both helper executables. It requires no
network or user data and fails if executables are missing. Run it on the final
SIF as well as the container build; import-only tests are insufficient.

### General runtime components

- Docker image size is roughly 11.4 GB; SIF size is roughly 4.6 GB.
- The former CUDA 11.8 image was tested with driver 520. The current CUDA
  11.8 cleanup still requires a new GPU acceptance run on each target node.
- PPM3 (`immers`), the membrane orientation code bundled with packmol-memgen,
  is rebuilt from patched source at build time, in both images. The stock
  binary computes the orientation and then dies printing it, because `opm.f`
  has a FORMAT descriptor missing a comma that current gfortran rejects at
  runtime; the build fails loudly rather than shipping a binary that can never
  produce a result. The aarch64 conda package carries the same bug, so the
  arm64 image installs `gfortran` in its builder stage for this and overwrites
  `/opt/mdclaw/bin/immers`. The rebuilt binary is identified by its compiled
  FORMAT string: running `immers` with no input dies at the first read, long
  before the bad descriptor is reached, so the two builds cannot be told apart
  by exit status.
- `PYTHONUTF8=1` is set in both images. A GPU platform driver can reset the
  process locale to C while it initialises — Apple's OpenCL does — and with
  `LANG=C.UTF-8` that makes every read and write without an explicit encoding
  ASCII, so a report containing an em dash fails to write. `preserve_locale`
  covers the Contexts MDClaw creates; UTF-8 mode covers everything else by
  taking the locale out of the decision.
- The lock supplies the seed environment. The standard image replaces its
  GPU stack completely with source-built OpenMM and pinned cu118 Torch/cu11
  libraries; CPU JAX serves analysis. Stage 3 uses a CUDA 11.8 base image.
- `openmm-torch` is built in Stage 2 against the matching OpenMM/Torch ABI, pinned
  to `OPENMM_TORCH_COMMIT` for PythonTorchForce. PLUMED is built against that
  same OpenMM. Updating the lock or plugin pin requires rebuilding the image
  and rerunning CPU and GPU acceptance before replacing a shared SIF.
- The `dev` extra (`ruff`, `pytest`, and `pytest-asyncio`) is installed in the
  conda-packed `/opt/mdclaw` environment so Singularity/Apptainer SIF workflows
  can run repository lint and tests without a separate host conda environment.

## SST2 (solute tempering)

`run_sst2` runs the SST2 package as a separate process. Every runtime installs
the matsunagalab/SST2 fork (branch `mdclaw`), pinned to a commit. The pin is
declared in **six** places, a bump changes all six, and
`test_sst2_pin_is_the_same_in_every_declaration` in
`tests/test_deployment_scripts.py` fails when they disagree:

| file | what |
| --- | --- |
| `pixi.toml` | `sst2` git `rev` (the standard image installs it from the lock) |
| `environment.yml` | pip section |
| `container/Dockerfile` | `ENV MDCLAW_SST2_REVISION` |
| `container/Dockerfile.pixi` | `MDCLAW_SST2_REVISION` (the image installs it from the lock) |
| `container/Dockerfile.rikyu-arm64` | the `pip install` line, and `MDCLAW_SST2_REVISION` |

Since `3dc7013` the fork lists `openmm` only as the `SST2[openmm]` extra: a
hard pip requirement made uv add a PyPI OpenMM wheel on top of the source-built
OpenMM of the pixi `cuda118` environment, which pixi cannot map to PyPI.

The install pulls `pdb_numpy` from PyPI. "Bump the three places" used to be the
wording here and it undercounted the two `MDCLAW_SST2_REVISION` declarations;
the pin drifted once because of it — mdclaw gained
`run_sst2 --charge-unscaled-indices-file` while all five sites stayed at
`5590f4f`, so the option reached a driver that had never heard of it and died
with argparse's exit 2. `run_sst2` now reads the installed driver's source and
refuses with `sst2_charge_unscaled_unsupported` when it predates the option, so
a stale pin is legible rather than cryptic — but the refusal is a diagnosis, not
a substitute for the bump.

Bumping the pin does **not** rebuild any image. The amd64 image and the shared
RIKYU arm64 image are rebuilt and switched separately, each on its own
approval, so a repository whose pin reads `ba48461` can still be facing an
image that carries `5590f4f`. Check `MDCLAW_SST2_REVISION` inside the image
rather than inferring the revision from the checkout.

SST2 is GPL-2.0 and MDClaw is MIT. Shipping both in one image is aggregation,
not combination: MDClaw never imports SST2, it execs `python -m SST2.driver`.
The package's `LICENSE` stays in its dist-info under site-packages; keep it
there. Bumping the SST2 pin changes container contents, so it needs an image
rebuild; a checkout on `MDCLAW_SST2_HOME` overrides the packaged copy for
development without a rebuild.

## Model Backends (BioEmu, Boltz-2)

BioEmu and Boltz-2 are not part of the image. They are installed on first use
into isolated venvs, keeping their independent Torch/CUDA stacks out of the
source-built OpenMM runtime:

```bash
mdclaw setup_model_backend --model bioemu --device cuda
mdclaw setup_model_backend --model boltz  --device cuda
mdclaw check_model_backend  --model bioemu
mdclaw check_model_backend  --model boltz
```

Runtime install rules for containers:

- A SIF is read-only, so the venv cannot be written under `/opt/mdclaw`. Point
  `MDCLAW_SURROGATE_DIR` at a writable, ideally shared, filesystem and
  bind-mount it. Weight caches (BioEmu / ColabFold / Boltz) should live on the
  same shared filesystem so they are downloaded once.

```bash
export MDCLAW_SURROGATE_DIR=/shared/fs/mdclaw-model-backends
singularity exec --nv \
  --bind "$MDCLAW_SURROGATE_DIR:$MDCLAW_SURROGATE_DIR" \
  mdclaw.sif mdclaw setup_model_backend --model boltz --device cuda
```

- These venvs pull their own Torch and have CUDA/driver requirements
  independent of the main OpenMM environment. Verify each with
  `mdclaw check_model_backend --model <name>`.
- Boltz is pinned to `BOLTZ_VERSION` in `mdclaw/surrogate/_base.py`; bump
  deliberately.

## Binds made by the launcher

`bin/mdclaw` binds the working directory and the package root into the
container, and (since 2026-09-21) the deepest existing directory of every
absolute path found in the arguments — a `--job-dir` outside the working
directory, a `--study-dir`, a reference structure inside a JSON argument —
minus `$HOME` (bound by default) and system prefixes, reduced to a minimal
set (`_mdclaw_arg_bind_dirs`). Before that, a job directory outside the
working directory was invisible inside the container and the tools reported
an existing job as "no progress.json; bootstrap the job first"; the `rounds`
tools now answer `rounds_job_dir_unreachable` when a job directory does not
exist from the process, instead of suggesting a bootstrap.
