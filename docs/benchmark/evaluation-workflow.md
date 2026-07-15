# Benchmark Evaluation Workflow

This runbook describes the intended public/private benchmark workflow. The goal
is to keep the solver blind to evaluator-only material while still producing a
reproducible score.

The important rule is simple: **the public package is for the solver; the
private evaluator package is for the scorer only.** A private package is not a
security boundary by itself. It becomes private only when it lives in a
repository, container mount, or filesystem path that the solver process cannot
read.

## Roles

Use three separate locations when possible:

```text
maintainer-repo/
  benchmarks/mdprepbench/
  benchmarks/mdstudybench/

solver-workspace/                  # visible to the evaluated agent
  public/mdprepbench/
  runs/<run_id>/tasks/<task_id>/submission/
  scratch/

evaluator-workspace/               # not visible to the evaluated agent
  private/mdprepbench/
  runs/<run_id>/tasks/<task_id>/submission/
  runs/<run_id>/tasks/<task_id>/harness_execution.json
```

The solver workspace may contain public prompts and output directories. The
evaluator workspace contains canonical `task.json`, `truth/`, scorer-only
references, submitted artifacts, and harness-measured runtime records.

## Automated Runner

For repeated local measurements, prefer the SWE-bench-style runner:

```bash
mdclaw run_benchmark_agent \
  --dataset-dir benchmarks/mdprepbench \
  --run-id pi_p01_001 \
  --task-ids P01_prep_simple_monomer_t4l \
  --agent-name pi
```

The runner creates public and private packages, creates a solver workspace,
runs the agent once per selected task, records measured execution evidence, and
scores with the private evaluator package. Claude Code and Codex can be run by
changing only `--agent-name`. Built-in profiles include the usual
non-interactive approval-bypass flags for benchmark runs:

The automated runner defaults to 30 minutes per task. Increase
`--max-walltime-minutes-per-task` for slow local MD or exploratory debugging
runs.

The runner is sequential by default. Pass `--jobs N` to run N tasks
concurrently within one run; all tasks write to disjoint
`run_dir/tasks/<task_id>` directories and the run is scored once after every
task finishes, so a parallel run yields the same single `summary.json` as a
sequential one. Add `--gpus M` (when > 0) to round-robin `CUDA_VISIBLE_DEVICES`
across tasks by task index, so N concurrent tasks land on N distinct GPUs.

Built-in profiles set explicit model flags by default: Pi uses
`spark1-vllm/deepseek-v4-flash`, Claude Code uses `sonnet`, and Codex
uses `gpt-5.4-mini`. Override this with `--agent-model <model>`; the resolved
model is recorded in `run_config.json`, `summary.json`, and each task's
`agent_run.json`.

The runner is neutral about the solver workflow. It does not require or reward
MDClaw skills. `tooling_condition` is only a run-summary grouping label: use
`mdclaw-free` for direct OpenMM/PDBFixer, MDCrow, or any solver that does not
call MDClaw, `mdclaw-cli-only` for MDClaw CLI without skills, and
`mdclaw-skills+cli` only for the MDClaw reference condition.

Skill exposure is tracked separately in `solver_context`. The automated runner
infers it from the command template by default (`none`, `skill-system`,
`skill-text-injected`, or `unknown`) and writes it to `run_config.json`,
`attestation.json`, `summary.json`, and per-task `agent_run.json`. If the
automatic inference is not accurate for a custom harness, pass
`--solver-context none`, `skill-system`, `skill-text-injected`, or `unknown`.
This is a comparison/audit field only; it never affects scoring.
For skill-system runs managed by `run_benchmark_agent`, pass
`--agent-skills-dir skills`. The runner installs that root into
`skills/`, `.agents/skills/`, `.claude/skills/`, `.codex/skills/`, and
`package.json` for Pi. Pi's default profile disables skills, so combine this
with `--agent-profile pi-user` when benchmarking Pi with skills.
The default MDClaw CLI policy is `forbid-without-skill`: if the solver uses
`mdclaw ...` while `solver_context` says no skill context was exposed, the
runner reports a run-condition violation. This keeps the main comparison as
`mdclaw-free` versus a declared MDClaw stage-skill run; use
`--mdclaw-cli-policy allow` only for an explicit CLI-only ablation.

