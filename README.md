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
source -> prep -> solv -> topo -> min -> eq -> prod -> analyze
```

The CLI manages node state and passes artifacts between stages. A later agent
can use `inspect_job` to resume from completed work, `explain_node` to validate
the next node before execution, and `trace_failure` to choose a new recovery
branch without rewriting terminal history.

The request controls how far the agent proceeds. MDClaw can stop after planning
or any named stage, or continue through analysis when the user explicitly asks
for an evidence-backed scientific answer. It does not treat a plan as permission
to run every stage or submit HPC jobs.

## Install / Deploy

MDClaw needs two things:

1. **Agent skills** from `skills/`, unless you use the CLI directly.
2. **One scientific runtime** containing the `mdclaw` CLI, AmberTools, OpenMM,
   and the Python dependencies.

### Fastest Setup From A Checkout

Install Singularity/Apptainer or Docker first. For Conda instead, create the
environment under [Choose One Runtime](#choose-one-runtime) before running this
block; the setup script will detect and reuse it.

```bash
git clone https://github.com/matsunagalab/mdclaw.git
cd mdclaw

# Expose the skills to repo-local agent harnesses.
scripts/install-agent-skills.sh

# Reuse a conda env named mdclaw, or pull the matching SIF/Docker image.
scripts/setup-container.sh

# Make the runtime-selecting wrapper available in this shell.
export PATH="$PWD/bin:$PATH"

mdclaw --version
mdclaw --list-json bootstrap_md_workflow
scripts/mdclaw-doctor.sh
```

`scripts/setup-container.sh` does not rebuild anything. It reuses an existing
`mdclaw` conda environment; otherwise it downloads the version-matched runtime
from `ghcr.io/matsunagalab/mdclaw` using Singularity/Apptainer or Docker.

### Install Skills For Your Agent

| Agent or use case | Skill setup | Runtime setup |
|---|---|---|
| Claude Code plugin | `/plugin marketplace add matsunagalab/mdclaw`, then `/plugin install mdclaw@mdclaw` | Session-start hook runs the packaged-runtime setup |
| Pi | `pi install git:github.com/matsunagalab/mdclaw@main` | Provide one runtime below; Pi installs skills only |
| Codex, OpenCode, generic repo-local agents | `scripts/install-agent-skills.sh` | Provide one runtime below |
| Direct CLI or development | Skills are optional | Use Conda or a packaged runtime below |

The installer creates discovery mirrors under `.agents/skills`,
`.claude/skills`, and `.codex/skills`; `skills/` remains the only source of
truth. Use `scripts/install-agent-skills.sh --copy` on filesystems that do not
support symlinks.

### Choose One Runtime

**Conda: local development or a controlled workstation**

```bash
conda env create -f environment.yml
export MDCLAW_RUNTIME=conda
export PATH="$PWD/bin:$PATH"
mdclaw --list-json bootstrap_md_workflow
```

`environment.yml` installs this checkout in editable mode. The wrapper invokes
it with `conda run`, so activation is optional.

**Singularity/Apptainer SIF: recommended for Linux HPC**

```bash
singularity pull mdclaw.sif \
  docker://ghcr.io/matsunagalab/mdclaw:latest
