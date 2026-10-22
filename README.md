<p align="center">
  <img src="docs/assets/mdclaw-logo.png" alt="MDClaw logo" width="720">
</p>

# MDClaw

MDClaw provides agent skills and CLI tools for molecular dynamics (MD) and
autonomous scientific investigation in the Amber/OpenMM ecosystem. It helps an
AI agent turn scientific intent into reproducible atomistic work: plan a study,
prepare systems, run MD, analyze trajectories, branch hypotheses, and package
evidence with provenance.

MDClaw is not one hidden end-to-end planner. Its responsibilities are explicit:

- **Skills** translate scientific intent into an MD procedure.
- **CLI tools** execute concrete operations and record their outputs.
- **A durable DAG** is the source of truth for progress, artifacts, failures,
  branching, and re-entry by another agent.

## How It Works

Every run is a small study, even when it contains only one system. A study can
hold one or more job DAGs, and each job follows the same artifact handoff:

```text
source -> prep -> solv -> topo -> min -> eq -> {prod | fep} -> analyze
```

Each job carries exactly one structural source identity: independent starting
systems are separate jobs in the same study, while branches inside a job
derive variants (mutations, ligands, protocols, temperatures, seeds) from its
prepared structure.

The CLI manages node state and passes artifacts between stages. A later agent
can use `inspect_job` to resume from completed work, `explain_node` to validate
the next node before execution, and `trace_failure` to choose a new recovery
branch without rewriting terminal history.

The request controls how far the agent proceeds. MDClaw can stop after planning
or any named stage, or continue through analysis when the user explicitly asks
for an evidence-backed scientific answer. It does not treat a plan as permission
to run every stage or submit HPC jobs.

## Install / Deploy

MDClaw needs two things, managed separately:

1. **Agent skills** from `skills/`, unless you use the CLI directly.
2. **One scientific runtime** containing the `mdclaw` CLI, AmberTools, OpenMM,
   and the Python dependencies: a [Pixi](https://pixi.sh) environment, a
   Singularity/Apptainer SIF, or a Docker image.

### Quickstart From A Checkout

Repo-local agents need no skill installer: the checkout already tracks the
discovery mirrors (`.agents/skills`, `.claude/skills`). Provide a runtime and
verify:

```bash
git clone https://github.com/matsunagalab/mdclaw.git
cd mdclaw

# Local env (recommended on a workstation): locked install in seconds.
pixi install
# ...or packaged runtime: downloads the version-matched image from
# ghcr.io/matsunagalab/mdclaw (SIF preferred, Docker fallback), skipping
# the pull when a local pixi or conda env already exists. Nothing is rebuilt.
scripts/setup-container.sh

# Put the runtime-selecting wrapper on PATH and verify.
export PATH="$PWD/bin:$PATH"
mdclaw --version
scripts/mdclaw-doctor.sh
```

### Install Skills For Your Agent

MDClaw skills follow the [Agent Skills](https://agentskills.io) standard, so
one command installs them for any supported harness — Claude Code, Codex,
Cursor, OpenCode, and others ([skills CLI](https://github.com/vercel-labs/skills),
Node 22.20+). Run it in the project the agent works in and select the skills
and target agents when prompted:

```bash
npx skills add matsunagalab/mdclaw
```

Two agent-native setups install more than the skill text and replace the
command above where they apply:

| Agent | Setup | What it adds |
|---|---|---|
| Claude Code plugin | `/plugin marketplace add matsunagalab/mdclaw`, then `/plugin install mdclaw@mdclaw` | `/mdclaw:*` commands and a session-start hook that prepares the packaged runtime |
| Pi | `pi install git:github.com/matsunagalab/mdclaw@main` | Pi-native skill management (runtime still provided separately) |

Every installer creates discovery mirrors (e.g. `.agents/skills`,
`.claude/skills`); `skills/` remains the only source of truth. Use
`npx skills add matsunagalab/mdclaw --copy` on filesystems that do not
support symlinks. Skills are optional when driving the CLI directly.

### Runtime Selection

`bin/mdclaw` selects exactly one runtime for each call. The order is an explicit
`MDCLAW_RUNTIME=pixi|conda|singularity|apptainer|docker` override, then the
checkout pixi env (`.pixi/envs/default`), conda env `mdclaw`, SIF, Docker, and
finally a local `mdclaw` executable. Set the override when more than one is
installed and you need predictable selection. Container calls bind the current
working directory at the same absolute path, so run from the study/project
directory and keep inputs below it.

Per-runtime setup — an administrator-provided shared SIF with no checkout,
Pixi for development, a SIF on Linux HPC, Docker on a desktop, legacy Conda —
plus verification and troubleshooting live in
[docs/agents/deployment.md](docs/agents/deployment.md). Image build and
publishing details are in `docs/developer/container.md`.

### AI Model Backends (BioEmu, Boltz-2)

BioEmu (monomer conformational ensembles) and Boltz-2 (structure prediction,
pinned to 2.2.1) are optional. They are **not** in the core runtime environment
or container because their Torch/CUDA stacks are independent. Install only the
backend you need:

```bash
mdclaw setup_model_backend --model bioemu --device cuda
mdclaw setup_model_backend --model boltz  --device cuda
mdclaw check_model_backend  --model bioemu
mdclaw check_model_backend  --model boltz
```

On a read-only SIF, point `MDCLAW_SURROGATE_DIR` at a writable (ideally shared)
filesystem and bind-mount it so the venv and model weight caches persist.

Once a backend is installed, the agent drives it through the skills: BioEmu
monomer ensembles via `bioemu-sample` (`generate_surrogate_candidates`,
recorded as a source bundle for preparation), Boltz-2 predictions via
`boltz-predict` (`boltz2_protein_from_seq`). See
`docs/developer/model-backends.md` for the registry contract and how to add
or swap a model.

## Ask In Plain Language

Users do not need to remember command names. Describe the work; MDClaw stops
where your request stops. There are two shapes:

**One system (a single job).** Name the structure and the last stage you
need — preparation, equilibration, production, or analysis. A direct run
still gets a thin study record with one `jobs/main` job, so a later agent can
inspect and resume it.

```text
Prepare PDB 1AKE chain A as a protein-only explicit-water system using the
default force field and water model. Continue through default equilibration
and stop before production.
```

**A scientific question (a study).** State the question, the comparison
groups, and what evidence would answer it. MDClaw plans the jobs and runs
only as far as you ask: stop at the plan for review, stop at a named stage,
or continue through analysis to an evidence-backed conclusion.

```text
Set up and run an apo-vs-holo MD study for the T4 lysozyme L99A
benzene-binding cavity (benzene-bound PDB 4W53). Test whether benzene
occupancy stabilizes the engineered hydrophobic cavity. Plan the minimal
job set, prepare and equilibrate it, run 50 ns of production per job,
analyze cavity hydration and ligand-pose observables, and return an
evidence-backed conclusion.
```

In both shapes a plan is never permission to run everything: HPC/SLURM
submission happens only when the request asks for it, and work still queued
or running is reported as a resumable DAG handoff instead of claimed as
complete.

## Repository Map

| Path | Role |
|---|---|
| `skills/` | Portable MDClaw skills. This is the source of truth for skill behavior. |
| `.agents/skills/` | Generic Agent Skills discovery entries, symlinked to `skills/`. |
| `.claude/skills/` | Repo-local Claude Code skill discovery entries, symlinked to `skills/`. |
| `.claude-plugin/` | Claude plugin marketplace metadata. |
| `hooks/` | Plugin lifecycle hooks, including packaged runtime setup. |
| `bin/mdclaw` | Runtime wrapper used by plugin and local deployments. |
| `pixi.toml`, `pixi.lock` | Locked definition of the scientific runtime (dev env and container Stage 1). |
| `environment.yml` | Legacy conda definition, kept for existing envs and the arm64 image. |
| `.github/workflows/` | CI (pixi env tests on macOS/Linux) and the container build/publish workflow. |
| `mdclaw/` | Python package and CLI tool implementations. |
| `container/` | Docker image and Singularity/Apptainer SIF build assets for the packaged MD runtime. |
| `docs/agents/` | Deployment notes for agent harnesses. |
| `docs/developer/` | Architecture, CLI internals, testing, release, and tool references. |
| `tests/` | Unit, smoke, and integration tests. |

## Supported Capabilities

| Area | Current support |
|---|---|
| Study and workflow | Minimal one-system plans and multi-job comparative studies; resumable per-job DAGs; immutable completed/failed nodes; explicit branches for variants and recovery. |
| Structure sources | PDB, AlphaFold/UniProt, and local PDB/mmCIF; biological assemblies and multi-model source bundles; optional source generation with Boltz-2, BioEmu, and MODELLER. |
| Inspection and selection | Proteins, DNA/RNA, ligands, waters, bare ions, glycans, PTMs, chain identity, source candidates, and covalent protein-glycan connectivity before preparation. |
| Protein and nucleic force fields | Amber ff19SB by default (ff14SB available), standard DNA OL15, and standard RNA OL3. Nucleic terminal charge corrections are derived from the selected force-field XML. |
| Ligands, glycans, and PTMs | Noncovalent small molecules through GAFF2; ligand charges from OpenFF NAGL by default, with AM1-BCC selectable (`--ligand-charge-method`) or as automatic fallback; covalently linked glycans through GLYCAM; SEP/TPO/PTR phosphorylation parameters. |
| Solvent and membranes | Explicit water (ff19SB + OPC, 15 A buffer, and 0.15 M salt by default), implicit solvent, vacuum, and Lipid21 membranes. The patch-tile membrane path supports selected neutral and anionic mixtures, including POPC/POPG, and derives lipid templates and charges from the active XML files. |
| Simulation | Standalone minimization, staged NVT/NPT equilibration, production MD, HMR (4 fs production default), XML-state restart, extensions, replicates, and optional PythonTorchForce custom bias/CV scripts. |
| Free energy and enhanced sampling | Hybrid-topology FEP for point-mutation folding ddG, absolute binding free energy by double decoupling with Boresch restraints, weighted-ensemble kinetics with round-driven sampling schemes, metadynamics, and SST2 solute tempering. |
| Analysis and evidence | Trajectory concatenation/fitting, RMSD, RMSF, distances, contact frequencies, Q values, equilibration detection, custom-result registration, provenance reports, Methods-style reports, and study-level evidence packages. |
| Execution | Local Pixi or Conda environments, Docker, Singularity/Apptainer SIF, explicit SLURM submission/monitoring, and homogeneous job arrays. |
| Evaluation | Opt-in execution-record protocol for external benchmark harnesses: stage records appended to `$MDCLAW_BENCHMARK_HARNESS_LOG` (`mdclaw/_benchmark_log.py`). |

Support is fail-closed where MDClaw can check the contract. Ambiguous molecule
selection, missing force-field templates, inconsistent charges, invalid DAG
parents, or incomplete topology artifacts return stable error codes and preserve
the failed node for provenance instead of silently continuing.

## Boundaries And Non-Goals

- **No unrequested end-to-end execution.** A plan records intent; it is not
  permission to run production, analysis, or HPC submission. The current
  request sets the stopping point. Long studies can be handed to another agent
  and resumed from the DAG.
- **Modified DNA/RNA is not supported by the standard MD-ready topology path.**
  Inspection stops before an unsafe ordinary-nucleotide substitution. The
  legacy modXNA helper is experimental and does not make that path supported;
  a user-supplied OpenMM System/ForceField escape hatch remains possible.
- **PTM coverage is deliberately narrow.** Phospho-histidine, O-GlcNAc,
  acetylation, methylation, ubiquitination, lipidation, and selectable phosphate
  protonation states are not turnkey workflows.
- **Specialized chemistry still needs an explicit model.** MDClaw does not
  automatically create covalent-ligand parameters, bonded metal-center models,
  or general organometallic chemistry. Supported bare structural ions are
  checked against the selected water/ion force-field templates.
- **Force-field scope is Amber/OpenMM, not universal.** Arbitrary force fields,
  CHARMM-native preparation, coarse-grained or polarizable models, and QM/MM are
  outside the standard workflow.
- **Free-energy and enhanced-sampling scope is specific, not general.** The
  turnkey pipelines are hybrid-topology FEP for a single point mutation
  (`skills/md-fep`), absolute binding free energy of one neutral non-covalent
  ligand (`skills/md-abfe`), and weighted-ensemble kinetics (`skills/md-we`),
  plus metadynamics and SST2 solute-tempering runs with their analyses.
  Relative binding FEP between ligands, replica exchange, and
  umbrella-sampling/PMF campaign automation are not provided.
- **Guardrails are not scientific proof.** Passing topology, charge, energy,
  and artifact checks does not establish convergence, adequate sampling, or a
  biological conclusion. Those require appropriate controls, replicates,
  analysis, and expert review.

## Developer Quickstart

```bash
pixi install   # locked env from pixi.toml + pixi.lock (installs mdclaw editable)
pixi run ruff check mdclaw/
pixi run pytest tests/test_registry.py tests/test_cli.py tests/test_guardrails.py tests/test_guardrail_code_registry.py tests/test_slurm_server.py -v
```

Short agent guidance is mirrored in `CLAUDE.md` and `AGENTS.md`; keep those
files identical. Long-form references:

- `docs/developer/architecture.md`
- `docs/developer/tool-reference.md`
- `docs/developer/cli-internals.md`
- `docs/developer/testing.md`
- `docs/developer/configuration.md`
- `docs/developer/container.md`
- `docs/developer/release.md`

## Release

Follow `docs/developer/release.md`: `scripts/bump-version.sh X.Y.Z` writes
every version location at once, and pushing the `vX.Y.Z` tag triggers the
container workflow, which builds the image from `pixi.lock`, runs the CPU
smoke test, and publishes the GHCR tags. The GPU smoke test stays a manual
gate before shared SIFs are replaced.

Users update skills with `/plugin update mdclaw@mdclaw` (Claude Code plugin)
or `npx skills update` (skills CLI).

## License

MDClaw is MIT-licensed ([LICENSE](LICENSE)). This repository contains only
MDClaw's own code, skills, and documentation; no third-party source is
vendored, so the MIT text is the only license in the source tree.

### Third-Party Software

MDClaw orchestrates scientific software that is installed separately — from
conda-forge/PyPI via pixi or conda, or prebuilt inside the published
container image — and that remains under its own license. The main runtime
components (license identifiers as declared by the installed packages):

| Component | License |
|---|---|
| OpenMM | LGPL-3.0-or-later |
| AmberTools | GPL-3.0-or-later AND LGPL-3.0-or-later AND BSD-3-Clause AND MIT |
| MDAnalysis | LGPL-3.0-or-later |
| MDTraj | LGPL-2.1-or-later |
| ParmEd | LGPL-2.1-or-later |
| PLUMED (container image only) | LGPL-3.0 |
| propka | LGPL-2.1 |
| SST2 | GPL-2.0 (invoked as a separate subprocess) |
| RDKit, PyTorch | BSD-3-Clause |
| pdb2pqr | BSD-style |
| PDBFixer, openmmforcefields, OpenFF toolkit / NAGL / Pablo, pymbar, e3nn, HPacker | MIT |
| BioEmu, Boltz-2 (optional model backends) | MIT |
| dimorphite-dl, HOLE2 | Apache-2.0 |
| gemmi | MPL-2.0 |
| PyMOL (open-source build) | PyMOL Open-Source License |
| MODELLER | Proprietary; free for academic use with a registered license key |

The container/SIF images redistribute these components in binary form; the
license texts travel inside the image with each conda package
(`/opt/mdclaw/conda-meta/` and the packages' `share/` directories). Two
components need explicit care: MODELLER ships in the image **without** a
license key — each user supplies their own `KEY_MODELLER` at run time and
accepts the salilab academic-use terms — and SST2 (GPL-2.0) is executed as a
standalone subprocess rather than imported. MDClaw's MIT license covers only
MDClaw's own files; using or redistributing the images means honoring the
component licenses above.
