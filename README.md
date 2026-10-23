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

Choose where the calculations will run. Then install the skills for your agent.

### On Your Workstation: Pixi

Install [Pixi](https://pixi.sh) once, then:

```bash
git clone https://github.com/matsunagalab/mdclaw.git
cd mdclaw
pixi install
export PATH="$PWD/bin:$PATH"
scripts/mdclaw-doctor.sh
```

Pixi installs the scientific dependencies from the committed lockfile. Use a
checkout outside cloud-synced folders: the large `.pixi/` environment contains
machine-specific files that should not be synced. Keep
`bin/` on your shell's PATH to use `mdclaw` from other project directories.
Doctor shows the selected environment, checks OpenMM and AmberTools, and
reports whether GPU execution and optional plugins are available. CPU-only
machines can prepare systems and run short checks; use a GPU for long MD.

### On A Compute Server: Shared SIF

Use the SIF file provided by your administrator. A SIF contains the packaged
scientific software; you do not need your own checkout or Pixi environment.
Replace the example path with the actual shared image:

```bash
export MDCLAW_SIF=/data/mdclaw.sif
singularity exec --nv "$MDCLAW_SIF" mdclaw --version
singularity exec --nv "$MDCLAW_SIF" mdclaw inspect_openmm_platforms
```

Use `apptainer` instead of `singularity` if that is the installed command.
Run GPU checks on a GPU node. See [server setup and Slurm](docs/agents/deployment.md#shared-lab-sif-no-user-checkout-required)
for working-directory mounts and job submission.

### Connect Your Agent

Inside this checkout, skills are already discoverable. For another project,
run this in that project's directory:

```bash
npx skills add matsunagalab/mdclaw
```

Select your agent and install `common` together with the workflow skills.
This installs instructions; it uses the runtime you prepared above.
Direct CLI use needs no skills.

[Deployment details](docs/agents/deployment.md) cover Claude Code plugins, Pi,
Docker, existing Conda environments, runtime selection, and troubleshooting.
BioEmu and Boltz-2 are optional, separate installs; see
[model backend setup](docs/developer/configuration.md#model-backend-runtime).
Image building and publishing are covered in the
[maintainer release guide](docs/developer/release.md).

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
