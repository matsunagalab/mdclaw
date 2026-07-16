<p align="center">
  <img src="docs/assets/mdclaw-logo.png" alt="MDClaw logo" width="720">
</p>

# MDClaw

MDClaw provides skills and CLIs for vibe-MD (Molecular Dynamics) simulations and autonomous
scientific investigation in the Amber/OpenMM ecosystem. It helps an AI agent
turn scientific intent into reproducible atomistic work: prepare systems, run
equilibration and production MD, analyze trajectories, branch hypotheses, and
package evidence with provenance.

## What MDClaw Can Do

- Turn a scientific question into a study plan (observables and decision
  criteria) that organizes one or more job DAGs, then, when requested, advance
  them to a named stage or an evidence-backed scientific answer.
- Prepare MD systems from PDB IDs, AlphaFold/UniProt entries, or local
  structure files.
- Generate monomer conformational source ensembles from MD surrogate models
  such as BioEmu, then hand selected candidates to the standard MD workflow.
- Inspect chains, ligands, waters, ions, glycans, DNA/RNA, and modified
  residues before committing to a setup.
- Clean structures, preserve selected ligands when safe, solvate systems, and
  assign Amber/OpenMM force fields.
- Build OpenMM-ready topology artifacts, then run equilibration and production
  MD with restartable state files.
- Branch workflows for mutations, PTMs, ligand choices, solvent models,
  temperatures, seeds, and protocols.
- Run locally, through containers, or on SLURM/HPC systems.
- Analyze trajectories and package reproducible evidence, provenance, figures,
  and Methods-style reports.
- Evaluate MD agents with the included MDPrepBench / MDStudyBench datasets and scorer.

## Install / Deploy

MDClaw has two independent layers. Setting them up separately avoids most
deployment confusion:

1. **Skills** — portable text under `skills/` that your agent reads. Installed
   by your agent entry point.
2. **MD runtime** — the `mdclaw` CLI plus AmberTools/OpenMM, provided by a conda
   env, a Singularity/Apptainer SIF, a Docker image, or a local install.

Pick an entry point (it installs the skills), then make one runtime available:

| Agent | Install skills with | Runtime |
|---|---|---|
| Claude Code plugin | `/plugin install mdclaw@mdclaw` | Plugin hook auto-picks conda/SIF/Docker |
| Pi | `pi install git:github.com/matsunagalab/mdclaw@main` | One runtime (below) |
| Codex, OpenCode, repo-local agents | `scripts/install-agent-skills.sh` | One runtime (below) |
| Direct CLI / development | (skills optional) | conda env (below) |

### Runtime

Everything except the plugin needs one runtime. The two common setups:

```bash
# skills + conda (local dev / workstation) — also installs the mdclaw CLI
conda env create -f environment.yml

# skills + SIF (HPC)
export MDCLAW_SIF=/path/to/mdclaw.sif
```

`bin/mdclaw` then auto-selects a runtime per call, in order: `MDCLAW_RUNTIME`
override, conda env `mdclaw`, SIF (`MDCLAW_SIF`), Docker
(`ghcr.io/matsunagalab/mdclaw`), then `mdclaw` on `PATH`. It binds the current
working directory at the same absolute path inside containers, so run `mdclaw`
from your project or job directory and paths resolve the same on host, Docker,
and Singularity/Apptainer.

Verify a checkout with `scripts/mdclaw-doctor.sh`. Full deployment matrix and
per-harness detail live in `docs/agents/deployment.md`; container specifics in
`docs/developer/container.md`.

### AI Model Backends (BioEmu, Boltz-2)

BioEmu (MD surrogate ensembles) and Boltz-2 (structure prediction, pinned to
2.2.1) are heavy AI models with their own Torch/CUDA stacks. They are **not**
part of the core runtime and are **not** baked into the container image. A
generic `VenvBackend` registry installs each into an isolated venv on first use,
keeping it out of the conda `mdclaw` environment:

```bash
mdclaw setup_model_backend --model bioemu --device cuda
mdclaw setup_model_backend --model boltz  --device cuda
mdclaw check_model_backend  --model bioemu
mdclaw check_model_backend  --model boltz
```

Backends declare capabilities (`supports_sampling`, `supports_prediction`)
rather than being hard-wired by name, so callers dispatch on what a model can do
and predictors stay swappable. `setup_surrogate_backend` /
`check_surrogate_backend` remain as `bioemu`-defaulted aliases.

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

## What MDClaw Can And Cannot Do

### Can Do

- Protein systems with Amber ff19SB (ff14SB available) in OpenMM.
- Explicit solvent, defaulting to OPC, a 15 A buffer, and 0.15 M salt, with
  neutralizing and excess ions.
- Standard DNA/RNA through the OL15/OL3 XMLs.
- Small-molecule ligands via openmmforcefields `GAFFTemplateGenerator` (GAFF2),
  with OpenFF NAGL partial charges and AM1-BCC as fallback.
- Phosphoserine / phosphothreonine / phosphotyrosine PTMs (SEP/TPO/PTR), with
  `phosaa` parameters auto-loaded in `build_amber_system`.
- Membrane embedding (patch-tile backend, Lipid21) for PC / PE / cholesterol
  compositions.
- Mutations, glycan/glycoprotein inspection, and multi-model / assembly /
  prediction source bundles that resolve to one prepared candidate.
- Minimization, multi-stage equilibration, and HMR production (4 fs default)
  with XML-state restart and extension.
- Branching variants: mutants, ligands, protocols, temperatures, and seeds.
- AI structure input: Boltz-2 prediction and BioEmu conformational ensembles
  (isolated model backends).
- Local, container (Docker, Singularity/Apptainer SIF), and SLURM/HPC execution,
  plus trajectory analysis and reproducible evidence packaging.

### Cannot Do (Out Of Scope Or Guarded)

- Modified / non-standard DNA/RNA bases: inspection reports them as unsupported
  and topology generation stops with a structured code rather than silently
  mapping them to ordinary nucleotides.
- PTMs beyond SEP/TPO/PTR (phospho-histidine, O-GlcNAc, acetylation,
  methylation, ubiquitination, lipidation, and user-selectable phosphate
  protonation states) are deferred.
- Anionic lipid mixtures such as `DOPE:DOPG` pack and build a valid topology but
  currently segfault during equilibration, so they are excluded from the
  defaults.
- Unsafe or ambiguous force-field conversion and parameterization paths are
  deliberately guarded: tools return structured error codes instead of silently
  building a dubious system.
- Multiple independent structural source roots in one job are out of scope,
  because they make input resolution and system identity ambiguous. Compare
  variants by branching within one job DAG instead.
- Not a general-purpose engine for arbitrary force fields, CHARMM-native setups,
  coarse-grained models, polarizable force fields, or QM/MM.

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
