---
title: CUDA 12・13対応環境の現状と対応案
category: research
updated: 2026-10-23
status: 調査時点の記録・2026-10-24に実装修正・GPU実機未検証
---

# CUDA 12・13対応環境の現状と対応案

対象は `7ac36a7`。ホストのCUDA Toolkitに環境を追従させるより、必要な最古ドライバとGPU世代に合わせて実行環境を固定する。新しいドライバでは古いCUDA runtimeを利用できるため、CUDA 13対応ホストにもCUDA 12版を使える。ただしOpenMMの実行時コンパイルと各ライブラリのGPU世代対応を確認する必要がある。[NVIDIA互換性](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)

2026-10-24追記：本人承認で標準DockerのCUDA 12.9統一、ARM Pixi lockと公式ARM版MODELLERの導入、GPU実計算検査の共通化を実装した。RIKYU構成は維持。以下は調査時点の記録であり、現在の変更と検証範囲は[開発記録](../memo.md)・[導入手順](../agents/deployment.md)を参照。

同日追加指示：n1〜n5の互換性を優先し、標準コンテナの12.9化は撤回。GPU依存を一括除去してcu118 Torch・CPU JAX・11.8でソースビルドしたOpenMMに置換する。Linux Pixiの12.9とRIKYUの13系は別構成で維持。実機合格は未確認。

## 現状

| 経路 | 実際の定義・固定内容 | 問題・制約 |
|---|---|---|
| Linux x86_64 pixi | OpenMM 8.6.1、PyTorch 2.7.1 cuda129、NVRTC 12.9.86 | CUDA 12.9単一環境。CUDA 12の古いドライバ全般を保証できない |
| 標準Docker／そのSIF | 上記lockを導入後、Torchを2.7.1+cu118へ置換、OpenMM 8.5.1をCUDA 11.8でソースビルド | condaのlibtorch・CUDA 12.9依存を除去する処理がなく残留する構成。最終的なロード先・ABI整合は実物での検証が必要 |
| RIKYU arm64 Docker／そのSIF | environment.ymlを加工してmambaで解決。OpenMM 8.5.1、NVRTC 13.0、Torch 2.10.0 cuda130、CUDA数学ライブラリ13.1・cuFFT 12.1以上 | pixi.lockを使わない。openmm-torchはsm_100向け。一般のARM GPUすべてを対象とする構成ではない |
| Linux aarch64 pixi | workspaceに宣言あり | defaultに入るmodeller featureが対象外とするため、default環境・lockにARM用パッケージがない |
| macOS arm64 pixi | OpenMM 8.6.1、CPU版Torch | NVIDIA CUDAの検証環境にはならない |

根拠: [pixi.toml](../../pixi.toml)、[pixi.lock](../../pixi.lock)、[標準Dockerfile](../../container/Dockerfile)、[RIKYU Dockerfile](../../container/Dockerfile.rikyu-arm64)。`pixi 0.82.1 info --json`でdefaultの対象がlinux-64-cuda／osx-arm64のみと確認。`pixi list --locked --no-install --platform linux-aarch64-cuda '^openmm$'`は「No packages found」。Linux x86_64は同コマンドで上表の固定版を確認した。インストール・lock更新はしていない。

## 互換性を決めるもの

