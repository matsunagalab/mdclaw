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
channel publishes **linux-64 only**. The amd64 image installs the conda package
from `container/Dockerfile` — not from `environment.yml`, which
`Dockerfile.rikyu-arm64` shares and which would then fail to solve on arm64.
The arm64 image takes MODELLER from the generic tarball instead, which ships an
aarch64 build (`lib/armv8-gnu`, gfortran-linked) beside the x86_64 one; its
`python3.3` extension is a stable-ABI (abi3) build that Python 3.12 loads, and
it is laid out to match the conda package so nothing downstream can tell the
images apart. Both declare `MDCLAW_MODELLER_VERSION`, so both run the smoke
check.

Heavy AI model backends (BioEmu, Boltz-2) are intentionally **not** baked into
the image. They ship their own Torch/CUDA stacks that conflict with the OpenMM
`cu118` pin, so they install into isolated venvs at runtime via
`mdclaw setup_model_backend --model <bioemu|boltz>`. See "Model backends" below.

It is not a separate skill distribution. Agent-facing skill text stays in
`skills/`; Docker and Singularity/Apptainer only provide the execution
environment behind `mdclaw <tool>`.

## Build And Test

```bash
docker build -f container/Dockerfile -t mdclaw:latest .
docker build -f container/Dockerfile --build-arg BIOEMU_DEVICE=cuda -t mdclaw:latest .
docker run --rm -v "$(pwd)/container/scripts/test-container.sh:/work/test.sh:ro" \
  mdclaw:latest bash /work/test.sh
docker run --rm --gpus all -v "$(pwd)/container/scripts/test-container.sh:/work/test.sh:ro" \
  mdclaw:latest bash /work/test.sh
```

## Publish To GHCR

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
| `MDCLAW_CUDA_TOOLKIT_VERSION` | arm64 image (`13.0`) | exact NVRTC version; the check still runs everywhere and only asserts a version when the variable is set |
| `MDCLAW_CUFFT_MIN_VERSION` | arm64 image (`12.1.0.78`) | cuFFT floor and API level |
| `MDCLAW_FUSEFIX_LIB` | arm64 image | the FUSE preload shim being present and mapped |
| `MDCLAW_PPM3_PATCHED` | both images | `immers` being the rebuilt binary, not the one the conda package ships |

The amd64 / CUDA 11.8 image declares none of the last two, ships cuFFT from the
CUDA 11.8 family, and has no shim, so both checks skip there. Adding a contract
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

### Never Run Singularity Inside A User Namespace

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

If that still fails outright, set `SINGULARITY_HOME` explicitly. A user namespace
is not the answer. `bin/mdclaw` warns when it is about to launch Singularity from
inside one.

## Runtime Notes

- Docker image size is roughly 11.4 GB; SIF size is roughly 4.6 GB.
- Minimum actively verified NVIDIA driver is 520.
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
- The image ships CUDA 11.8 to cover mixed HPC clusters with older drivers.
- OpenMM 8.5.1 is source-built against CUDA 11.8 so NVRTC-generated PTX matches
  the driver floor. 8.5.1 is the floor required by openmmforcefields >= 0.16
  (uses `openmm.app.topology.MergedResidue`, added in 8.5).
- NVRTC and nvrtc-builtins are copied into `/opt/mdclaw/lib/` so the slim
  runtime image can JIT without using a devel base image.
- `openmm-torch` (the production custom force / CV bias plugin backend, used
  via `PythonTorchForce`) is source-built in Stage 2 against the source OpenMM
  and the cu118 PyTorch, and is stripped from the conda env file because the
  conda build links the conda OpenMM ABI. It is pinned to `OPENMM_TORCH_COMMIT`
  (the master commit that added `PythonTorchForce`, #179) because no tagged
  release ships it yet. Bumping `openmm`, `pytorch`, or `OPENMM_TORCH_COMMIT`
  requires a container rebuild + push (container contents changed).
- The `dev` extra (`ruff`, `pytest`, and `pytest-asyncio`) is installed in the
  conda-packed `/opt/mdclaw` environment so Singularity/Apptainer SIF workflows
  can run repository lint and tests without a separate host conda environment.

## Model Backends (BioEmu, Boltz-2)

BioEmu and Boltz-2 are not part of the image. They are installed on first use
into isolated venvs, keeping their independent Torch/CUDA stacks out of the
OpenMM `cu118` runtime:

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
  independent of the OpenMM `cu118` build. Verify each with
  `mdclaw check_model_backend --model <name>`.
- Boltz is pinned to `surrogate_server.BOLTZ_VERSION`; bump deliberately.
