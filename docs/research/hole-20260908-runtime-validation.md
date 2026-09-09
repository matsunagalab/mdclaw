# mdahole2 / HOLE runtime addition

2026-09-08. Shweta requested a working pore-analysis stack, including the external executable rather than only an importable MDAnalysis compatibility module.

## Delivered components

- HOLE 2.3.1, conda-forge Linux aarch64 build `hed5cd8a_1`: `hole`, `sph_process`, `sos_triangle`, auxiliary executables and radius tables.
- mdahole2 0.5.0 from the official PyPI wheel, using existing MDAnalysis 2.10.0.
- `environment.yml` declares `hole2=2.3.1`; `pyproject.toml` declares `mdahole2>=0.5,<0.6`. Both Dockerfiles already consume these definitions. Pip-only installation still requires installing the external HOLE package separately.
- `container/scripts/test-container.sh` now exercises two trajectory frames and surface generation, not just imports. Usage is documented in [the container guide](../developer/container.md#hole-pore-analysis).

The [upstream installation guide](https://www.mdanalysis.org/mdahole2/getting_started.html) supports a Python interface plus a separate conda-forge HOLE executable. The recommended Python import is `from mdahole2.analysis import HoleAnalysis`; `MDAnalysis.analysis.hole2` is a deprecated compatibility interface. Upstream sources: [mdahole2](https://github.com/MDAnalysis/mdahole2), [HOLE](https://github.com/osmart/hole2). License texts are included in the installed wheel and `/opt/mdclaw/share/licenses/hole2/LICENSE`.

## Actual construction and provenance

The cluster-local SIF was rebuilt on Linux ARM64 from the previously validated SMO-fix runtime prefix. The dependency transaction was inspected with installed packages frozen. Final additions are HOLE plus the mdahole2 wheel; existing scientific dependency packages were not replaced. MDClaw was reinstalled from a wheel to include its new requirement metadata, retaining the exact previously validated Python source tree.

The initially inspected conda mdahole2 0.5.0 package reported Python distribution/version metadata as 0.0.0. Its interface packages were removed and replaced with the official PyPI 0.5.0 wheel, consistent with the repository's pip stage. HOLE remains managed by conda. No metadata was hand-edited to disguise the version.

- Bundle ID: `b7526a99807f`.
- MDClaw package version: 0.6.8; source commit before dependency edits: `1af2127`.
- Preserved Python source tree SHA-256: `561049fe825429b42086115b672d46bf79f51c6011969b62068f1f1f9e088c76`.
- HOLE package SHA-256: `92f0b70d3f2260f5c3003e3929bcdff47c17f2d3794db2b409589006729dcab0`.
- mdahole2 wheel SHA-256: `6b7a9be247cd99331e8840c9012d0ed7a86874fe110983bbaed00eb39459e7a8`.
- Final SIF SHA-256: `6c384c0ac9fb79e30e9314717e4037a2f61152498942edf6a0fd69f7ab9bbf21`.

The SIF includes `/opt/mdclaw/hole-runtime-manifest.json` with dependency/build-input hashes. This is a cluster-local runtime update, not a new version tag or GHCR publication. A fresh multi-stage Docker build and an amd64 runtime execution were not performed in this session.

## Acceptance

Synthetic cylindrical carbon walls have prescribed radii 5 and 3 Å, with default carbon VDW radius 1.85 Å, producing expected central pore radii 3.15 and 1.15 Å. The test supplies an initial point/direction to HOLE, uses seed 31415, and requires finite profiles for both frames and agreement within 0.15 Å. This tests the actual HOLE algorithm, not an axis-only radius estimator.

| Check | Result |
| --- | --- |
| First-frame central minimum | 3.14947 Å |
| Second-frame central minimum | 1.14955 Å |
| Frame coverage | Both frames, finite profiles, >10 central samples each |
| sph_process + sos_triangle surface | 173,709-byte VMD file generated |
| Final SIF standalone HOLE execution | Passed without checkout PYTHONPATH |
| GPU-node container verification | 28 passed, zero failed |
| OpenMM CUDA/PME, cuFFT prefault, PyTorch FFT | Passed |
| Shell syntax / repository-configured Ruff / diff whitespace | Passed |

Slurm job **87590**, account **rkp00079**, NVIDIA GB200. The synthetic test requires no downloaded structure, user data or scientific MD workflow. It does not claim that a particular pathway in SMO is a conducting pore; selecting and validating that pathway is a separate analysis.

Evidence is retained in `/home/rku00161/mdclaw/.validation/hole-20260908`, with construction logs under `/tmp/mdclaw-hole-candidate`.

## Shared deployment

The existing shared compatibility path
`/data1/rkp00079/mdclaw-rikyu-arm64-cuda130-cufft121-fusefix-6f171d2f0fa5.sif`
was switched after acceptance to
`/data1/rkp00079/mdclaw-rikyu-arm64-cuda130-cufft121-hole2-b7526a99807f.sif`.
The immediately previous SMO-fix SIF remains intact, with a rollback reference
`/data1/rkp00079/mdclaw-rikyu-arm64-cuda130-cufft121-fusefix-6f171d2f0fa5.pre-hole-20260908.sif`.
Checksum, runtime manifest and deployment JSON are sidecars beside the new image. Previously opened images are not modified in place. User repositories and simulation artifacts are untouched.
