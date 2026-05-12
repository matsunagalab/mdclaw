---
name: MD Benchmark
description: "Run MDAgentBench tasks with prompt-driven MD agents and deterministic scorer commands. Use for benchmark runs, agent submissions, and comparing MD agents."
---

# MD Benchmark

MDAgentBench evaluates a prompt-driven MD agent. The agent may use MDClaw,
MDCrow, GROMACS, Amber, OpenMM scripts, or another backend, but scoring is
always artifact-based and script-driven.

## Rule

Give the agent only:

- `<task_dir>/prompt.md`
- `<task_dir>/task.json`
- `<task_dir>/input/`

Never expose:

- `<task_dir>/truth/`
- `<task_dir>/scorer/`

No fake trajectories, fake metrics, fake citations, or guessed conclusions.
Treat `<task_dir>/task.json` as the execution contract. In particular, read
`failure_policy`, `time_limit_minutes`, `task_intent`, and the scoring checks
before deciding whether a task can be blocked.

## Minimum Attempt Policy

If `failure_policy.blocked_by_missing_input_allowed=false` and
`failure_policy.insufficient_information_allowed=false`, do not submit
`manifest.status="blocked"` just because the run may be slow or inconvenient.
Attempt the required stages until one of these happens:

- the task succeeds
- the task reaches the task time limit
- a concrete MDClaw/tool/runtime failure stops progress

For execution tasks, attempt the MD stages requested by the task intent. For a
standard explicit-water MD task this means source/prep, solvation, topology,
equilibration, and production. For restart tasks, run the requested chunks and
attempt the concatenation/continuity checks. For comparative answer tasks, run
the requested systems before reporting an effect direction.

Before writing `manifest.status="blocked"`, record enough evidence to prove
that the task was actually attempted:

- exact `mdclaw ...` commands or sub-agent actions attempted
- deepest stage reached: `source`, `prep`, `solv`, `topo`, `eq`, `prod`,
  `analysis`, or `report`
- exit code or timeout status
- stdout/stderr or log file paths
- walltime
- concrete blocker and the next command that would have been run

Write this evidence in `provenance.json`, `evidence_report.json`, and a
decision log when useful. If no required stage was attempted, the submission is
not a valid MDClaw benchmark attempt.

## MDClaw Agent

For MDClaw, launch one sub-agent per task and give it this prompt:

```text
You are the MDClaw benchmark agent for <task_id>.

Read only <task_dir>/prompt.md, <task_dir>/task.json, and <task_dir>/input/.
Do not read truth/ or scorer/.

Use MDClaw CLI tools and MDClaw skills to run real MD when the task asks for it.
Write the required benchmark submission files to <submission_dir>/.

Before deciding blocked, read failure_policy and time_limit_minutes from
task.json. If blocked outcomes are not allowed, do not stop because the task is
long; run until success, timeout, or a concrete tool/runtime failure.

For execution tasks, attempt the requested MDClaw stages. For explicit-water MD,
that normally means source/prep, solvation, topology, equilibration, and
production. For restart tasks, run the requested chunks and attempt trajectory
concatenation/continuity checks. For comparative answer tasks, run the requested
systems before reporting an effect direction.

Do not fabricate. If blocked after real attempts, write
manifest.status="blocked" and explain the real blocker in evidence_report.json
and provenance/decision logs. Include commands, deepest stage reached, exit
codes or timeout status, log paths, walltime, and the next command that would
have been run.
```

## MDCrow Agent

Placeholder: run MDCrow from the same public task inputs, then export the
standard `submission/` contract. Keep the scorer unchanged.

## Generic Agent

Placeholder: any agent is valid if it reads only public inputs and writes the
standard `submission/` directory. Process-based agents can be wrapped with
`run_benchmark_suite backend="command"`.

## Scorer

After the agent writes `submission/`, score only with scripts:

```bash
mdclaw validate_benchmark_submission --task-file <task_dir>/task.json --submission-dir <submission_dir>
mdclaw score_benchmark_submission --task-file <task_dir>/task.json --submission-dir <submission_dir> --run-id <run_id> --output-file <run_task_dir>/score.json
mdclaw summarize_benchmark_run --run-dir benchmark_runs/<run_id>
```