- ホストのNVIDIAドライバ、GPUの世代、環境内のCUDA runtime／NVRTC／数学ライブラリは別々に確認する。DockerにはホストのCUDA Toolkit自体は不要。[NVIDIA Container Toolkit](https://github.com/NVIDIA/nvidia-container-toolkit/blob/main/README.md?plain=1)
- CUDA 12.xはR525以降、13.xはR580以降というminor互換の範囲があるが、PTXというGPU向け中間コードを古いドライバが読めない場合は対象外。OpenMM [8.5.1](https://raw.githubusercontent.com/openmm/openmm/8.5.1/platforms/cuda/src/CudaContext.cpp)と[8.6.1](https://raw.githubusercontent.com/openmm/openmm/8.6.1/platforms/cuda/src/CudaContext.cpp)はNVRTCでPTXを生成し、ドライバで読み込む。[NVIDIAの制限](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)
- NVRTC 12.9版の保守的な基準はR575級（12.9 GAは575.51.03、Update 1は575.57.08）。これはCUDA 12.9の全プログラムに必須という意味ではない。同様に標準Dockerfileの「CUDA 11.8なら450+」はOpenMMの無条件保証としては過大。11.8対応ドライバ520.61.05以上を保守的な基準とし、それより古い構成は個別検証が必要。[版ごとの対応](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-toolkit-release-notes/index.html)、[11.8](https://docs.nvidia.com/cuda/archive/11.8.0/cuda-toolkit-release-notes/index.html)
- BlackwellはCUDA 12.8からネイティブ対応があり、CUDA 13が一律必須ではない。CUDA 13はMaxwell・Pascal・Voltaのコンパイル／ライブラリ対応を削除しているので、全GPUの13統一は適さない。[Blackwell](https://docs.nvidia.com/cuda/archive/12.8.0/blackwell-compatibility-guide/index.html)、[CUDA 13](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-toolkit-release-notes/index.html)
- Pixiの`cuda`宣言と`CONDA_OVERRIDE_CUDA`は互換能力の宣言・上書きであり、ドライバを更新するものではない。runtimeは依存指定とlockで固定する。[Pixi](https://pixi.prefix.dev/latest/workspace/system_requirements/)

## 優先する改善案

1. **最小ドライバとGPU世代を先に決める。** 現在の12.9を標準にできるのは、そのNVRTCを実行できるドライバ群を対象にする場合。R535／R550等も含むなら、低いNVRTCでのソースビルドか既存11.8版の維持が必要。CUDA 13対応ドライバという理由だけで13版へ切り替えない。
2. **各構成を最初から整合した依存として解決する。** 通常CUDA 12版、必要な既存RIKYU CUDA 13版、必要なら旧環境向け11.8版を区別する。互いに競合するCUDA版を同じsolve-groupへ押し込まず、Pixi環境ごとにlockする。ARM用のMODELLER配布経路も分ける。Dockerは対応するlockから作り、OpenMM／Torchの後置換をなくすか、ソースビルド層を明示し同じ版を使う。12.9より古いOpenMMビルドのパッケージ供給方法は実装時に検討する。
3. **実計算の検査を共通にする。** doctorは既にContext生成・エネルギー・1ステップを試すが、GPUが使えなくてもCPUが動けば成功する。GPU必須の受入検査ではCUDAを明示し、失敗をCPU成功で通さない。OpenMMの力・PME、TorchのGPU演算・FFT、PythonTorchForceの力／勾配を検査する。DockerとSIFの両方で実行する。
4. **ロードされたライブラリを確認する。** ドライバ、GPU名・compute capability、OpenMM／Torch／NVRTC版、libcuda・NVRTC・cuFFT・libtorchの実ロード先を記録する。SIFの`--nv`はホストのライブラリを取り込むため、ホストの設定も影響する。`--nvccli`はexperimentalで、一律の置換策にしない。[Apptainer](https://apptainer.org/docs/user/latest/gpu.html)

既存の[標準smoke](../../container/scripts/test-container.sh)はGPUなしをSKIPし、GPUありでもOpenMMのCUDA登録確認まで。[RIKYU検査](../../container/scripts/test-rikyu-gpu.sh)は実際のOpenMM計算・PME・Torch FFTまで行うが、PythonTorchForceはimport確認のみ。[doctor](../../scripts/_internal/doctor.py)と[platform inspection](../../mdclaw/simulation/platform.py)は登録確認より強い検査へ既に改善済み。[CI](../../.github/workflows/ci.yml)・[Container](../../.github/workflows/container.yml)・[SIF download](../../.github/workflows/sif.yml)はGPU実行の保証にはならない。

起動側の追加論点: [runtime.sh](../../scripts/_internal/runtime.sh)はnvidia-smiの存在でGPUオプションを付けるだけで、CUDA版に合うイメージ／Pixi環境は選ばない。[entrypoint.sh](../../container/scripts/entrypoint.sh)のcompat判定もドライバ版ではなくnvidia-smiの有無。互換パッケージの自動注入は、対応GPU・ドライバを確認した例外に限定する。[NVIDIA forward compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/forward-compatibility.html)

## 未確認

調査端末はmacOS arm64。対象計算サーバーの現在のドライバ／GPU一覧、最終イメージの動的リンク先、各ドライバでのGPU計算は今回未確認。大学停電に関する既存指示に従いSSH接続していない。公開済みSIFの内容を現checkoutのDockerfileと同一とはみなしていない。次の実装判断に必要なのは、対応対象ホストのドライバ・GPU・CPUアーキテクチャの一覧。
