<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/mdclaw-logo-dark.png">
    <img src="docs/assets/mdclaw-logo-light.png" alt="MDClaw" width="480">
  </picture>
</p>

<p align="center">
  <strong>Molecular dynamics with AmberTools and OpenMM, driven by your AI coding agent.</strong><br>
  Describe the system or the question. Every stage is checked, recorded and resumable.
</p>

<p align="center">
  <a href="https://github.com/matsunagalab/mdclaw/actions/workflows/ci.yml"><img src="https://github.com/matsunagalab/mdclaw/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/matsunagalab/mdclaw/tags"><img src="https://img.shields.io/github/v/tag/matsunagalab/mdclaw?label=version&color=0E9FB7" alt="Version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-283747" alt="MIT license"></a>
</p>

<p align="center">
  <a href="#install">Install</a> &nbsp;·&nbsp;
  <a href="#what-you-can-ask-for">Examples</a> &nbsp;·&nbsp;
  <a href="#capabilities">Capabilities</a> &nbsp;·&nbsp;
  <a href="#what-mdclaw-does-not-do">Limits</a> &nbsp;·&nbsp;
  <a href="docs/agents/deployment.md">Deployment guide</a>
</p>

<p align="center">
  <a href="docs/assets/mdclaw-cbi-demo.mp4">
    <img src="docs/assets/mdclaw-cbi-demo.gif" alt="CBI 2026 demo: Codex prepares and simulates chignolin, displays the trajectory and plots the analysis" width="880">
  </a>
</p>

<p align="center"><sub>CBI 2026: a real Codex session, from a request to simulation, molecular playback and analysis.<br>The 20-second video is edited and accelerated. <a href="docs/assets/mdclaw-cbi-demo.mp4">Watch the full-resolution MP4</a> · <a href="#try-the-chignolin-demo">Try the demo</a></sub></p>

MDClaw gives agents such as Claude Code and Codex two things: **skills**, which
tell the agent what to do and why, and **command-line tools**, which do the work.
The tools are also the layer that can refuse. Molecule selection, force-field
templates, charges, stage dependencies and required files are checked in code,
and a failed check stops with a stable error code and the next action, rather
than relying on the agent to be careful.

Each stage records its inputs, outputs and status as a node in a directed acyclic
graph. A study can be inspected, resumed, branched or written up later, without
the original chat. You can also call the tools directly, with no agent at all.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/workflow-dark.png">
    <img src="docs/assets/workflow-light.png" alt="Workflow: Structure, Prepare, Solvate, Force field, Minimize, Equilibrate, Production, Analyze" width="880">
  </picture>
</p>

> [!IMPORTANT]
> A run that finishes is not a validated result. MDClaw's checks cover topology,
> charges, energies and files. They do not establish convergence, adequate
> sampling or a biological conclusion. Those need appropriate controls,
> replicates and expert review.

## What you can ask for

Start your agent in the directory where the simulation files should live, then
say what to do and where to stop.

**Prepare and equilibrate one system**

```text
Prepare PDB 1AKE chain A as a protein-only explicit-water system using the default
force field and water model. Continue through default equilibration and stop
before production.
```

**Compare systems and analyze the results**

```text
Set up and run an apo-vs-holo MD study for the T4 lysozyme L99A benzene-binding
cavity (benzene-bound PDB 4W53). Plan the minimal job set, run 50 ns of production
per job, compare RMSD, RMSF and cavity-lining distances, and state what the data
do and do not support.
```

**Estimate a free-energy change**

```text
Estimate the folding free-energy change of the T4 lysozyme L99A mutation by FEP,
and report the convergence diagnostics with the result.
```

**Watch a running job**

```text
Show the progress of prod_001 in Molstar.
```

To see a plan first, ask the agent to propose the study and stop before running
anything. For long simulations use a GPU, and say so if jobs should go through
Slurm.

## Install

MDClaw has two parts, installed separately: the **runtime** (the scientific
software that performs the calculations) and the **skills** (the instructions for
your agent). Set up the runtime first, on the machine that will run the
calculations.

### 1. Get the runtime

Find your machine below. On Linux with an NVIDIA GPU, `nvidia-smi` prints the
driver version.

