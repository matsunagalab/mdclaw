---
name: MD Benchmark
description: "Run MDAgentBench v1.0 evaluations for MDClaw or external MD agents. Use when creating benchmark runs, producing task submissions, validating/scoring submissions, or summarizing benchmark results."
---

# MD Benchmark

Read `skills/common/preamble.md` and `skills/common/tool-output.md` before
acting.

Use this skill to evaluate MD agents with the artifact-based MDAgentBench
v1.0 contract. The benchmark scores only submitted files, not chat history,
tool calls, or harness-specific logs.

MDClaw is only one possible backend. External agents, GROMACS workflows,
standalone OpenMM scripts, and lab-specific automation can participate if they
read only the public task files and write the standard `submission/` artifacts.
Use `mdclaw` commands below as the scorer/validator interface, not as a
requirement on the agent under test.

For automated comparisons across many harnesses and OpenRouter model slugs,
read and follow `skills/md-benchmark-openrouter/SKILL.md` in addition to this
skill. The long-form guide is `docs/benchmark/openrouter-harness-matrix.md`.

## Core Contract

Each task has:

- `task.json` — public task contract the agent may read.
- `input/` — agent-readable input files (PDB, mutation requests, MD
  protocols).
- `truth/` — **scorer-only** ground truth. The agent MUST NOT read this
  directory.
- `submission/` — artifact directory the agent produces.
- `score.json` — deterministic + ground-truth + (optional) LLM-judge score.
- `summary.json` — run-level aggregate produced by `summarize_benchmark_run`.

Default checked-in dataset: `benchmarks/mdagentbench/` (9 tasks,
schema_version `1.0`).

## Task Families And Intent Map

Use this map before reading an individual `task.json`. The family names are
human-facing intent labels; the scorer still reads `primary_score` and
`secondary_scores` from the task contract.

| Family | Agent Goal | Public Inputs To Inspect | Expected Submission Evidence | Tasks |
|---|---|---|---|---|
| System Preparation & Guardrails | Build MD-ready inputs when chemistry is supported; refuse when parameterization would be unsafe. | PDB files, ligand references, prep requests. | `prepared_structure.pdb`, topology artifacts, guardrail code, ligand RMSD metrics. | T02, T03 |
| Execution / Engine Reliability | Prove the MD engine can run, save, reload, and continue short simulations without corrupt trajectories. | PDB files, solvent specs, MD/restart protocols. | Trajectories, checkpoints/states, finite-energy/no-NaN metrics, restart-continuity metrics. | T01, T04, T05 |
| Scientific Answer vs Experimental Truth | Answer a mutation or binding-effect question using the allowed public evidence, with confidence and limitations. | Structure, mutation request, allowed citation pool. | `evidence_report.effect.direction`, confidence, limitations, provenance. | T06, T07 |
| Evidence & Methods Communication | Package analysis and methods so another scientist can audit the numeric claims and workflow choices. | Analysis request, study brief, structures or generated trajectories. | Figures, captions, `metrics.json`, `methods.md`, study provenance, limitations. | T08, T09 |

Task short names:

- `T01_engine_smoke`: Engine smoke MD.
- `T02_prep_metalloenzyme_guardrail`: Metal guardrail refusal.
- `T03_prep_ligand_pose_t4l_benzene`: Ligand-pose preserving preparation.
- `T04_exec_short_protein_md`: Short end-to-end protein MD.
- `T05_exec_restart_continue`: Restart continuation correctness.
- `T06_answer_stability_t4l_l99a`: Stability direction answer.
- `T07_answer_ppi_hotspot_barnase_d39a`: Binding hotspot direction answer.
- `T08_communicate_t4l_dynamics`: Figure/metrics communication.
- `T09_study_t4l_wt_vs_l99a_methods`: Study methods package.

## Critical Rules

- Before producing a submission, read `task.json` and the task's `input/`
  directory only. **Do not read `truth/`, `scorer/`, or `expected/`.**
