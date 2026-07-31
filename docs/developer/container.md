# Container Runtime Build And Distribution

The container is MDClaw's packaged scientific runtime. It contains the `mdclaw`
CLI plus CUDA runtime, PyTorch, AmberTools, OpenMM, PyMOL
(`pymol-open-source`, for headless structure previews), MDTraj, and MDAnalysis.

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
`linux/amd64` / CUDA 11.8 image:

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

The Dockerfile uses the arm64 manifests of CUDA 13.0.2 on Ubuntu 24.04, builds
OpenMM 8.5.1 against CUDA 13.0, and compiles `openmm-torch` for compute
capability 10.0 (`sm_100`). PyTorch is also the CUDA 13.0 arm64 conda-forge
build. Keep the OpenMM NVRTC toolkit at or below the maximum CUDA version
supported by the host driver: OpenMM compiles kernels at Context creation, and
a driver can reject PTX emitted by a newer minor toolkit with
`CUDA_ERROR_UNSUPPORTED_PTX_VERSION`. The build and smoke tests therefore
assert that the bundled NVRTC runtime is 13.0. The packed runtime is copied
into several OCI layers so no single application layer must carry the complete
multi-gigabyte conda environment during an Apptainer pull.

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

## Runtime Notes

- Docker image size is roughly 11.4 GB; SIF size is roughly 4.6 GB.
- Minimum actively verified NVIDIA driver is 520.
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
- `setup_surrogate_backend` / `check_surrogate_backend` remain as
  `bioemu`-defaulted aliases.