export MDCLAW_SIF="$PWD/mdclaw.sif"
export MDCLAW_RUNTIME=singularity
export PATH="$PWD/bin:$PATH"
mdclaw --list-json bootstrap_md_workflow
```

Use `apptainer pull` and `MDCLAW_RUNTIME=apptainer` instead when that is the
installed command.

**Docker: desktop or workstation container runtime**

```bash
docker pull ghcr.io/matsunagalab/mdclaw:latest
export MDCLAW_RUNTIME=docker
export MDCLAW_DOCKER_IMAGE=ghcr.io/matsunagalab/mdclaw:latest
export PATH="$PWD/bin:$PATH"
mdclaw --list-json bootstrap_md_workflow
```

`bin/mdclaw` selects exactly one runtime for each call. The order is an explicit
`MDCLAW_RUNTIME=conda|singularity|apptainer|docker` override, then conda env
`mdclaw`, SIF, Docker, and finally a local `mdclaw` executable. Set the override
when more than one is installed and you need predictable selection. Container
calls bind the current working directory at the same absolute path, so run from
the study/project directory and keep inputs below it.

Troubleshooting and less common deployment layouts are in
`docs/agents/deployment.md`; image and SIF details are in
`docs/developer/container.md`.

### AI Model Backends (BioEmu, Boltz-2)

BioEmu (monomer conformational ensembles) and Boltz-2 (structure prediction,
pinned to 2.2.1) are optional. They are **not** in the core conda environment or
container because their Torch/CUDA stacks are independent. Install only the
backend you need:

```bash
mdclaw setup_model_backend --model bioemu --device cuda
mdclaw setup_model_backend --model boltz  --device cuda
mdclaw check_model_backend  --model bioemu
mdclaw check_model_backend  --model boltz
```

On a read-only SIF, point `MDCLAW_SURROGATE_DIR` at a writable (ideally shared)
filesystem and bind-mount it so the venv and model weight caches persist.

BioEmu can then generate a monomer conformational ensemble as a source bundle:

```bash
mdclaw generate_surrogate_candidates \
  --model bioemu \
  --amino-acid-sequence YYDPETGTWY \
  --num-samples 100 \
  --max-candidates 20 \
  --job-dir <job_dir> \
  --node-id source_001
