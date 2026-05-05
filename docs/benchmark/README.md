# MDAgentBench

MDAgentBench is a tool-agnostic benchmark contract for molecular dynamics
agents. It evaluates MDClaw as one backend, but the submission format is just
files on disk, so Claude Code, Cursor, OpenCode, Pi, GROMACS workflows, raw
OpenMM scripts, or other harnesses can be compared with the same scorer.

## Scores

The benchmark reports four composite scores:

- `preparation`: system preparation quality, including pose preservation,
  structured guardrails, and MD-ready artifacts.
- `execution`: short MD completion, restart/continuation correctness, finite
  energy, and analysis completeness.
- `scientific_answer`: agreement with curated experimental direction or
  supplied truth, plus calibrated interpretation.
- `evidence_communication`: plot/data consistency, methods traceability,
  figure readiness, and explicit limitations.

Each task has one `primary_score` and optional `secondary_scores`. This avoids
double-counting one failure across all axes.

## Dataset Layout

The checked-in pilot lives at `benchmarks/mdagentbench/`:

```text
benchmarks/mdagentbench/
  dataset.json
  schemas/
    task_schema_v0.1.json
    submission_manifest_schema_v0.1.json
    score_schema_v0.1.json
  tasks/<task_id>/
    task.json
    input/
    truth/experimental_truth.json
    expected/required_outputs.json
    expected/scoring_rubric.json
    scorer/llm_judge_prompt.json
```

`benchmarks/mdagentbench_lite_v0_1/` contains the 30-task Lite v0.1 skeleton
with the same schema. Concrete task input files can be populated from the
curated source pools listed in each `task.json`.

## Submission Contract

Every harness writes a `submission/` directory:

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

Only the artifacts are scored. The scorer does not inspect chat transcripts,
tool calls, or harness-specific logs.

## Running A Baseline

Create or refresh benchmark contracts:

```bash
mdclaw create_pilot_benchmark --benchmark-dir benchmarks/mdagentbench --overwrite
mdclaw create_lite_benchmark --benchmark-dir benchmarks/mdagentbench_lite_v0_1 --overwrite
```

Initialize a run:

```bash
mdclaw init_benchmark_run \
  --output-dir benchmark_runs \
  --run-id 20260505_cursor_gpt55_mdclaw_pilot \
  --execution-mode dry_run \
  --judge-mode deterministic
```

For each task, a harness should read `task.json`, produce `submission/`, then
score it:

```bash
mdclaw score_benchmark_submission \
  --task-file benchmarks/mdagentbench/tasks/exec_short_protein_md/task.json \
  --submission-dir benchmark_runs/20260505_cursor_gpt55_mdclaw_pilot/tasks/exec_short_protein_md/submission \
  --run-id 20260505_cursor_gpt55_mdclaw_pilot \
  --output-file benchmark_runs/20260505_cursor_gpt55_mdclaw_pilot/tasks/exec_short_protein_md/score.json
```

Aggregate the run:

```bash
mdclaw summarize_benchmark_run \
  --run-dir benchmark_runs/20260505_cursor_gpt55_mdclaw_pilot
```

The runner appends durable records to:

- `benchmark_runs/runs.jsonl`
- `benchmark_runs/summaries.jsonl`

## Using The Agent Skill

Claude Code and Cursor users can start from the benchmark skill:

```text
/mdclaw:md-benchmark run a smoke evaluation for exec_short_protein_md
```

The skill keeps the evaluated-agent and scorer roles separate. While producing
a task submission, the agent should read `task.json` and input files only; it
should not read the task's `truth/` or `scorer/` directories before writing
`submission/`. After artifacts are produced, use `validate_benchmark_submission`,
`score_benchmark_submission`, and `summarize_benchmark_run` to validate, score,
and aggregate results.

Good first smoke tasks are:

- `exec_short_protein_md`, for execution metrics such as completion, finite
  energy, and no NaN values.
- `prep_guardrail_bad_ligand`, for structured-failure and guardrail reporting.

## Structured LLM Judge

Human judges are not part of the benchmark. If qualitative judging is enabled,
the harness or external evaluator must call an LLM with
`scorer/llm_judge_prompt.json` and save the raw structured JSON. That file can
then be passed to `score_benchmark_submission --llm-judge-file`. The scorer
stores the judge model, rubric scores, violations, and prompt hash fields in
`score.json`.

## MDClaw Adapter

Use `export_mdclaw_submission` to create a conservative submission skeleton
from a `job_dir` or `study_dir`:

```bash
mdclaw export_mdclaw_submission \
  --job-dir job_1ake \
  --task-id exec_short_protein_md \
  --run-id 20260505_cursor_gpt55_mdclaw_pilot \
  --output-dir benchmark_runs/20260505_cursor_gpt55_mdclaw_pilot/tasks/exec_short_protein_md/submission
```

The adapter records provenance and common evidence/methods artifacts, but it
does not infer scientific success. Harnesses should still fill `metrics.json`
with task-specific deterministic metrics.

## Plotting Dependency

Figure-producing analysis tasks require `matplotlib` with a headless backend.
`environment.yml` and `pyproject.toml` both declare it so PNG-producing
`analyze_server` tests are not skipped in benchmark environments.
