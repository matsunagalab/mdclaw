---
name: MD Benchmark
description: "Run MDAgentBench evaluations for MDClaw or external MD agents. Use when creating benchmark runs, producing task submissions, validating/scoring submissions, or summarizing benchmark results."
---

# MD Benchmark

Read `skills/common/preamble.md` and `skills/common/tool-output.md` before
acting.

Use this skill to evaluate MD agents with the artifact-based MDAgentBench
contract. The benchmark scores only submitted files, not chat history, tool
calls, or harness-specific logs.

## Core Contract

Each task has:

- `task.json`: public task contract the agent may read before acting.
- `submission/`: artifact directory produced by the evaluated agent or harness.
- `score.json`: deterministic and optional structured-judge score for one task.
- `summary.json`: aggregate score for a benchmark run.

Default checked-in datasets:

- Pilot: `benchmarks/mdagentbench/`
- Lite v0.1 skeleton: `benchmarks/mdagentbench_lite_v0_1/`

## Critical Rules

- Before producing a task submission, read `task.json` and input files only.
  Do not read `truth/` or `scorer/` for that task.
- Do not set success metrics to `true` unless the submitted artifacts support
  them. If work was not run or is uncertain, use `partial`, `failed`, or
  `blocked` and explain the limitation.
- Keep `submission/manifest.json` consistent with files that actually exist.
- Treat `export_mdclaw_submission` as a conservative skeleton exporter. It does
  not infer task-specific scientific success; fill `metrics.json` explicitly.
- The scorer does not call an LLM. If qualitative judging is requested, an
  external evaluator writes structured JSON from `scorer/llm_judge_prompt.json`
  and passes it through `--llm-judge-file`.

## Submission Layout

Every harness writes a task-level `submission/` directory:

```text
submission/
  manifest.json
  metrics.json
  evidence_report.json
  provenance.json
  decision_log.jsonl
  figures/
  methods.md
```

Only files required by the task contract need to be present, but
`manifest.json`, `metrics.json`, `evidence_report.json`, and `provenance.json`
are the common minimum.

## Workflow

1. Choose the dataset and task:

   ```bash
   mdclaw list_benchmark_tasks
   mdclaw validate_benchmark_task --task-file benchmarks/mdagentbench/tasks/<task_id>/task.json
   ```

2. Initialize a run:

   ```bash
   mdclaw init_benchmark_run \
     --output-dir benchmark_runs \
     --run-id <run_id> \
     --execution-mode dry_run \
     --judge-mode deterministic
   ```

3. Produce `submission/` for each task under:

   ```text
   benchmark_runs/<run_id>/tasks/<task_id>/submission/
   ```

4. Validate before scoring:

   ```bash
   mdclaw validate_benchmark_submission \
     --task-file benchmarks/mdagentbench/tasks/<task_id>/task.json \
     --submission-dir benchmark_runs/<run_id>/tasks/<task_id>/submission
   ```

5. Score the task:

   ```bash
   mdclaw score_benchmark_submission \
     --task-file benchmarks/mdagentbench/tasks/<task_id>/task.json \
     --submission-dir benchmark_runs/<run_id>/tasks/<task_id>/submission \
     --run-id <run_id> \
     --output-file benchmark_runs/<run_id>/tasks/<task_id>/score.json
   ```

6. Summarize the run:

   ```bash
   mdclaw summarize_benchmark_run \
     --run-dir benchmark_runs/<run_id>
   ```

Report the overall score, per-axis scores, failed checks, missing outputs, and
any limitations that affect interpretation.

## Common Task Patterns

- Preparation tasks usually require prepared structures, topology-ready
  artifacts, or structured guardrail metrics under `preparation.*`.
- Execution tasks usually require finite-energy and completion evidence under
  `execution.*`, plus provenance for restart or continuation tasks.
- Scientific-answer tasks usually require a clear `effect.direction` in
  `evidence_report.json`; only state a direction when supported by submitted
  evidence.
- Evidence-communication tasks usually require consistent metrics, figures,
  captions, methods, limitations, and provenance.

## MDClaw Job Adapter

When a completed MDClaw `job_dir` or `study_dir` already exists, start with:

```bash
mdclaw export_mdclaw_submission \
  --job-dir <job_dir> \
  --task-id <task_id> \
  --run-id <run_id> \
  --output-dir benchmark_runs/<run_id>/tasks/<task_id>/submission
```

Then inspect the task's deterministic checks and fill `metrics.json`,
`manifest.json`, and `evidence_report.json` with task-specific values that are
supported by the artifacts.

## Dataset Maintenance

Create or refresh benchmark skeletons only when explicitly needed:

```bash
mdclaw create_pilot_benchmark --benchmark-dir benchmarks/mdagentbench
mdclaw create_lite_benchmark --benchmark-dir benchmarks/mdagentbench_lite_v0_1
```

Use `--overwrite` only when the user intends to regenerate existing task
contracts.