| Your machine | GPU support | Runtime |
|---|---|---|
| Cluster, Linux x86-64, Singularity or Apptainer | CUDA 11.8, driver R520 or newer | [Container](#container-singularity-or-apptainer), `mdclaw:latest` |
| Cluster, Linux ARM64, Singularity or Apptainer | CUDA 12.9, driver R575 or newer | [Container](#container-singularity-or-apptainer), `mdclaw:latest` |
| RIKYU | CUDA 13 | [Container for RIKYU](#rikyu), `mdclaw:latest-rikyu` |
| Linux workstation, driver R575 or newer | CUDA 12.9 | [Pixi](#pixi), `pixi install` |
| Linux workstation, driver R520 to R574 | CUDA 11.8 | [Pixi](#pixi), `pixi install -e cuda118` |
| Linux, no NVIDIA GPU | CPU | [Pixi](#pixi), `CONDA_OVERRIDE_CUDA=12.9 pixi install` |
| Mac, Apple Silicon | OpenCL | [Pixi](#pixi), `pixi install` |
| Any machine with Docker | Same as the container; CPU only on a Mac | [Docker](#docker) |

Intel Macs are not supported. CUDA 11.8 cannot drive Blackwell GPUs, so use
`pixi install` for those on x86-64.

#### Pixi

Pixi installs the exact package versions recorded in `pixi.lock`.

```bash
curl -fsSL https://pixi.sh/install.sh | sh    # then open a new terminal
git clone https://github.com/matsunagalab/mdclaw.git
cd mdclaw
pixi install                                  # or the command from the table
export PATH="$PWD/bin:$PATH"                  # add the absolute path to your shell startup file
scripts/mdclaw-doctor.sh
```

- The first install compiles openmm-torch and PLUMED, and OpenMM in `cuda118`.
  Allow up to 30 minutes and about 60 GB of free disk. Afterwards
  `rm -rf .pixi/bld` recovers most of that space.
- Keep the checkout outside cloud-sync folders.
- `bin/mdclaw` reads the NVIDIA driver and selects the environment.
  `MDCLAW_PIXI_ENV` overrides it.
- MODELLER on Linux ARM64 needs one more step:
  `pixi run python scripts/install-modeller-arm64.py`.

#### Container (Singularity or Apptainer)

The same tag serves x86-64 and ARM64; the download matches your machine. No
checkout or Pixi is needed. Use `apptainer` in place of `singularity` if that is
what you have.

```bash
singularity pull mdclaw.sif docker://ghcr.io/matsunagalab/mdclaw:latest
export MDCLAW_SIF="$PWD/mdclaw.sif"
export MDCLAW_RUNTIME=singularity
```

The download takes about 25 GB of temporary space for a 5.5 GB SIF. If `/tmp` or
your home directory is small, see
[temporary storage](docs/developer/container.md#singularity). If your group
already has a shared SIF, point `MDCLAW_SIF` at it instead.

Then check it on a GPU node. `--nv` exposes the NVIDIA GPU to the container.

```bash
singularity exec --nv "$MDCLAW_SIF" mdclaw --version
singularity exec --nv "$MDCLAW_SIF" python /opt/mdclaw/share/mdclaw/test-gpu.py
```

The last line of the GPU test reads `CUDA_GPU_SMOKE=PASS`. For job submission and
bind mounts, see [server setup and Slurm](docs/agents/deployment.md#sif-no-user-checkout-required).

#### RIKYU

RIKYU has its own image: its GPUs need a newer cuFFT, NVRTC must stay at 13.0 for
the driver, and SIFs there are mounted through FUSE. Do not use plain
`mdclaw:latest` on RIKYU.

```bash
export APPTAINER_TMPDIR=/path/with/25GB/free
export APPTAINER_CACHEDIR=/path/with/25GB/free
apptainer pull mdclaw.sif docker://ghcr.io/matsunagalab/mdclaw:latest-rikyu
export MDCLAW_SIF="$PWD/mdclaw.sif"
export MDCLAW_RUNTIME=apptainer
```

Run the GPU check from the SIF, inside a GPU job. A container filesystem does not
reproduce the FUSE failure this test guards against.

```bash
apptainer exec --nv "$MDCLAW_SIF" python /opt/mdclaw/share/mdclaw/test-gpu.py --require-torch-force
```

To build an unreleased commit on an ARM64 host, see the
[ARM64 image guide](docs/developer/container.md#arm64--cuda-13-image).

#### Docker

```bash
docker pull ghcr.io/matsunagalab/mdclaw:latest
export MDCLAW_RUNTIME=docker
export MDCLAW_DOCKER_IMAGE=ghcr.io/matsunagalab/mdclaw:latest
```

On a Mac this runs the ARM64 Linux image on the CPU, because Docker exposes no GPU
to Linux containers there. SIF files do not run on macOS. For the GPU on a Mac, use
[Pixi](#pixi).

### 2. Connect your agent

Installing skills does not install the runtime, so keep the one from step 1
available to the agent. If you work inside the MDClaw checkout, skills are found
automatically and you can skip this step.

**Claude Code**

```bash
claude plugin marketplace add matsunagalab/mdclaw
claude plugin install mdclaw@mdclaw --scope user
```

**Codex**

```bash
codex plugin marketplace add matsunagalab/mdclaw
codex plugin add mdclaw@mdclaw
```

Start a new session afterwards. With a container runtime, export `MDCLAW_SIF` and
`MDCLAW_RUNTIME` before starting the agent. With Pixi, put the checkout's `bin/`
on `PATH`. If you do neither, the plugin's startup hook downloads a container when
Singularity, Apptainer or Docker is installed.

<details>
<summary><strong>Other agents, the skills CLI, and Pi</strong></summary>

<br>

The skills CLI works with Claude Code, Codex, Cursor, OpenCode and other agents. It
needs [Node.js](https://nodejs.org/en/download) 22.20 or newer.

```bash
npx skills add matsunagalab/mdclaw            # for the current project
npx skills add matsunagalab/mdclaw --global   # for your user account
```

Select your agent, and install `common` together with the workflow skills. Pi
installs from the repository:

```bash
pi install git:github.com/matsunagalab/mdclaw@main
```

See the [deployment guide](docs/agents/deployment.md) for Conda, runtime selection
and troubleshooting. BioEmu and Boltz-2 are optional, separate installs: see
[model backend setup](docs/developer/configuration.md#model-backend-runtime).

</details>

### 3. Check it

`scripts/mdclaw-doctor.sh` runs a real OpenMM Context on each platform, starts
AmberTools, and reports the selected runtime, usable GPUs and optional features.
Resolve any `[fail]` line before running calculations. A missing optional feature
only affects the workflows that need it.

### What differs between runtimes

| | Pixi on Linux | Pixi on macOS | Containers |
|---|---|---|---|
| OpenMM 8.6.1, PLUMED 2.9.4, PythonTorchForce, SST2 | Yes | Yes | Yes |
| Compute platform | CUDA | OpenCL | CUDA |
| MODELLER (bring your own `KEY_MODELLER`) | Yes; ARM64 needs the extra step | Yes | Yes |
| HOLE binaries, for custom pore analysis | Yes | No | Yes |
| Membrane orientation with PPM3 | No (`ppm3_format_bug`) | Not verified | Yes |

The PPM3 binary in the conda package crashes while printing its result, and only
the container images rebuild it. In a Pixi environment, orient a membrane protein
from an OPM homolog, with MEMEMBED, or pass `--preoriented`.

## Try the chignolin demo

The CBI video above uses **CLN025**, a ten-residue chignolin variant
([PDB 5AWL](https://www.rcsb.org/structure/5AWL), chain A, `YYDPETGTWY`).
The structure is public and MDClaw downloads it from the PDB. After installing
the runtime and skills above, start Codex or Claude Code in an empty working
directory and give it this prompt:

```text
Use MDClaw to run a short chignolin demo in a new study directory.
Fetch CLN025 (PDB 5AWL, chain A), keep the peptide only, and prepare it
with ff19SB and OPC water, 15 angstrom padding and 0.15 M NaCl.
Minimize, equilibrate for 10 ps NVT followed by 10 ps NPT, then run
100 ps of NPT production at 300 K and 1 bar with HMR and a 4 fs time step.
Save a trajectory frame every 1 ps. Use the local GPU if available.
Show the prepared system, then play the production trajectory with
cartoon and ball-and-stick representations. Calculate backbone RMSD
to the first saved frame and C-alpha RMSF after backbone alignment,
and show the plots. Keep the study, DAG records and all output files.
```

**Expected output:** a study directory containing the job's stage records,
prepared solvated structure, OpenMM system and state files, a production DCD
trajectory with 100 saved frames, energy/time logs, and RMSD/RMSF analysis
files and plots. The agent can display the trajectory using the viewer
available in your client; see [Viewing results](#viewing-results).
Numerical trajectories differ with random velocities and hardware.

**Expected time:** allow about **10-30 minutes** on a desktop with a supported
GPU, after installation. This is the maintainer's estimate for the complete
agent workflow, including preparation, simulation and analysis; CPU-only runs,
first-use downloads and agent response times can take longer. The CBI recording
used MDClaw 0.6.12 on an Apple M4 with OpenCL. This short run demonstrates the
workflow; it does not establish folding or sampling convergence.

To use your own data, replace the PDB identifier in the prompt with the path
to your PDB/mmCIF file and specify the chains, molecules and simulation goal.

## Viewing results

Ask to see a structure or a trajectory. The display skill picks the route that
your client supports, and `inspect_job` shows where each job stands.

| Client | How Molstar is shown |
|---|---|
| Claude Code (terminal, Desktop, Remote Control) | A private claude.ai Artifact page. It needs no port or tunnel, so a desktop outside a site firewall can open it, and it can show a production that is still running. |
| Codex desktop | An attached MCP viewer. Coordinates stay on the host. The Codex plugin includes it. |
| A browser that reaches the host | `mdclaw serve_molstar`, with the bundled Mol*. |

An Artifact page carries the displayed coordinates through claude.ai, so do not use
it when data must stay on the host. Still images use PyMOL. Preparation views keep
water, ions and the periodic cell; production views show the solute and any lipids.
See the [viewer guide](docs/developer/molstar-viewer.md).

## Capabilities

Every workflow below is available through the skills and the CLI. Some need
optional software, which the doctor reports.

### Free energy and enhanced sampling

| Method | What MDClaw does | Limits |
|---|---|---|
| **Mutation FEP** ([skill](skills/md-fep/SKILL.md)) | Hybrid-topology relative free energy, analyzed with MBAR. Gives ΔΔG of folding from folded and capped-tripeptide legs. Charge-changing mutations use a co-alchemical ion. | One substitution per calculation. No glycoproteins. Implicit solvent is not supported. A binding cycle exists in `estimate_ddg --cycle binding`; the skill guides the folding cycle. |
| **Absolute binding free energy** ([skill](skills/md-abfe/SKILL.md)) | Double decoupling with a Boresch restraint, 1 M standard state, analyzed with MBAR. | One neutral, noncovalent ligand. |
| **Umbrella sampling** ([page](skills/md-production/distance-restraints.md)) | Fixed-bias windows with distance restraints, custom forces or PLUMED, followed by potential of mean force (PMF) analysis using task-specific scripts (e.g. with PyMBAR). | Check window overlap and convergence. |
| **Metadynamics** ([page](skills/md-production/metadynamics.md)) | Well-tempered metadynamics through OpenMM, with multiple walkers sharing a bias. | One collective variable: a center-of-mass distance. |
| **SST2** ([page](skills/md-production/sst2.md)) | Simulated solute tempering: a single trajectory with REST2-style scaling and tempering moves, analyzed with `analyze_tempering`. | Explicit solvent with PME. |
| **Weighted ensemble** ([skill](skills/md-we/SKILL.md)) | Split, merge and recycle walkers over distance, RMSD, dihedral or Q coordinates, locally or packed on GPUs with Slurm MPS. Gives rates and mean first-passage times from unbiased dynamics. | The rate holds only once the flux has converged. |
| **Biased and steered MD** ([distance restraints](skills/md-production/distance-restraints.md), [PLUMED](skills/md-production/plumed.md)) | Harmonic atom or center-of-mass distance restraints, steering to target distances (including umbrella-window preparation), PythonTorchForce custom forces, and PLUMED restraints. | PLUMED is limited to history-free actions: `DISTANCE`, `ANGLE`, `TORSION`, `GROUP`, `COM`, `CENTER`, `WHOLEMOLECULES`, `RESTRAINT`, `MOVINGRESTRAINT`, `PRINT`, `UNITS`. |

### Preparation

| Area | What is supported |
|---|---|
| Structures | PDB, AlphaFold via UniProt, local PDB/mmCIF, biological assemblies. Optional generators: Boltz-2, BioEmu (monomers), MODELLER. |
| Proteins and nucleic acids | Amber ff19SB (default) and ff14SB, with other Amber force fields in the catalog; DNA OL15 and RNA OL3. Protonation by PROPKA at pH 7.4. Mutations with side-chain reconstruction by HPacker. |
| Water and ions | OPC (default), OPC3, TIP3P, SPC/E, TIP4P-Ew, TIP3P-FB, TIP4P-FB. 15 Å buffer and 0.15 M NaCl by default. ff19SB with TIP3P is refused, following Amber's guidance. |
| Implicit solvent | Generalized Born: HCT, OBC1, OBC2, GBn, GBn2. |
| Ligands | Noncovalent small molecules with GAFF2, charges from OpenFF NAGL with automatic fallback to AM1-BCC. |
| Glycans and PTMs | Covalently linked glycans with GLYCAM. Phosphorylated Ser, Thr and Tyr (SEP, TPO, PTR) with ff19SB. |
| Membranes | Lipid21. Equilibrated patches ship for POPC, POPE, DPPC, POPC:CHL1 4:1, POPC:POPE:CHL1 2:1:1 and DPPC:DOPC:CHL1 1:1:1 with OPC or TIP3P, and DOPC with OPC. Other compositions are built inside a Slurm allocation; anionic mixtures such as DOPE:DOPG have a known crash during patch equilibration. |

### Simulation, analysis and reporting

| Area | What is supported |
|---|---|
| Simulation | Minimization, multi-stage NVT/NPT equilibration, production, restart and extension, replicates. Production defaults: 4 fs with hydrogen mass repartitioning, Langevin middle integrator, PME with a 1.0 nm cutoff. |
| Analysis | RMSD, RMSF, distances, contact frequency, native-contact fraction, equilibration detection, trajectory joining and fitting, and the estimators for FEP, ABFE, SST2, metadynamics and weighted ensemble. |
| Custom analysis | Anything else is written for the task and recorded with `register_analysis_result`. |
| Reporting | Methods-style reports with BibTeX and provenance, and offline MDDB deposit bundles. |
| Execution | Local runs, Slurm jobs and arrays, GPU packing with MPS, and throughput estimates. |

## What MDClaw does not do

**Methods that are not provided**

- Replica exchange, and accelerated or Gaussian-accelerated MD.
- Relative binding free energies between ligands.
- MM/PBSA, thermodynamic integration, and BAR as a separate estimator.
- Coarse-grained or polarizable models, and QM/MM.
- CHARMM-native preparation. `build_openmm_system` accepts OpenMM force-field XML
  for other force fields, outside the standard workflow.
- Production in vacuum. A vacuum system can be built and minimized, but equilibration
  and production are refused.

**Chemistry that needs your own model**

- Covalent ligands, bonded metal centers and general organometallics. Bare
  structural ions are checked against the selected water and ion templates.
- PTMs beyond SEP, TPO and PTR: phospho-histidine, acetylation, methylation,
  ubiquitination, lipidation, and a choice of phosphate protonation state.
- Modified DNA or RNA. The workflow stops rather than substituting an ordinary
  nucleotide; a legacy modXNA helper is experimental.
- Charged ligands in absolute binding free-energy calculations.

**Analyses that are not built in**

PCA, clustering, Markov state models, secondary structure, hydrogen bonds, SASA,
radius of gyration, radial distribution functions, pores and cavities, hydration,
and lipid order or thickness. An agent can write these as custom analyses, and you
should review that code as you would any other.

## Test status

| Configuration | Automated | On real GPUs |
|---|---|---|
| Linux x86-64, CUDA 11.8 (image and `cuda118`) | CPU tests in CI | Acceptance runs on RTX A6000, TITAN X Pascal, GTX 1080 Ti and RTX 2080 Ti before a shared SIF is replaced |
| RIKYU image | CPU tests in CI | Run the GPU check above before adopting a new image |
| Linux, CUDA 12.9 (`default` and the ARM64 image) | CPU tests in CI | Not yet exercised on a GPU by the maintainers |
| macOS Apple Silicon | CPU tests in CI | OpenCL not yet covered by CI |

MDClaw is research software under active development. Check the doctor output and
[known issues](docs/developer/roadmap-and-known-issues.md) before relying on it.

## Results and resuming work

Each study has a plan and one or more jobs. Variants and retries create new branches
and leave earlier results intact. To continue, give the agent the study directory
and say what to do next: it reads progress with `inspect_job`, checks the next step
with `explain_node`, and investigates failures with `trace_failure`.

## Updating

Update the skills the way you installed them, then start a new session. Update the
runtime separately, because a skill update does not change it.

| Installed with | Update |
|---|---|
| Claude Code plugin | `claude plugin marketplace update mdclaw`, then `claude plugin update mdclaw@mdclaw` |
| Codex plugin | `codex plugin marketplace upgrade mdclaw`, then `codex plugin add mdclaw@mdclaw` |
| Skills CLI | `npx skills update` |
| Pixi runtime | `git pull`, then `pixi install` with the same options |
| SIF runtime | `singularity pull` again; keep the previous SIF until the new one passes the GPU check |

## Development

```bash
pixi install
pixi run ruff check mdclaw/
pixi run pytest tests/test_registry.py tests/test_cli.py tests/test_guardrails.py tests/test_guardrail_code_registry.py tests/test_slurm_server.py -v
```

Agent guidance is mirrored in `CLAUDE.md` and `AGENTS.md`; keep them identical. See
the [architecture](docs/developer/architecture.md),
[tool reference](docs/developer/tool-reference.md),
[CLI internals](docs/developer/cli-internals.md),
[testing guide](docs/developer/testing.md),
[configuration reference](docs/developer/configuration.md) and
[release guide](docs/developer/release.md).

A `vX.Y.Z` tag builds every image from `pixi.lock` on native GitHub runners, runs
the CPU smoke test, and publishes `mdclaw:X.Y.Z` (x86-64 and ARM64) and
`mdclaw:X.Y.Z-rikyu`. The GPU check stays a manual gate before a shared SIF is
replaced.

<details>
<summary><strong>Repository map</strong></summary>

<br>

| Path | Role |
|---|---|
| `skills/` | Portable MDClaw skills; the source of truth for skill behavior. |
| `.agents/skills/`, `.claude/skills/` | Discovery entries, symlinked to `skills/`. |
| `.claude-plugin/`, `.codex-plugin/`, `hooks/` | Plugin metadata and lifecycle hooks. The Codex manifest adds the Molstar MCP server. |
| `bin/mdclaw` | Runtime wrapper used by plugin and local deployments. |
| `pixi.toml`, `pixi.lock` | Locked definition of the runtime: the `default`, `cuda118` and `cuda13` environments. |
| `recipes/` | Source builds run by pixi on install: OpenMM for CUDA 11.8, openmm-torch, PLUMED, openmm-plumed. |
| `container/` | Image definitions: `Dockerfile.pixi` (x86-64 and ARM64) and `Dockerfile.rikyu-arm64`. |
| `environment.yml` | Legacy conda definition, kept for existing environments. |
| `mdclaw/` | Python package and CLI tools. |
| `docs/agents/`, `docs/developer/` | Deployment notes and developer references. |
| `tests/` | Unit, smoke and integration tests. |

</details>

## License

MDClaw's own code and agent skills are licensed under the **MIT License**
([LICENSE](LICENSE)). Third-party software, bundled files, model weights and
reference data retain their respective licenses.

### Bundled third-party files

| Material | License and notices |
|---|---|
| Mol* viewer build | MIT. [License](mdclaw/visualization/molstar/vendor/LICENSE), [version and provenance](mdclaw/visualization/molstar/vendor/README.md). Notices embedded in the bundle are retained. |
| MDDB workflow schema and constants | Apache-2.0. [License](mdclaw/evidence/mddb_schema_data/LICENSE), [upstream source and provenance](mdclaw/evidence/mddb_schema_data/README.md). |

### Scientific packages

The following summarizes the main scientific dependencies and optional tools.
Versions and platform-specific installations are recorded in
[pixi.lock](pixi.lock), [pixi.toml](pixi.toml), [pyproject.toml](pyproject.toml)
and the source-build [recipes](recipes/). License labels below follow package
metadata and upstream notices. Each package's license files govern its use.

| Package | License |
|---|---|
| [OpenMM](https://docs.openmm.org/latest/userguide/library/01_introduction.html#license) | MIT for the APIs, application layer, CPU and reference platforms. LGPL-3.0-or-later for GPU platforms. Additional notices are included upstream. |
| openmm-torch / PythonTorchForce | MIT ([recipe](recipes/openmm-torch/recipe/recipe.yaml)). |
| PLUMED / openmm-plumed | LGPL-3.0-only / LGPL-3.0-or-later ([PLUMED recipe](recipes/plumed/recipe/recipe.yaml), [plugin recipe](recipes/openmm-plumed/recipe/recipe.yaml)). |
| AmberTools | GPL-3.0-or-later, LGPL-3.0-or-later, BSD-3-Clause and MIT, depending on the component. These are not alternative licenses for the whole package. |
| [SST2](https://github.com/matsunagalab/SST2/blob/3dc7013/LICENSE) | GPL-2.0. |
| [MDAnalysis](https://github.com/MDAnalysis/mdanalysis/blob/develop/LICENSE) | LGPL-3.0-or-later, with component-specific notices. |
| [MDTraj](https://pypi.org/project/mdtraj/1.11.1.post1/), ParmEd | LGPL-2.1-or-later. |
| [mdahole2](https://github.com/MDAnalysis/mdahole2), HOLE2 | LGPL-2.1-or-later for mdahole2, Apache-2.0 for HOLE2. |
| [PROPKA](https://pypi.org/project/propka/3.5.1/) | LGPL-2.1. |
| [PDB2PQR](https://github.com/Electrostatics/pdb2pqr/blob/main/LICENSE.md) | BSD-style, with additional bundled-code notices. |
| PDBFixer, openmmforcefields | MIT. |
| OpenFF Toolkit, NAGL, [Pablo](https://github.com/openforcefield/openff-pablo/blob/main/LICENSE) | MIT. |
| RDKit, PyTorch | BSD-3-Clause. |
| pymbar, e3nn, [HPacker](https://github.com/gvisani/hpacker/blob/main/LICENSE) | MIT. |
| dimorphite-dl | Apache-2.0. |
| gemmi | MPL-2.0. |
| Biopython | Biopython License (the pinned 1.81 package). |
| NumPy, OpenBLAS | BSD-3-Clause. |
| Matplotlib | PSF-based license, with third-party notices. |
| [BioEmu](https://github.com/microsoft/bioemu/blob/main/LICENSE), [Boltz-2](https://github.com/jwohlwend/boltz/blob/main/LICENSE) | MIT for the software. Check the accompanying terms for downloaded model weights and data. |
| [PyMOL, open-source build](https://github.com/schrodinger/pymol-open-source/blob/master/LICENSE) | PyMOL Open-Source License. |
| [MODELLER](https://salilab.org/modeller/registration.html) | Proprietary. Requires a registered license key. Free use is limited to eligible academic nonprofit institutions. [Other users](https://salilab.org/modeller/nonacademic.html) need the applicable license. |

<details>
<summary><strong>Supporting Python packages</strong></summary>

| Package | License |
|---|---|
| PubChemPy, Pydantic, PyYAML, MCP Python SDK | MIT |
| HTTPX, python-dotenv | BSD-3-Clause |
| Requests, rustworkx | Apache-2.0 |
| pyxdg | LGPL-2.0-only (locked package metadata) |

This is a summary of direct dependencies, not a complete inventory of every
transitive dependency or operating-system library.

</details>

### Containers and redistribution

Container images combine MDClaw with separately licensed packages. MDClaw's
MIT license does not relicense those packages. Consult their copyright notices,
license files and source-distribution requirements when redistributing an image.
Conda package metadata is under `/opt/mdclaw/conda-meta/` in the images, and
Python package metadata and bundled notices are in the installed package files.

MODELLER is installed without a license key. Users supply their own
`KEY_MODELLER` at runtime under the applicable MODELLER terms. A runtime key
does not grant permission to redistribute MODELLER. Its
[license agreement](https://salilab.org/modeller/registration.html) requires
prior written permission for redistribution.
