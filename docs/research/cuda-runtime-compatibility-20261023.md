---
title: Current state of CUDA 12 / 13 compatible environments and proposed responses
category: research
updated: 2026-10-23
status: Record as of the investigation; implementation revised on 2026-10-24; not yet verified on real GPU hardware
---

# Current state of CUDA 12 / 13 compatible environments and proposed responses

The target is `7ac36a7`. Rather than making the environment follow the host's CUDA Toolkit, pin the runtime environment to the oldest driver and GPU generation that must be supported. Newer drivers can use an older CUDA runtime, so the CUDA 12 build can also be used on CUDA 13 capable hosts. However, OpenMM's runtime compilation and each library's GPU-generation support must be checked. [NVIDIA compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)

Addendum 2026-10-24: with the owner's approval, we implemented unifying the standard Docker image on CUDA 12.9, adding an ARM Pixi lock and the official ARM build of MODELLER, and sharing the real GPU computation checks. The RIKYU configuration is kept. What follows is the record as of the investigation; for the current changes and the scope of verification, see the [development record](../memo.md) and the [deployment guide](../agents/deployment.md).

Additional instruction the same day: prioritize compatibility with n1–n5, and withdraw moving the standard container to 12.9. Remove the GPU dependencies wholesale and replace them with cu118 Torch, CPU JAX, and OpenMM built from source against 11.8. Keep Linux Pixi's 12.9 and RIKYU's 13.x as separate configurations. Passing on real hardware has not been confirmed.

## Current state

| Path | Actual definition / pinned contents | Problems / constraints |
|---|---|---|
| Linux x86_64 pixi | OpenMM 8.6.1, PyTorch 2.7.1 cuda129, NVRTC 12.9.86 | A single CUDA 12.9 environment. Cannot guarantee all older CUDA 12 drivers |
| Standard Docker / its SIF | After installing the above lock, Torch is replaced with 2.7.1+cu118, and OpenMM 8.5.1 is built from source against CUDA 11.8 | There is no step that removes conda's libtorch and CUDA 12.9 dependencies, so they remain. The final load targets and ABI consistency must be verified on the actual artifact |
| RIKYU arm64 Docker / its SIF | environment.yml is rewritten and solved with mamba. OpenMM 8.5.1, NVRTC 13.0, Torch 2.10.0 cuda130, CUDA math libraries 13.1, cuFFT 12.1 or later | Does not use pixi.lock. openmm-torch targets sm_100. Not a configuration that targets all ARM GPUs in general |
| Linux aarch64 pixi | Declared in the workspace | The modeller feature included in default excludes this platform, so the default environment / lock has no ARM packages |
| macOS arm64 pixi | OpenMM 8.6.1, CPU Torch | Cannot serve as a verification environment for NVIDIA CUDA |

Evidence: [pixi.toml](../../pixi.toml), [pixi.lock](../../pixi.lock), [standard Dockerfile](../../container/Dockerfile), [RIKYU Dockerfile](../../container/Dockerfile.rikyu-arm64). `pixi 0.82.1 info --json` confirmed that default targets only linux-64-cuda / osx-arm64. `pixi list --locked --no-install --platform linux-aarch64-cuda '^openmm$'` returns "No packages found". For Linux x86_64, the same command confirmed the pinned versions in the table above. Nothing was installed and the lock was not updated.

## What determines compatibility