- The agent under test may use any MD engine or workflow. Scoring only depends
  on the files listed in `manifest.json` plus the task contract.
- Curator-fixed inputs: every task ships its own concrete PDB and config
  files in `input/`. Do not select different cases.
- Honesty first: only set boolean success metrics to `true` when the
  artifacts support them. If work was not run or is uncertain, use
  `manifest.status="partial"` (× 0.6 multiplier) or `"blocked"` (zero) and
  explain in `evidence_report.limitations`.
- For T02-style structured refusal: set `manifest.status="failed"` and emit
  `metrics.preparation.guardrail_code` from the allowed set; that earns full
  credit when the truth file confirms the expected guardrail.
- The scorer re-runs computations (md5, trajectory load, RMSD recompute,
  caption ↔ metrics consistency). Submitted JSON values are
  cross-validated; they are not trusted blindly.

## Submission Layout

```text
submission/
  manifest.json          # required
  metrics.json           # required by most tasks
  evidence_report.json   # required by most tasks
  provenance.json        # required by all tasks
  decision_log.jsonl     # optional, useful for traceability
  methods.md             # required by T09
  figures/               # required by T08; manifest.outputs.figures must list each
  prepared_structure.pdb # required by T03
```

## Scorer Runtime

Run benchmark validation/scoring commands either entirely inside the
`mdclaw:latest` container (Mode A) or entirely inside a `mdclaw` conda env
(Mode B). **Never mix scorer runtimes inside one run.**

```bash
# Mode A — container
docker run --rm -v "$PWD:/work" -w /work mdclaw:latest \
  mdclaw init_benchmark_run --output-dir benchmark_runs --run-id <id>

# Mode B — conda
conda run -n mdclaw mdclaw init_benchmark_run \
  --output-dir benchmark_runs --run-id <id>

# bin/mdclaw wrapper auto-selects Mode B when a 'mdclaw' conda env exists,
# otherwise falls back to singularity → docker.
```

The benchmark CLI runs in pure Python (no MD compute), but it stays inside
the chosen runtime so the dependency closure (mdclaw, pydantic, mdtraj) is
always self-consistent. The agent/runtime being evaluated can be a separate
program or container; it only needs to produce the required `submission/`
directory.

## Workflow

1. List tasks and validate the dataset:

   ```bash
   mdclaw list_benchmark_tasks
   mdclaw validate_benchmark_task --task-file \
       benchmarks/mdagentbench/tasks/T01_engine_smoke/task.json
   ```

2. Initialize a run:

   ```bash
   mdclaw init_benchmark_run \
     --output-dir benchmark_runs \
     --run-id <YYYYMMDD>_<harness>_<id> \
     --execution-mode lite \
     --judge-mode deterministic \
     --backend-name <md-engine-or-workflow> \
     --harness-name <agent-runner> \
     --model-name <llm-or-agent-model>
   ```

3. For each task, read `task.json` + `input/`, do the work, write
   `submission/` under
   `benchmark_runs/<run_id>/tasks/<task_id>/submission/`.

4. Validate before scoring:

   ```bash
   mdclaw validate_benchmark_submission \
     --task-file benchmarks/mdagentbench/tasks/<task_id>/task.json \
     --submission-dir benchmark_runs/<run_id>/tasks/<task_id>/submission
   ```

5. Score:

   ```bash
   mdclaw score_benchmark_submission \
     --task-file benchmarks/mdagentbench/tasks/<task_id>/task.json \
     --submission-dir benchmark_runs/<run_id>/tasks/<task_id>/submission \
     --run-id <run_id> \
     --output-file benchmark_runs/<run_id>/tasks/<task_id>/score.json
   ```

6. Summarize:

   ```bash
   mdclaw summarize_benchmark_run --run-dir benchmark_runs/<run_id>
   ```

   `runs.jsonl` and `summaries.jsonl` are appended with last-write-wins
   semantics on `run_id`.

