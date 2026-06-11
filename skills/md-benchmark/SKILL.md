---
name: md-benchmark
description: "Run MDPrepBench and MDStudyBench tasks with prompt-driven MD agents and deterministic scorer commands. Use for benchmark runs, agent submissions, and comparing MD agents."
---

# MD Benchmark

MDPrepBench and MDStudyBench evaluate prompt-driven MD agents under the
MDAgentBench family. The agent may use MDClaw, MDCrow, GROMACS, Amber, OpenMM
scripts, or another backend, but scoring is always artifact-based and
script-driven.

Use the suite that matches the task:

- `benchmarks/mdprepbench`: preparation-only tasks (`P01`-`P25`), focused on
  source retrieval, preparation, topology artifacts, minimization evidence, and
  preparation provenance.
- `benchmarks/mdstudybench`: scientific question / study tasks (`S01`-`S03`),
  focused on comparative MD evidence, analysis metrics, methods drafts,
  provenance, decision logs, and calibrated scientific answers.

For MDClaw commands, do not use the external GNU `timeout` wrapper. macOS does
not ship `timeout`; rely on the task time limit, tool/runtime errors, and
MDClaw's internal timeout handling instead.

## Rule

Give the agent the task prompt and a submission directory. The prompt is the
problem statement; it names public sources such as PDB IDs, UniProt accessions,
DOIs, URLs, protocols, and required outputs. Retrieval and provenance are part
of the evaluated behavior.

Agent-facing files:

- `<task_dir>/prompt.md`
- For external agents, prefer an exported public package from
  `mdclaw export_benchmark_public_package`; give the agent only
  `prompt.md`, `submission_contract.json`, and `submission_checklist.md`.

Canonical harness/scorer metadata:

- `benchmarks/mdprepbench/tasks/<task_id>/task.json`
- `benchmarks/mdstudybench/tasks/<task_id>/task.json`

Never expose:

- canonical `task.json` to the benchmark agent
- `<task_dir>/truth/`
- `<task_dir>/scorer/`

No fake trajectories, fake metrics, fake citations, or guessed conclusions.
Treat canonical `task.json` as runner/scorer metadata, not a solution recipe.
Harness code may read `failure_policy`, `time_limit_minutes`, required outputs,
and scoring checks before deciding whether a task can be blocked; agents should
not read it. The runner must not inject task-specific MDClaw command-line
arguments, geometry values, selected chains, model numbers, or workflow knobs
that are not present in the public prompt/submission contract. Those choices
belong to the evaluated agent and must be justified in its submission.

Evaluated agents solve exactly one task at a time. They must not inspect,
categorize, or hardcode behavior across the benchmark suite, and must not write
benchmark-wide solver scripts. A task-local helper script is allowed only when
it executes real workflow steps for the current task and is recorded in
`provenance.command_log`.

`run_id` is an opaque label for the run directory and records. Do not infer task
subset, smoke-test behavior, execution depth, or expected outcome from words in
the run ID. Suite and task selection come only from the benchmark name, dataset
directory, and explicit task IDs.

## Minimum Attempt Policy

If `failure_policy.blocked_by_missing_input_allowed=false` and
`failure_policy.insufficient_information_allowed=false`, do not submit
`manifest.status="blocked"` just because the run may be slow or inconvenient.
Attempt the required stages until one of these happens:

- the task succeeds
- the task reaches the task time limit
- a concrete MDClaw/tool/runtime failure stops progress

For prep tasks, attempt source retrieval, preparation, explicit solvation by
default, topology build, and a short minimization / finite-energy check. Use an
implicit topology only when the prompt explicitly asks for implicit/no-solvent
handling; in that case, do not retain crystallographic or bulk ions as explicit
particles for implicit solvent. Vacuum/no-solvent is a separate explicit prompt
choice and may keep explicit ions. Full equilibration and production are not part of the prep battery. For
execution tasks outside the prep battery, attempt the MD work requested by the
prompt. For restart tasks, run the requested chunks and attempt the
concatenation/continuity checks. For MDStudyBench comparative answer tasks, run
the requested systems before reporting an effect direction, list real trajectory
artifacts in `manifest.outputs.trajectories`, and mirror the quantitative MD
analysis in `metrics.md_analysis` and `evidence_report.evidence.md_metrics`.
For MDStudyBench dry-run evidence-bundle tasks, do not invent trajectories;
submit the requested methods, decision log, evidence report, and study/report
provenance evidence.

Before writing `manifest.status="blocked"`, record enough evidence to prove
that the task was actually attempted:

- exact `mdclaw ...` commands or sub-agent actions attempted
- deepest stage reached: `source`, `prep`, `solv`, `topo`, `min`, `eq`, `prod`,
  `analysis`, or `report`
- exit code or timeout status
- stdout/stderr or log file paths
- walltime
- concrete blocker and the next command that would have been run

Write this evidence in `provenance.json`, `evidence_report.json`, and a
decision log when useful. If no required stage was attempted, the submission is
not a valid MDClaw benchmark attempt.

## Full Benchmark Claim

Do not call a run a full benchmark unless every selected task is validated and
scored, and the MD execution / comparative-answer tasks were
genuinely attempted. If long MD tasks only received blocked placeholders, report
the run as partial or blocked-only, not full.

## Short Prompt Interface

The intended user-facing prompts are short:

```text
MDPrepBenchを run_id=prep_full_run で実行して評価して
```

```text
MDPrepBenchの P11_prep_site_protonation_t4l_glu11 だけを実行して評価して
```

```text
MDStudyBenchの S03_t4l_wt_vs_l99a_methods だけを実行して評価して
```

For these prompts, prepare the run, execute each task from its generated
`agent_prompt.md`, then run `score_benchmark_run`. Keep the evaluated task
agent and the scorer separated as described below.