- Check the host's NVIDIA driver, the GPU generation, and the CUDA runtime / NVRTC / math libraries inside the environment separately. Docker does not need the host's CUDA Toolkit itself. [NVIDIA Container Toolkit](https://github.com/NVIDIA/nvidia-container-toolkit/blob/main/README.md?plain=1)
- Minor-version compatibility covers CUDA 12.x from R525 and 13.x from R580, but it does not apply when an older driver cannot read PTX, the intermediate GPU code. OpenMM [8.5.1](https://raw.githubusercontent.com/openmm/openmm/8.5.1/platforms/cuda/src/CudaContext.cpp) and [8.6.1](https://raw.githubusercontent.com/openmm/openmm/8.6.1/platforms/cuda/src/CudaContext.cpp) generate PTX with NVRTC and load it through the driver. [NVIDIA's limitations](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)
- The conservative baseline for NVRTC 12.9 is the R575 class (12.9 GA is 575.51.03, Update 1 is 575.57.08). This does not mean it is required for every CUDA 12.9 program. Likewise, the standard Dockerfile's "450+ for CUDA 11.8" overstates what OpenMM unconditionally supports. Take driver 520.61.05 or later, which supports 11.8, as the conservative baseline; older configurations need individual verification. [Per-version support](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-toolkit-release-notes/index.html), [11.8](https://docs.nvidia.com/cuda/archive/11.8.0/cuda-toolkit-release-notes/index.html)
- Blackwell has native support from CUDA 12.8, so CUDA 13 is not universally required. CUDA 13 removes compilation / library support for Maxwell, Pascal, and Volta, so unifying all GPUs on 13 is not appropriate. [Blackwell](https://docs.nvidia.com/cuda/archive/12.8.0/blackwell-compatibility-guide/index.html), [CUDA 13](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-toolkit-release-notes/index.html)
- Pixi's `cuda` declaration and `CONDA_OVERRIDE_CUDA` declare / override a compatibility capability; they do not update the driver. The runtime is pinned by dependency specifications and the lock. [Pixi](https://pixi.prefix.dev/latest/workspace/system_requirements/)

## Proposed improvements, in priority order

1. **Decide the minimum driver and GPU generations first.** The current 12.9 can be the standard only if the target is the set of drivers that can run that NVRTC. If R535 / R550 etc. are also included, either a source build against a lower NVRTC or keeping the existing 11.8 build is needed. Do not switch to a 13 build merely because the drivers support CUDA 13.
2. **Solve each configuration as a consistent set of dependencies from the start.** Distinguish the normal CUDA 12 build, the existing RIKYU CUDA 13 build where needed, and, if needed, an 11.8 build for older environments. Do not force mutually conflicting CUDA versions into the same solve-group; lock each Pixi environment separately. Also separate the MODELLER distribution path for ARM. Build Docker images from the corresponding lock, and either eliminate the after-the-fact OpenMM / Torch replacement or make the source-build layer explicit and use the same version. How to supply packages for OpenMM builds older than 12.9 is to be decided at implementation time.
3. **Share the real computation checks.** doctor already tries Context creation, energy, and one step, but it succeeds if the CPU works even when the GPU is unusable. In GPU-required acceptance checks, specify CUDA explicitly and do not let a CPU success mask a failure. Check OpenMM forces / PME, Torch GPU operations / FFT, and PythonTorchForce forces / gradients. Run them in both Docker and the SIF.
4. **Check which libraries are actually loaded.** Record the driver, GPU name / compute capability, OpenMM / Torch / NVRTC versions, and the actual load paths of libcuda, NVRTC, cuFFT, and libtorch. SIF `--nv` pulls in host libraries, so the host configuration also matters. `--nvccli` is experimental and should not be used as a blanket replacement. [Apptainer](https://apptainer.org/docs/user/latest/gpu.html)

The existing [standard smoke](../../container/scripts/test-container.sh) SKIPs when there is no GPU, and even with a GPU it only goes as far as checking OpenMM's CUDA registration. The [RIKYU check](../../container/scripts/test-rikyu-gpu.sh) goes as far as actual OpenMM computation, PME, and Torch FFT, but PythonTorchForce is only import-checked. [doctor](../../scripts/_internal/doctor.py) and [platform inspection](../../mdclaw/simulation/platform.py) have already been improved to checks stronger than registration checks. [CI](../../.github/workflows/ci.yml), [Container](../../.github/workflows/container.yml), and [SIF download](../../.github/workflows/sif.yml) do not guarantee GPU execution.

Additional launch-side issues: [runtime.sh](../../scripts/_internal/runtime.sh) only adds GPU options based on the presence of nvidia-smi; it does not select an image / Pixi environment that matches the CUDA version. The compat decision in [entrypoint.sh](../../container/scripts/entrypoint.sh) is likewise based on whether nvidia-smi exists, not on the driver version. Limit automatic injection of compatibility packages to exceptions whose GPU and driver support has been confirmed. [NVIDIA forward compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/forward-compatibility.html)

## Not yet confirmed

The investigation machine was macOS arm64. The current driver / GPU inventory of the target compute servers, the dynamic link targets of the final images, and GPU computation under each driver were not confirmed this time. Following the existing instruction concerning the university power outage, no SSH connection was made. The contents of the published SIF are not assumed to be identical to the current checkout's Dockerfile. What the next implementation decision needs is a list of the drivers, GPUs, and CPU architectures of the hosts to be supported.