```bash
mdclaw run_benchmark_agent \
  --dataset-dir benchmarks/mdprepbench \
  --run-id claude_p01_001 \
  --task-ids P01_prep_simple_monomer_t4l \
  --agent-name claude-code
```

```bash
mdclaw run_benchmark_agent \
  --dataset-dir benchmarks/mdprepbench \
  --run-id codex_p01_001 \
  --task-ids P01_prep_simple_monomer_t4l \
  --agent-name codex
```

The default profiles are plain non-interactive profiles that read only the
generated task prompt. Use `--agent-profile pi-user` to let Pi use normal
user-wide discovery with an isolated session directory, or `--agent-command` for
a custom command template.

Supported template variables are `{{agent_prompt}}`,
`{{task_instructions}}`, `{{prompt_file}}`, `{{submission_dir}}`,
`{{work_dir}}`, `{{solver_workspace}}`, `{{task_id}}`, `{{run_id}}`,
`{{run_dir}}`, `{{agent_session_dir}}`, `{{agent_model}}`, and
`{{repo_root}}`. Template values are shell-quoted before execution.
`submission_dir` is output-only; use `work_dir` for study/job/scratch files.

When the agent invokes `mdclaw` commands, the runner sets an opt-in environment
hook so the MDClaw CLI appends measured stage records to a runner-owned JSONL
log that is folded into `harness_execution.json`. This replaces hand-written
harness records. Agents that never call the MDClaw CLI need an adapter or
stage-recording wrapper before strict stage-level provenance can pass. The
runner also writes an agent-safe `record_stage.py` wrapper into each task
workspace and exposes it in `task_instructions.json` as `stage_recording` and
in `$MDCLAW_BENCHMARK_STAGE_WRAPPER`; non-MDClaw solvers can run
`$MDCLAW_BENCHMARK_STAGE_WRAPPER --stage source -- <command>` for source,
prep, topology, and minimization commands. This is an execution-evidence
requirement, not an MDClaw-skills requirement.

The runner also exposes `submission_preflight` in `task_instructions.json`.
That command runs the public `tools/validate_submission.py` script against the
exact `submission_dir` and public `submission_contract.json`. It is the same
tool for MDClaw and non-MDClaw solvers and does not contain task-specific
recipes.

After the agent process exits, the runner finalizes the handoff before scoring:
it detects and terminates leftover process-group members, copies the solver
submission to the evaluator task directory, runs the public preflight, scans
solver-visible MDClaw `progress.json` files for active `running`/queued nodes,
and writes `finalization.json`. These diagnostics do not change the raw
artifact score, but they are surfaced in `summary.json` as contract/harness
status fields so incomplete background work is not confused with a completed MD
preparation.

The default runner uses a separate solver workspace but does not create a hard
OS/container sandbox. For leaderboard-quality held-out results, run the solver
workspace in a container or account that cannot read the maintainer checkout,
the private package, or the evaluator run directory.

## 1. Export Public And Private Packages

From the maintainer checkout, export the solver-facing package:

```bash
mdclaw export_benchmark_public_package \
  --dataset-dir benchmarks/mdprepbench \
  --output-dir /path/to/solver-workspace/public/mdprepbench
```

Export the evaluator-facing package to a location the solver cannot read:

```bash
mdclaw export_benchmark_private_package \
  --dataset-dir benchmarks/mdprepbench \
  --output-dir /path/to/evaluator-workspace/private/mdprepbench
```

Use `benchmarks/mdstudybench` for MDStudyBench runs. Do not mount the canonical
benchmark task directory, the private package, `truth/`, scorer-only references,
or `harness_tasks.json` into the solver workspace.

## 2. Run The Solver With Public Material Only

For each task, give the evaluated agent only:

- `public/<suite>/tasks/<task_id>/prompt.md`
- `public/<suite>/tasks/<task_id>/submission_contract.json`
- `public/<suite>/tasks/<task_id>/submission_checklist.md`
- an empty or existing `submission/` directory to write

The runner may enforce time limits and provide a scratch directory, but it must
not inject task-specific chains, ligands, model numbers, membrane geometry,
salt settings, or workflow knobs that are absent from the public prompt or
contract.

For MDPrepBench v0.3, the solver writes only the raw OpenMM triple,
`prepared_structure.pdb`, and task-specific raw files under `submission/`.
MDStudyBench keeps its suite-specific manifest, provenance, and evidence files.

## 3. Record Harness-Measured Execution

Strict tasks require a harness-owned execution record outside `submission/`.
The runner should write one JSON file per task, for example:

```json
{
  "schema_version": "1.0",
  "run_id": "20260613_ref",
  "task_id": "P04_prep_multi_ligand_filter_3pwb",
  "records": [
    {
      "stage": "source",
      "command": "agent action or command captured by the harness",
      "exit_code": 0,
      "walltime_seconds": 12.3
    }
  ]
}
```

For MDPrepBench, completed submissions normally need `source`, `prep`, `topo`,
and `min` stages. For MDStudyBench comparative tasks, the usual stages are
`source`, `prep`, `prod`, `analysis`, and `report`. Task-specific public
contracts list the required stages in `harness_evidence_requirements`.

Do not ask the solver to write this file. The point is that the harness measures
what actually ran and stores the record somewhere the solver cannot modify.

## 4. Move Artifacts To The Evaluator

After the solver stops, copy or mount only the submitted artifacts and the
harness execution record into the evaluator workspace:

```text
evaluator-workspace/runs/<run_id>/tasks/<task_id>/
  submission/
    topology/system.xml
    topology/topology.pdb
    topology/state.xml
    prepared_structure.pdb
    <task-specific raw artifacts>
  harness_execution.json
```

The private evaluator package first appears in this evaluator phase. It should
not have been present in the solver workspace.

## 5. Score With The Private Evaluator Package

For one task:

```bash
mdclaw validate_and_score_benchmark_submission \
  --task-file /path/to/evaluator-workspace/private/mdprepbench/tasks/<task_id>/task.json \
  --submission-dir /path/to/evaluator-workspace/runs/<run_id>/tasks/<task_id>/submission \
  --run-id <run_id> \
  --harness-record-file /path/to/evaluator-workspace/runs/<run_id>/tasks/<task_id>/harness_execution.json \
  --validation-output-file /path/to/evaluator-workspace/runs/<run_id>/tasks/<task_id>/validation.json \
  --output-file /path/to/evaluator-workspace/runs/<run_id>/tasks/<task_id>/score.json
```

For a prepared run directory:

```bash
mdclaw score_benchmark_run \
  --run-dir /path/to/evaluator-workspace/runs/<run_id> \
  --dataset-dir /path/to/evaluator-workspace/private/mdprepbench
```

`score_benchmark_run` looks for each task's harness record at
`tasks/<task_id>/harness_execution.json`.

## What This Prevents

This workflow prevents the evaluated agent from reading scorer-only task
contracts, hidden truth files, and deterministic check details during solving.
It also prevents a solver from fabricating runtime by hand-editing a submission
file, because the scorer uses a separate harness-owned execution record.

It does not protect a benchmark if the solver is allowed to read the canonical
repository, the private package, or the evaluator workspace. In that case the
run is not held out and should not be reported as a comparable benchmark result.

## Audit Checklist

Before reporting a run as comparable, check:

- The solver workspace contained the public package, not the private package.
- The solver could not read canonical `task.json`, `truth/`, scorer-only
  references, `harness_tasks.json`, or `harness_instructions.json`.
- Every strict task has `harness_execution.json` outside `submission/`.
- The private evaluator package used for scoring matches the benchmark version
  reported in the run.
- `summary.json` records the intended `tooling_condition`.
- Subset runs are reported as subsets, not full-suite scores.
