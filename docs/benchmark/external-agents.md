# External Agents And Programs

MDAgentBench is an artifact-based agent benchmark contract. Your agent does not
need to use MDClaw. It may use GROMACS, OpenMM scripts, Amber, MDCrow, another
workflow manager, or a custom LLM harness. The scorer only compares files under
`submission/` against the public task contract and scorer-only truth files.

For automated `harness × model` experiments using OpenRouter, see
[`openrouter-harness-matrix.md`](openrouter-harness-matrix.md).

## What To Read

Read these files before running your agent:

- `benchmarks/mdagentbench/dataset.json`: task list, benchmark version, and
  family metadata.
- `benchmarks/mdagentbench/tasks/<task_id>/task.json`: public task contract,
  scoring axes, required outputs, public inputs, and deterministic checks.
- `benchmarks/mdagentbench/tasks/<task_id>/input/`: public task inputs such as
  PDB files, mutation requests, MD protocols, ligand references, and study
  briefs.

Do not let the agent under test read:

- `benchmarks/mdagentbench/tasks/<task_id>/truth/`: scorer-only answers. For
  scientific-answer tasks, this contains the held-back experimental direction.
- `benchmarks/mdagentbench/tasks/<task_id>/scorer/`: judge prompts and
  evaluator material.

## What To Submit

Every agent writes a `submission/` directory. A typical run layout is:

```text
benchmark_runs/<run_id>/
  run_config.json
  tasks/
    <task_id>/
      submission/
        manifest.json
        metrics.json
        evidence_report.json
        provenance.json
        decision_log.jsonl
        prepared_structure.pdb
        figures/
        methods.md
      score.json
  summary.json
```

Required files vary by task. Check `task.json.required_outputs` first. The
common files are:

- `manifest.json`: `run_id`, `task_id`, `status`, and paths to submitted
  artifacts.
- `metrics.json`: deterministic values such as `execution.no_nan`,
  `execution.simulated_time_ps`, ligand RMSD, or analysis summary values.
- `evidence_report.json`: scientific conclusion, figure captions, limitations,
  and `effect.direction` for scientific-answer tasks.
- `provenance.json`: agent, harness, backend, model, scripts, raw outputs, and
  md5 records when available.
- Task artifacts: trajectories/checkpoints for execution tasks,
  `prepared_structure.pdb` for ligand-preparation tasks, `figures/` for
  communication tasks, and `methods.md` for study-methods tasks.

For execution tasks, trajectory and topology artifacts can be supplied through
`manifest.outputs` rather than copied to a benchmark-specific fixed filename:

```json
{
  "outputs": {
    "trajectories": ["ckpt_exports/traj.dcd"],
    "topology": ["ckpt_exports/topology.pdb"]
  }
}
```

Paths are resolved relative to the `submission/` directory. This keeps the
benchmark agent-independent: an agent with a file registry only needs to point
the manifest at the relevant exported files.

## What The Scorer Compares

The scorer does not read chat transcripts, tool-call logs, or private harness
state. It reads `task.json`, scorer-only `truth/` when needed, and your
`submission/` files.

- Execution tasks compare `metrics.json` flags with reloadable trajectories:
  finite energy, no NaNs, minimum frame counts, simulated time, and restart
  continuity.
- Preparation tasks compare required artifacts, structured guardrail codes,
  and ligand-pose RMSD against public reference coordinates.
- Scientific-answer tasks compare `evidence_report.effect.direction` against
  held-back truth in `truth/experimental_truth.json`.
- Evidence-communication tasks compare figure/method artifacts and check that
  numeric caption claims match `metrics.json`.

## MDCrow-Style File Registries

MDCrow stores generated files in a checkpoint directory and tracks them through
file IDs in `ckpt/paths_registry.json`. MDAgentBench does not need a
MDCrow-specific adapter if the final submission records the relevant files in
the standard manifest.

A generic MDCrow-style workflow is:

1. Start the benchmark run with `harness_name="mdcrow"` and
   `backend_name="mdcrow-openmm"` or the backend actually used.
2. Give the agent only `task.json` and `input/`; do not expose `truth/` or
   `scorer/`.
3. Let MDCrow run normally and produce its checkpoint files.
4. Export or copy the relevant files under `submission/`, for example
   `submission/mdcrow/traj.dcd` and `submission/mdcrow/topology.pdb`.
