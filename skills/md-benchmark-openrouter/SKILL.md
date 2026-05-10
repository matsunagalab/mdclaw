---
name: MD Benchmark OpenRouter Matrix
description: "Run MDAgentBench harness × OpenRouter model matrix evaluations. Use when comparing multiple agent harnesses, model slugs, or OpenRouter provider-routing settings."
---

# MD Benchmark OpenRouter Matrix

Read `skills/md-benchmark/SKILL.md`, `skills/common/preamble.md`, and
`skills/common/tool-output.md` before acting.

Use this skill when the user wants to compare multiple harnesses and LLM models
against MDAgentBench. OpenRouter is the model/provider router; MDAgentBench
still scores only `submission/` artifacts.

## Required Mental Model

- `harness` = the agent runner, e.g. Pydantic AI, OpenAI Agents SDK, LangGraph,
  smolagents, Cursor, Claude Code, OpenCode, or a custom script.
- `model_provider` = `openrouter` for these runs.
- `model_name` = OpenRouter model slug, e.g.
  `anthropic/claude-sonnet-4-5`.
- `backend_name` = MD engine or workflow used by the harness, e.g. `openmm`,
  `gromacs`, `literature-answer-workflow`, or `mock`.
- `run` = one harness/model combination over one or more benchmark tasks.

Every combination must end in a normal MDAgentBench `submission/`, `score.json`,
and `summary.json`.

## Critical Rules

- Never read `truth/`, `scorer/`, or `expected/` as the agent under test.
- Do not hide failed combinations. If a harness/model fails, still create a
  submission with `manifest.status="blocked"` or `"partial"` and run scoring.
- Record routing in `provenance.json`:
  `router.name="openrouter"`, `router.model`, and `router.provider`.
- Record run metadata:
  `harness_name`, `backend_name`, `model_provider="openrouter"`, `model_name`.
- For publishable comparisons, prefer `provider.allow_fallbacks=false` or
  explicit `provider.only` so the actual provider is controlled.
- If using tool calling or structured output, set `provider.require_parameters`
  where supported and record any unsupported-provider failures.
- Keep OpenRouter API keys out of committed files. Use `OPENROUTER_API_KEY`.

## Recommended Workflow

1. Create or inspect a matrix config such as
   `examples/benchmark/harness_matrix.openrouter.json`.
2. Run mock mode first:

   ```bash
   python examples/benchmark/run_openrouter_matrix.py \
     --config examples/benchmark/harness_matrix.openrouter.json \
     --output-dir benchmark_runs \
     --mock
   ```

3. Inspect generated `run_config.json`, `provenance.json`, `score.json`, and
   `summary.json`.
4. For real OpenRouter runs:

   ```bash
   export OPENROUTER_API_KEY=...
   python examples/benchmark/run_openrouter_matrix.py \
     --config examples/benchmark/harness_matrix.openrouter.json \
     --output-dir benchmark_runs
   ```

5. Compare runs by `summary.json` and keep per-task `score.json` for audit.

## Config Contract

Matrix config fields:

- `run_prefix`: prefix for generated run IDs.
- `tasks`: list of MDAgentBench `task_id` values.
- `harnesses`: list of `{name, adapter}` objects.
- `models`: list of `{name, provider}` objects where `name` is an OpenRouter
  model slug and `provider` is passed to OpenRouter / provenance.
- `budget`: optional token, walltime, and cost budget metadata.

`generic-openrouter` is the built-in minimal adapter for plan-only tasks. It can
call OpenRouter directly, but it does not run MD and is not sufficient for
execution tasks that require real trajectories.

## Documentation

Long-form guide:
`docs/benchmark/openrouter-harness-matrix.md`.
