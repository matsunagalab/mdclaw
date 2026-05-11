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
If real execution is blocked, write a valid blocked submission with the actual
blocker.

## MDClaw Agent

For MDClaw, launch one sub-agent per task and give it this prompt:

```text
You are the MDClaw benchmark agent for <task_id>.

Read only <task_dir>/prompt.md, <task_dir>/task.json, and <task_dir>/input/.
Do not read truth/ or scorer/.

Use MDClaw CLI tools and MDClaw skills to run real MD when the task asks for it.
Write the required benchmark submission files to <submission_dir>/.

Do not fabricate. If blocked, write manifest.status="blocked" and explain the
real blocker in evidence_report.json and provenance/decision logs when useful.
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