```

The generated candidates are recorded in the source node's `source_bundle.json`
with `source_type="surrogate"` and can be consumed by
`prepare_complex --source-candidate-id candidate_NNN`. Boltz-2 is driven through
the `boltz-predict` skill / `boltz2_protein_from_seq` tool once its backend is
installed. See `docs/developer/model-backends.md` for the registry contract and
how to add or swap a model.

## Ask In Plain Language

Users do not need to remember command names. The framing of your request
decides where MDClaw stops. There are three patterns:

**Plan only.** Ask the agent to plan a study. It records a lightweight
`study_plan.json` (question, MD goal, planned jobs, observables, decision
criteria) and stops so you can review before any system is built.

```text
Plan an MD study for the PSD-95 PDZ3 domain bound to the CRIPT peptide
(PDB 1BE9). Test whether the H372A mutation weakens dynamic coupling between
the distal alpha-3 helix and the peptide-binding groove. Define the WT and
mutant jobs, peptide-contact and groove-dynamics observables, and decision
criteria.
```

**Run to a stage or resume.** Name the last stage you need: preparation,
equilibration, production, or analysis. For an existing study or job, state
the new purpose; MDClaw inspects the DAG, reuses completed artifacts, and
continues only as far as that request requires. A direct one-system run still
gets a thin study record with one `jobs/main` job.

```text
Prepare PDB 1AKE chain A as a protein-only explicit-water system using the
default force field and water model. Continue through default equilibration
and stop before production.
```

**Scientific answer.** Explicitly ask the agent to answer the scientific
question using MD. It advances every required job through the planned
analysis, packages evidence, applies the decision criteria, and returns a
supported conclusion.

```text
Set up and run an apo-vs-holo MD study for the T4 lysozyme L99A
benzene-binding cavity (benzene-bound PDB 4W53). Test whether benzene
occupancy stabilizes the engineered hydrophobic cavity. Plan the minimal
job set, prepare and equilibrate it, run 50 ns of production per job,
analyze cavity hydration and ligand-pose observables, and return an
evidence-backed conclusion.
```

Good prompts for **planning** state the scientific question, comparison
groups, and what evidence would answer the question. Good prompts for a
**stage or resume** name the target study or structure and the last required
stage. Good prompts for a **scientific answer** add a production length,
replicate count, observables, and the conclusion to be supported. If required
work remains queued or running, MDClaw reports a resumable DAG handoff instead
of claiming completion. HPC/SLURM submission occurs only when the current
request explicitly asks for it.

## Repository Map

| Path | Role |
|---|---|
| `skills/` | Portable MDClaw skills. This is the source of truth for skill behavior. |
| `.agents/skills/` | Generic Agent Skills discovery entries, symlinked to `skills/`. |
| `.claude/skills/` | Repo-local Claude Code skill discovery entries, symlinked to `skills/`. |
| `.claude-plugin/` | Claude plugin marketplace metadata. |
| `hooks/` | Plugin lifecycle hooks, including packaged runtime setup. |
| `bin/mdclaw` | Runtime wrapper used by plugin and local deployments. |
| `mdclaw/` | Python package and CLI tool implementations. |
| `container/` | Docker image and Singularity/Apptainer SIF build assets for the packaged MD runtime. |
| `benchmarks/mdprepbench/` | Preparation workflow benchmark tasks and scorer contracts. |
| `benchmarks/mdstudybench/` | Scientific question and study-bundle benchmark tasks. |
| `docs/agents/` | Deployment notes for agent harnesses. |
| `docs/developer/` | Architecture, CLI internals, testing, release, and tool references. |
| `tests/` | Unit, smoke, benchmark, and integration tests. |

## Supported Capabilities

| Area | Current support |
|---|---|
| Study and workflow | Minimal one-system plans and multi-job comparative studies; resumable per-job DAGs; immutable completed/failed nodes; explicit branches for variants and recovery. |
| Structure sources | PDB, AlphaFold/UniProt, and local PDB/mmCIF; biological assemblies and multi-model source bundles; optional source generation with Boltz-2, BioEmu, and MODELLER. |
| Inspection and selection | Proteins, DNA/RNA, ligands, waters, bare ions, glycans, PTMs, chain identity, source candidates, and covalent protein-glycan connectivity before preparation. |
| Protein and nucleic force fields | Amber ff19SB by default (ff14SB available), standard DNA OL15, and standard RNA OL3. Nucleic terminal charge corrections are derived from the selected force-field XML. |
| Ligands, glycans, and PTMs | Noncovalent small molecules through GAFF2 with OpenFF NAGL charges and AM1-BCC fallback; covalently linked glycans through GLYCAM; SEP/TPO/PTR phosphorylation parameters. |
| Solvent and membranes | Explicit water (ff19SB + OPC, 15 A buffer, and 0.15 M salt by default), implicit solvent, vacuum, and Lipid21 membranes. The patch-tile membrane path supports selected neutral and anionic mixtures, including POPC/POPG, and derives lipid templates and charges from the active XML files. |
| Simulation | Standalone minimization, staged NVT/NPT equilibration, production MD, HMR (4 fs production default), XML-state restart, extensions, replicates, and optional PythonTorchForce custom bias/CV scripts. |
| Analysis and evidence | Trajectory concatenation/fitting, RMSD, RMSF, distances, contact frequencies, Q values, equilibration detection, custom-result registration, provenance reports, Methods-style reports, and study-level evidence packages. |
| Execution | Local Conda, Docker, Singularity/Apptainer SIF, explicit SLURM submission/monitoring, and homogeneous job arrays. |
| Evaluation | Agent-agnostic MDPrepBench and MDStudyBench runners, public task packaging, raw-artifact validation, and scoring. |

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
- **Advanced sampling and free-energy methods are not turnkey pipelines.** The
  custom-force interface can run a user-defined bias, but MDClaw does not yet
  provide complete alchemical FEP/TI, replica-exchange, metadynamics, or
  umbrella-sampling/PMF campaign automation and validation.
- **One job has one structural source identity.** Its source bundle may contain
  many candidates, but `prep` selects one. Put independent starting systems in
  separate study jobs; branch a prepared job for mutations, ligands, protocols,
  temperatures, or seeds.
- **Guardrails are not scientific proof.** Passing topology, charge, energy,
  and artifact checks does not establish convergence, adequate sampling, or a
  biological conclusion. Those require appropriate controls, replicates,
  analysis, and expert review.

## Benchmarking

MDClaw includes two artifact-based benchmark suites under the MDAgentBench
family:

- `MDPrepBench-v0.3` in `benchmarks/mdprepbench/`: a 40-task preparation
  workflow battery covering ligand/chain selection, residue protonation, PTMs,
  glycans, nucleic acids, membranes, assemblies, ion concentration, metal
  cofactors (zinc, non-zinc Mn/Ca), custom drug-like ligand parameterization,
  protein-protein and protein-DNA complexes, side-chain reconstruction, and
  backend-neutral raw OpenMM artifact validation.
- `MDStudyBench-v0.2` in `benchmarks/mdstudybench/`: four uniform-load
  scientific-answer and auditable study-bundle comparisons spanning
  destabilizing, weakened-binding, stabilizing, and ligand-affinity directions,
  so a constant prior cannot win.

Both suites are agent-agnostic: evaluated agents read `prompt.md` and write
`submission/`; the scorer reads `task.json`, scorer-only truth files, and
submitted artifacts. This is deliberately MDClaw-free on the solve side: an
agent may use MDClaw, direct OpenMM scripts, another MD-prep stack, or a custom
runner, as long as the submitted artifacts satisfy the public contract. Keep
submissions slim. The scorer derives properties such as model/assembly choice,
net charge, ion molarity, water model, and component presence from the submitted
OpenMM bundle and structures instead of trusting self-reported metrics or
free-form explanations. Public benchmark tasks do not require MDClaw-specific
guardrail codes; scientific MD reasoning lives in MDStudyBench, kept small and
curated rather than mixed back into MDPrepBench.

User-facing benchmark requests should stay short:

```text
MDPrepBenchを run_id=prep_full_run で実行して評価して
```

```text
MDPrepBenchの P11_prep_site_protonation_t4l_glu11 だけを実行して評価して
```

Use `mdclaw run_benchmark_agent` for automated agents, or
`mdclaw prepare_benchmark_run` to create agent-safe task packages and score the
finished submissions separately with the canonical scorer.

### MDPrepBench

Create a run workspace from the repository root:

```bash
mdclaw prepare_benchmark_run \
  --output-dir benchmark_runs \
  --run-id prep_full_run \
  --dataset-dir benchmarks/mdprepbench \
  --execution-mode lite