5. Write `manifest.json` with `outputs.trajectories` and `outputs.topology`
   pointing at those files.
6. Write `metrics.json`, `evidence_report.json`, and `provenance.json` with the
   model, harness, backend, and any raw-output hashes available.

The same pattern applies to any other file-registry agent. The benchmark
contract is the `submission/` directory, not the internal registry.

## Standard Flow

Initialize a run with metadata for the agent under test:

```bash
mdclaw init_benchmark_run \
  --output-dir benchmark_runs \
  --run-id 20260510_external_gromacs_t06 \
  --execution-mode plan_only \
  --judge-mode deterministic \
  --backend-name gromacs \
  --backend-version 2024.4 \
  --harness-name external-python-script \
  --model-name my-agent
```

Create a submission scaffold:

```bash
mdclaw create_benchmark_submission_template \
  --task-id T06_answer_stability_t4l_l99a \
  --run-id 20260510_external_gromacs_t06 \
  --output-dir benchmark_runs/20260510_external_gromacs_t06/tasks/T06_answer_stability_t4l_l99a/submission \
  --agent-name my-agent \
  --backend-name gromacs \
  --harness-name external-python-script
```

Run your agent or external program. It should read only `task.json` and
`input/`, then replace the template values with real metrics, evidence, and
artifacts. When the submission is genuinely complete, update
`manifest.status` from the template default `partial` to `completed`; otherwise
leave it `partial` or use `blocked` / intentional `failed` as appropriate.

Validate and score:

```bash
mdclaw validate_benchmark_submission \
  --task-file benchmarks/mdagentbench/tasks/T06_answer_stability_t4l_l99a/task.json \
  --submission-dir benchmark_runs/20260510_external_gromacs_t06/tasks/T06_answer_stability_t4l_l99a/submission

mdclaw score_benchmark_submission \
  --task-file benchmarks/mdagentbench/tasks/T06_answer_stability_t4l_l99a/task.json \
  --submission-dir benchmark_runs/20260510_external_gromacs_t06/tasks/T06_answer_stability_t4l_l99a/submission \
  --run-id 20260510_external_gromacs_t06 \
  --output-file benchmark_runs/20260510_external_gromacs_t06/tasks/T06_answer_stability_t4l_l99a/score.json

mdclaw summarize_benchmark_run \
  --run-dir benchmark_runs/20260510_external_gromacs_t06
```

## Automating Harness And Model Matrices

For larger comparisons, treat the harness and model router as separate axes:

- `harness`: Pydantic AI, OpenAI Agents SDK, LangGraph, smolagents, Cursor,
  Claude Code, OpenCode, or a custom script.
- `model router`: OpenRouter, direct provider APIs, local vLLM, etc.
- `model`: the concrete model slug, such as
  `anthropic/claude-sonnet-4-5` or `openai/gpt-5.5`.

OpenRouter is convenient because many harnesses can use it through an
OpenAI-compatible API, native OpenRouter integration, or LiteLLM. Record the
model slug and provider routing settings in `provenance.json` so results remain
auditable. The detailed workflow is in
[`openrouter-harness-matrix.md`](openrouter-harness-matrix.md).

## Minimal T06 Submission

For `T06_answer_stability_t4l_l99a`, a minimal external-agent submission can
score without running MD because the task is `plan_only`. The key comparison is:

```text
submission/evidence_report.json: effect.direction
vs.
tasks/T06_answer_stability_t4l_l99a/truth/experimental_truth.json: expected_direction
```

Example `evidence_report.json`:

```json
{
  "schema_version": "1.0",
  "run_id": "20260510_external_gromacs_t06",
  "task_id": "T06_answer_stability_t4l_l99a",
  "summary": "T4 lysozyme L99A is reported as destabilizing relative to WT.",
  "effect": {
    "direction": "destabilizing",
    "confidence": "high"
  },
  "limitations": [
    "Literature-only answer; no new MD was run."
  ]
}
```

Execution tasks are different: `metrics.json` alone is not enough. If a task
declares a `trajectory_rescan` check, the scorer reloads the trajectory named
in the task contract and compares frame count and NaN status.

## Schemas

Machine-readable schemas are checked in under
`benchmarks/mdagentbench/schemas/`:

- `task.schema.json`: public task contract.
- `submission_manifest.schema.json`: `submission/manifest.json` shape.
- `score.schema.json`: scorer output shape.

Use these schemas when building adapters for other agents or workflow systems.
