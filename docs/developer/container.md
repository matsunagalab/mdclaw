# Container Runtime Build And Distribution

The container is MDClaw's packaged scientific runtime. It contains the `mdclaw`
CLI plus CUDA runtime, PyTorch, AmberTools, OpenMM, Boltz-2, PyMOL
(`pymol-open-source`, for headless structure previews), and an isolated BioEmu
surrogate backend venv.

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
- `openmm-torch` (the production custom force / CV bias plugin backend) is
  source-built in Stage 2 against the source OpenMM and the cu118 PyTorch, and
  is stripped from the conda env file because the conda build links the conda
  OpenMM ABI. Bumping `openmm`, `pytorch`, or `OPENMM_TORCH_VERSION` requires a
  container rebuild + push (container contents changed).
- BioEmu is installed into `/opt/mdclaw/surrogates/bioemu/venv`, not into the
  conda-packed `/opt/mdclaw` environment. Use `BIOEMU_DEVICE=cuda` at build time
  to install `bioemu[cuda]`; the default installs CPU BioEmu for import and
  metadata checks.
- `ruff` is installed in the conda-packed `/opt/mdclaw` environment so
  Singularity/Apptainer SIF workflows can run repository lint checks without a
  separate host conda environment.