```

To run only a small subset:

```bash
mdclaw prepare_benchmark_run \
  --output-dir benchmark_runs \
  --run-id prep_p11 \
  --dataset-dir benchmarks/mdprepbench \
  --execution-mode lite \
  --task-ids P11_prep_site_protonation_t4l_glu11
```

Give the evaluated agent the per-task
`benchmark_runs/<run_id>/tasks/<task_id>/agent_prompt.md`, or the task entries
listed in `benchmark_runs/<run_id>/agent_tasks.json`. Each task instruction
points to agent-safe `prompt.md`, `submission_contract.json`,
`submission_checklist.md`, and target `submission/` paths. Do not give the
agent `harness_tasks.json`, `harness_instructions.json`, canonical `task.json`,
`truth/`, or `scorer/`. Each evaluated agent should solve only its current task;
do not ask it to inspect the full suite or write a benchmark-wide solver script.
`run_id` is only a label; do not infer smoke-test shortcuts or task subsets from
words in it. Task-local Python helpers should run via
`conda run -n mdclaw python ...`, and agents should retry failed workflow steps
with new MDClaw nodes rather than rerunning or deleting terminal nodes.

For normal MDClaw DAG runs, create a `min` node after topology and run:

```bash
mdclaw --job-dir <job_dir> --node-id min_001 run_minimization
```

The `min` node writes `minimized_structure.pdb`, `minimized.xml`, and
`minimization_report.json`; downstream `eq` nodes should parent from `min_001`.

For MDPrepBench v0.3, submit the completed minimized state as
`topology/state.xml`; the evaluator derives the minimized PDB view and report.

For non-MDClaw solvers, package an already-built OpenMM artifact triple with
the standalone helper instead of importing MDClaw into the solver:

```bash
python benchmarks/tools/package_submission.py \
  --submission-dir <submission_dir> \
  --task-id <task_id> \
  --system-xml <system.xml> \
  --topology-pdb <topology.pdb> \
  --state-xml <state.xml> \
  --prepared-structure <prepared_structure.pdb>
```

Add task-specific raw artifacts only when the public contract asks for them,
for example `--extra-output wt_prepared_structure.pdb=<source_file>` for a
branching task.

After the agent writes the task `submission/` directories, evaluate the run:

```bash
mdclaw score_benchmark_run \
  --run-dir benchmark_runs/<run_id> \
  --dataset-dir benchmarks/mdprepbench
