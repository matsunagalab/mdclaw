# OpenRouter Harness Matrix Runs

This guide describes how to evaluate many combinations of agent harnesses and
LLM models against MDAgentBench. OpenRouter is used as the model/provider
router; MDAgentBench remains the artifact scorer.

## Mental Model

Keep three layers separate:

- **Benchmark contract**: `task.json`, `input/`, `submission/`, `score.json`,
  and `summary.json`. This is MDAgentBench and does not depend on OpenRouter.
- **Harness**: the agent runner that reads public task files and writes
  `submission/` artifacts. Examples: Pydantic AI, OpenAI Agents SDK, LangGraph,
  smolagents, a shell script, or a lab-specific runner.
- **Model router**: OpenRouter, used to call model slugs such as
  `anthropic/claude-sonnet-4-5`, `openai/gpt-5.5`, or
  `google/gemini-2.5-pro`.

The same harness can be run across many OpenRouter model slugs, and the same
model can be run through several harnesses. Each harness/model combination
should produce normal MDAgentBench submissions.

```text
matrix config
  -> harness adapter
  -> OpenRouter model
  -> submission/
  -> validate + score
  -> summary.json
```

## When To Use OpenRouter

OpenRouter is useful when the benchmark goal is model comparison across many
providers because it offers one OpenAI-compatible API and a single model slug
namespace. It is not required. Direct Anthropic, OpenAI, Gemini, local vLLM, or
other model integrations are valid if they write the same `submission/` files.

For publishable comparisons, decide how much routing freedom to allow:

- **Reproducibility-first**: set `provider.allow_fallbacks=false`, optionally
  `provider.only=[...]`, and record the provider routing in `provenance.json`.
- **Cost-first**: set `provider.sort="price"` or `provider.max_price`.
- **Availability-first**: allow fallbacks and record that fallbacks were allowed.

OpenRouter's `provider` object can include options such as `order`,
`allow_fallbacks`, `require_parameters`, `zdr`, `only`, `ignore`, `sort`, and
`max_price`. Tool calling and structured output support can differ by provider,
so set `require_parameters=true` when the harness depends on those features.

## Matrix Config

An example config lives at `examples/benchmark/harness_matrix.openrouter.json`:

```json
{
  "run_prefix": "20260510_openrouter_matrix",
  "tasks": ["T06_answer_stability_t4l_l99a", "T07_answer_ppi_hotspot_barnase_d39a"],
  "harnesses": [
    {"name": "generic-openrouter", "adapter": "generic-openrouter"},
    {"name": "pydantic-ai", "adapter": "examples/benchmark/adapters/pydantic_ai_openrouter.py"}
  ],
  "models": [
    {"name": "anthropic/claude-sonnet-4-5", "provider": {"allow_fallbacks": false}},
    {"name": "openai/gpt-5.5", "provider": {"allow_fallbacks": false}}
  ],
  "budget": {"max_tokens_per_task": 20000, "max_walltime_minutes_per_task": 30}
}
```

`run_openrouter_matrix.py` treats each `harness × model` pair as one benchmark
run and evaluates every listed task inside that run. Run IDs are derived from:

```text
<run_prefix>__<harness_name>__<model_slug_sanitized>
```

## Running A Mock Matrix

Use mock mode for CI and local plumbing checks. It does not call OpenRouter and
must not be used as leaderboard evidence.

```bash
python examples/benchmark/run_openrouter_matrix.py \
  --config examples/benchmark/harness_matrix.openrouter.json \
  --output-dir benchmark_runs \
  --mock
```

Mock mode verifies that the matrix expands, submission directories are created,
router provenance is written, tasks are scored, and summaries are produced.

## Running Against OpenRouter

Set an API key, then run without `--mock`:

```bash
export OPENROUTER_API_KEY=...

python examples/benchmark/run_openrouter_matrix.py \
  --config examples/benchmark/harness_matrix.openrouter.json \
  --output-dir benchmark_runs
```

The generic adapter calls OpenRouter's Chat Completions API using only the
public `task.json` and selected `input/` files. It asks the model for an
`evidence_report.json` compatible answer. For execution-heavy tasks, the generic
adapter is not enough because real trajectories and system artifacts are needed;
use a harness that can run MD.

## Harness Adapter Notes

Recommended adapter patterns:

- **Pydantic AI**: use `Agent('openrouter:<model-slug>')` or
  `OpenRouterModel` / `OpenRouterProvider`, then write `submission/`.
- **OpenAI Agents SDK**: use its LiteLLM provider path or another compatible
  provider wrapper, then write `submission/`.
- **LangGraph/LangChain**: use `ChatOpenRouter` or an OpenAI-compatible chat
  model configured with OpenRouter's base URL.
- **smolagents**: use an OpenAI-compatible model wrapper or LiteLLM model with
  OpenRouter configuration.
- **Custom script**: call OpenRouter directly, write the artifact contract, and
  let MDAgentBench score it.

Adapters should never read `truth/` or `scorer/`.

## Provenance Requirements

Every submission should record model routing details in `provenance.json`:

```json
{
  "agent": {"name": "my-agent"},
  "harness": {"name": "pydantic-ai", "adapter": "pydantic-ai-openrouter"},
  "backend": {"name": "literature-answer-workflow"},
  "model": {"provider": "openrouter", "name": "anthropic/claude-sonnet-4-5"},
  "router": {
    "name": "openrouter",
    "model": "anthropic/claude-sonnet-4-5",
    "provider": {"allow_fallbacks": false}
  }
}
```

This lets later summaries distinguish model choice from harness behavior.

## Failure Handling

Do not drop failed combinations. If a harness fails before producing useful
artifacts, still create a submission with:

- `manifest.status="blocked"` for infrastructure/setup failures.
- `manifest.status="partial"` for incomplete but inspectable work.
- `manifest.status="failed"` only for intentional structured refusals that are
  part of a task contract, such as guardrail tasks.

Then run validation/scoring so the failed combination appears in the final
matrix summary.

## Comparing Results

Use each run's `summary.json` for harness/model-level comparison:

- `overall_score`: mean weighted score over tasks in that run.
- `scores.preparation`, `scores.execution`, `scores.scientific_answer`,
  `scores.evidence_communication`: axis scores where evaluable.
- `task_scores[]`: task-level pass/fail check IDs and integrity warnings.
- `runtime`: walltime, token, and GPU-hour totals when recorded by submissions.

Keep the raw `score.json` files when publishing comparisons so readers can see
which deterministic checks passed or failed.