## Pilot tasks (v1.0)

| Task | Primary axis | Mode | Target system |
|---|---|---|---|
| T01_engine_smoke | execution | lite | Chignolin (5AWL), explicit TIP3P water |
| T02_prep_metalloenzyme_guardrail | preparation | dry_run | Carbonic anhydrase II (2CBA) |
| T03_prep_ligand_pose_t4l_benzene | preparation | lite | T4L L99A + benzene (181L) |
| T04_exec_short_protein_md | execution | lite | T4 lysozyme WT (2LZM), explicit TIP3P water |
| T05_exec_restart_continue | execution | lite | Chignolin (5AWL) |
| T06_answer_stability_t4l_l99a | scientific_answer | plan_only | T4 lysozyme L99A |
| T07_answer_ppi_hotspot_barnase_d39a | scientific_answer | plan_only | Barnase D39A on 1BRS |
| T08_communicate_t4l_dynamics | evidence_communication | dry_run | T4 lysozyme WT |
| T09_study_t4l_wt_vs_l99a_methods | evidence_communication | dry_run | T4L WT vs L99A |

Five tasks reuse T4 lysozyme as a shared scaffold so scoring is comparable
across harnesses.

## Structured LLM Judge (optional, deferred to v1.x automation)

Deterministic mode is the default. To add qualitative scoring:

1. An external evaluator reads `<task_dir>/scorer/llm_judge_prompt.json`,
   combines it with the agent's submission, calls an LLM, and saves the
   structured response (a JSON object with `enabled`, `judge_model`,
   `temperature`, `scores`, `violations`).
2. Pass that file via `--llm-judge-file` to `score_benchmark_submission`.
3. Score axes that have a populated secondary will reflect the judge values;
   axes without a judge entry remain `null`.

A `mdclaw run_llm_judge` automation tool will land in v1.x. For now the
judge file must be produced externally.

## External Agent Quick Guide

For external agents and programs, the shortest contract is:

1. Read `benchmarks/mdagentbench/dataset.json` to choose a task.
2. Read only `tasks/<task_id>/task.json` and `tasks/<task_id>/input/`.
3. Do not read `truth/` or `scorer/`.
4. Write `benchmark_runs/<run_id>/tasks/<task_id>/submission/`.
5. Validate, score, and summarize with the `mdclaw` benchmark commands.

The detailed external-user guide is `docs/benchmark/external-agents.md`.
For OpenRouter-backed harness/model matrix runs, use
`docs/benchmark/openrouter-harness-matrix.md`.

## Generic Submission Template

For non-MDClaw agents, create a scaffold without a `job_dir`:

```bash
mdclaw create_benchmark_submission_template \
  --task-id <task_id> \
  --run-id <run_id> \
  --output-dir benchmark_runs/<run_id>/tasks/<task_id>/submission \
  --agent-name <agent-name> \
  --backend-name <openmm|gromacs|other> \
  --harness-name <runner>
```

Then fill task-specific metrics, evidence, and artifacts before scoring.

## Optional MDClaw Job Adapter

When a completed MDClaw `job_dir` already exists, start with:

```bash
mdclaw export_mdclaw_submission \
  --job-dir <job_dir> \
  --task-id <task_id> \
  --run-id <run_id> \
  --output-dir benchmark_runs/<run_id>/tasks/<task_id>/submission
```

The adapter writes a partial-status skeleton (manifest, metrics, provenance,
evidence_report). Fill in task-specific deterministic values yourself.

## Dataset Maintenance

The v1.0 dataset is curator-authored and lives at
`benchmarks/mdagentbench/`. Schema files are generated from the pydantic
models:

```bash
mdclaw write_benchmark_schemas --output-dir benchmarks/mdagentbench/schemas
```

`create_pilot_benchmark` is now a no-op when the dataset already exists; it
returns success and does not regenerate task contracts.
