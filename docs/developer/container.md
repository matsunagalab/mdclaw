# Container Build And Distribution

The production image contains CUDA runtime, PyTorch, AmberTools, OpenMM,
Boltz-2, PyMOL (`pymol-open-source`, for headless structure previews), and the
`mdclaw` CLI.

## Build And Test

```bash
docker build -f container/Dockerfile -t mdclaw:latest .
docker run --rm mdclaw:latest bash container/scripts/test-container.sh
docker run --rm --gpus all mdclaw:latest bash container/scripts/test-container.sh
```

## Publish To GHCR

```bash
gh auth refresh --hostname github.com --scopes write:packages
gh auth token | docker login ghcr.io -u <github-username> --password-stdin

docker tag mdclaw:latest ghcr.io/matsunagalab/mdclaw:latest
docker push ghcr.io/matsunagalab/mdclaw:latest
```

The GHCR package must be public for unauthenticated Singularity pulls.

## Singularity

```bash
singularity pull mdclaw.sif docker://ghcr.io/matsunagalab/mdclaw:latest
singularity exec --nv mdclaw.sif mdclaw --list
singularity exec --nv mdclaw.sif bash container/scripts/test-container.sh
```

## Runtime Notes

- Docker image size is roughly 11.4 GB; SIF size is roughly 4.6 GB.
- Minimum actively verified NVIDIA driver is 520.
- The image ships CUDA 11.8 to cover mixed HPC clusters with older drivers.
- OpenMM 8.2.0 is source-built against CUDA 11.8 so NVRTC-generated PTX matches
  the driver floor.
- NVRTC and nvrtc-builtins are copied into `/opt/mdclaw/lib/` so the slim
  runtime image can JIT without using a devel base image.