## MDClaw Agent

Prepare an MDClaw benchmark run from the repository root with:

```bash
mdclaw prepare_benchmark_run \
  --output-dir benchmark_runs \
  --run-id <run_id> \
  --dataset-dir benchmarks/mdprepbench \
  --execution-mode lite
```

The command writes `<run_dir>/agent_tasks.json` plus one
`task_instructions.json` per task. Each instruction points to the agent-safe
`prompt.md`, `submission_contract.json`, and the task's `submission/`
directory. Scoring metadata is written separately to `harness_tasks.json` and
`harness_instructions.json`; do not give those files to the evaluated agent.
Use `--task-ids P01_prep_simple_monomer_t4l P02_prep_1ake_chain_ap5` to run a
subset.

For StudyBench, select the study dataset explicitly:

```bash
mdclaw prepare_benchmark_run \
  --output-dir benchmark_runs \
  --run-id <run_id> \
  --dataset-dir benchmarks/mdstudybench \
  --execution-mode lite \
  --task-ids S01_stability_t4l_l99a
```

For MDClaw, launch one evaluated agent per task with the generated prompt:

```text
Use the md-benchmark skill. Run the task in:
<run_dir>/tasks/<task_id>/agent_prompt.md
```

The generated `agent_prompt.md` points to `task_instructions.json`, which in
turn points to only agent-safe files. Do not hand-write long benchmark prompts;
keep task-specific requirements in `prompt.md`, `submission_contract.json`, and
`submission_checklist.md`.

Internal submission rules for this skill:

- Solve only the task referenced by the current `agent_prompt.md`; do not
  inspect sibling task directories or categorize all benchmark tasks.
- Treat `run_id` and directory names as labels only; do not infer smoke-test
  shortcuts, task subsets, or expected outcomes from them.
- Do not write benchmark-wide solver scripts or task-ID case tables. Batch
  orchestration belongs to the harness/operator, not the evaluated agent.
- Task-local helper scripts are allowed only if they run real workflow steps
  for the current task and are recorded in `provenance.command_log`.
- Run Python helpers inside the MDClaw environment, e.g.
  `conda run -n mdclaw python ...`; system `python3` may not have OpenMM,
  gemmi, or MDClaw installed.
- Do not delete or hand-edit DAG node directories, `node.json`, or
  `progress.json`. If a step must be retried, create a new node or use the
  MDClaw node/need tools so provenance remains auditable.
- For MDPrepBench, attempt source, prep, topology export, and the `min` stage.
- For completed prep submissions, use `manifest.outputs.topology` as a list
  containing `system.xml`, `topology.pdb`, and `state.xml`.
- For ordinary MDClaw DAG runs, use a `min` node after `topo` and record
  `run_minimization` plus its `minimized_structure.pdb` /
  `minimization_report.json` artifacts.
- For MDClaw topology builds being packaged directly for MDPrepBench,
  `state.xml` carries the topology-time minimized coordinates and
  `topology.pdb` supplies the atom/residue topology. Create the benchmark
  `minimized_structure.pdb` with:
  ```bash
  mdclaw export_state_pdb \
    --topology-pdb-file <topology.pdb> \
    --state-xml-file <state.xml> \
    --output-pdb-file <submission_dir>/minimized_structure.pdb
  ```
  Record this command in `provenance.command_log`. Do not assume
  `topology.pdb` itself is the minimized structure unless the workflow
  explicitly wrote it with minimized coordinates.
- Prefer a standalone `min` node for MDClaw MDPrepBench submissions. If you
  package topology-time minimized coordinates directly from `state.xml`, record
  that command as the `min` stage in `provenance.command_log`.
- Record `topology.backend = "openmm"` in `metrics.json` when the public
  contract requires OpenMM topology artifacts.
- Fill every public `metric_requirements` path and follow
  `submission_blueprint`; do not invent hidden task options.
- Record provenance `command_log` entries for the stages named by the public
  checklist: normally `source`, `prep`, `topo`, and `min` for MDPrepBench.
- For MDStudyBench comparative tasks, submit real trajectories under
  `manifest.outputs.trajectories` and connect `metrics.md_analysis` to the
  conclusion.
- For MDStudyBench dry-run evidence-bundle tasks, submit methods, decision log,
  evidence report, and study/report provenance without inventing trajectories.
- If blocked after real attempts, use a non-completed status and record the
  attempted commands, deepest stage, exit code or timeout, logs, walltime,
  blocker, and next intended command.
- Scoring is always separate: evaluated agents stop after writing
  `submission/`; the harness runs scorer commands.

## MDCrow Agent

Placeholder: run MDCrow from the task prompt, then export the standard
`submission/` contract. Keep the scorer unchanged.

## Generic Agent

Placeholder: any agent is valid if it solves the prompt, retrieves public
sources as needed, and writes the standard `submission/` directory.

## Scorer

After the agent writes `submission/`, score only with scripts:

```bash
mdclaw validate_and_score_benchmark_submission \
  --task-file <canonical_task_dir>/task.json \
  --submission-dir <submission_dir> \
  --run-id <run_id> \
  --validation-output-file <run_task_dir>/validation.json \
  --output-file <run_task_dir>/score.json
```

For a run directory prepared by MDClaw, score all task submissions and write the
run summary with:

```bash
mdclaw score_benchmark_run \
  --run-dir <run_dir> \
  --dataset-dir benchmarks/mdprepbench
```

Use `--dataset-dir benchmarks/mdstudybench` for StudyBench runs.

Read the wrapper's normalized fields: `validation_success`, `score_status`,
`weighted_total`, and `benchmark_passed`. Do not infer benchmark pass/fail from
the wrapper's `success` field alone; `success` only means the evaluator wrapper
completed.