```

This writes per-task `validation.json` / `score.json` files and a run-level
`summary.json`.

For full-suite comparisons across the local Pi, Claude Code, and Codex CLIs, run
the operator wrapper. It launches one scored run per agent:

```bash
conda run -n mdclaw python benchmarks/tools/run_mdprepbench_all_agents.py \
  --output-dir benchmark_runs \
  --run-id-prefix 20260702_mdprepbench_all \
  --agents pi claude-code codex \
  --jobs 5 --gpus 4 --repeats 3
```

- `--jobs N` runs N tasks per agent concurrently; `--gpus M` (when > 0)
  round-robins `CUDA_VISIBLE_DEVICES` across those tasks. Both pass straight
  through to `mdclaw run_benchmark_agent`, so a parallel run still yields one
  scored `summary.json` per agent.
- `--repeats R` runs each agent R times (`<prefix>_<agent>_rep1..repR`) and
  writes per-agent `mean` / `stdev` of the overall score into the
  `*_all_agents_operator_summary.json` `aggregates` block.
- `--agent-model AGENT=MODEL` overrides the model per harness; `--dry-run`
  prints the generated commands without launching agents; `--task-ids <id>`
  runs a short smoke subset.
- `--agent-skills-dir skills` runs an explicit skill-enabled condition. The
  wrapper selects `pi-user` for Pi unless `--agent-profile pi=...` overrides it.

### MDStudyBench

MDStudyBench uses the same run/evaluate tools with the study dataset. For the
full four-task curated suite:

```bash
mdclaw prepare_benchmark_run \
  --output-dir benchmark_runs \
  --run-id study_full_run \
  --dataset-dir benchmarks/mdstudybench \
  --execution-mode lite \
  --judge-mode llm_judge
```

After submissions are written, evaluate with:

```bash
mdclaw score_benchmark_run \
  --run-dir benchmark_runs/<run_id> \
  --dataset-dir benchmarks/mdstudybench
```

All four tasks expect comparative WT/mutant (or paired-ligand) MD evidence with
index-aligned `outputs.trajectories` / `outputs.topology`; the scorer reloads the
trajectories and verifies the substitution, so a literature guess without real MD
scores zero. When `run_config.json` selects `judge_mode=llm_judge`,
`score_benchmark_run` auto-runs the judge for tasks that declare rubrics.
Deterministic mode neither launches nor consumes judge files.

To run every agent over the suite, use the study wrapper. It shares the
MDPrepBench operator flags (`--agents`, `--jobs`, `--gpus`, `--repeats`,
`--agent-model`, `--dry-run`), and defaults to each task's declared 24 h budget
and `--judge-mode llm_judge`:

```bash
conda run -n mdclaw python benchmarks/tools/run_mdstudybench_all_agents.py \
  --output-dir benchmark_runs \
  --run-id-prefix 20260702_mdstudybench_all \
  --agent-skills-dir skills \
  --jobs 4 --gpus 4
```

For an external agent or runner that should receive only public files, export
the agent-visible package first:

```bash
mdclaw export_benchmark_public_package \
  --dataset-dir benchmarks/mdprepbench \
  --output-dir benchmark_public/mdprepbench

mdclaw export_benchmark_public_package \
  --dataset-dir benchmarks/mdstudybench \
  --output-dir benchmark_public/mdstudybench
```

The exported package contains prompts, submission contracts, and
submission-facing schemas only; it omits `task.json`, `truth/`, and `scorer/`.

See `benchmarks/README.md` for suite layout, `docs/benchmark/README.md` for
MDPrepBench details, and `docs/benchmark/mdstudybench.md` for StudyBench tasks.

## Developer Quickstart

```bash
conda env create -f environment.yml   # installs the mdclaw CLI editable (-e .)
conda activate mdclaw
ruff check mdclaw/
pytest tests/test_mcp_server.py tests/test_cli.py tests/test_guardrails.py tests/test_slurm_server.py -v
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

Follow `docs/developer/release.md`. Version tags must stay synchronized across
the Python package, plugin metadata, marketplace metadata, and container image.

Users update the plugin with:

```text
/plugin update mdclaw@mdclaw
```

## License

MIT
