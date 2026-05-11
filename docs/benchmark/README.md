# MDAgentBench v1.0

MDAgentBench is an artifact-based benchmark dataset for molecular dynamics
agents. It follows the same broad pattern as SWE-bench and GAIA: public task
files and inputs are separated from scorer-only truth, agents write
standardized submissions, and an independent scorer evaluates the artifacts.
MDClaw is the repository that currently ships the dataset and scorer, but it
is not part of the task contract. Any agent, model, or MD backend can be
compared by writing the same `submission/` files and running the same scorer.

The v1.0 release replaces the v0.1 pilot with:

- 9 tasks centered on chignolin, T4 lysozyme (WT + L99A), barnase-barstar,
  and carbonic anhydrase — all curator-fixed, no agent-chosen inputs.
- Held-back ground truth in `<task>/truth/` (scorer-only). The agent's
  `task.json` contains no `expected_*` fields.
- A pydantic-v2 framework that re-validates submissions (md5, trajectory
  rescan, RMSD recompute, caption ↔ metrics consistency) instead of trusting
  submitted JSON.
- Per-axis aggregation that divides by the number of tasks where each axis
  is in scope, so a perfect run reaches 1.0 on every populated axis.
- A self-contained scorer/validator runtime in either the `mdclaw:latest`
  container or a `mdclaw` conda env. The agent being evaluated may use any
  separate MD toolchain as long as it emits the benchmark artifacts.

MDAgentBench is not an LLM-only benchmark. A run measures the combined behavior
of an agent workflow, its runner, the underlying model, and the MD backend.
For model-only comparisons, keep the runner and backend fixed and record the
model/provider routing in `provenance.json` and `run_config.json`. For runner
comparisons, keep the model and backend fixed. For backend comparisons, keep
the agent and model fixed.

New to the benchmark? External agents and programs should start with
[`external-agents.md`](external-agents.md). It explains which files are public,
which files are scorer-only, what to submit, and what the scorer compares.

## Benchmark Families

MDAgentBench v1.0 is organized around four benchmark families. These families
describe the human intent of each task; the scoring contract still uses the
machine-readable `primary_score` and `secondary_scores` fields in `task.json`.

| Family | What It Tests | Scored By | Tasks |
|---|---|---|---|
| **System Preparation & Guardrails** | Whether an agent can produce MD-ready system artifacts, preserve key chemistry, and refuse unsafe parameterization when required. | Required files, structured guardrail codes, ligand-pose RMSD recomputation. | T02, T03 |
| **Execution / Engine Reliability** | Whether an agent can run short MD, produce reloadable trajectories, avoid NaNs, and continue cleanly from restart state. | Completion flags, finite-energy/no-NaN checks, trajectory rescans, restart-continuity checks. | T01, T04, T05 |
| **Scientific Answer vs Experimental Truth** | Whether an agent can answer a mutation or binding-effect question with the correct experimental direction and calibrated limits. | Held-back ground-truth comparisons in `truth/`. | T06, T07 |
| **Evidence & Methods Communication** | Whether an agent can package figures, metrics, captions, Methods text, provenance, and limitations so the result is auditable. | Figure/file checks, caption ↔ metrics consistency, Methods/provenance requirements. | T08, T09 |

Use the family names for discussion and task selection. Use the canonical
`task_id` values for submissions and scoring.

## Scores

Four composite axes, each in `[0, 1]`:

- `preparation` — system preparation quality, including pose preservation,
  structured guardrails, MD-ready artifacts.
- `execution` — short MD completion, restart correctness, finite energy,
  and trajectory artifact integrity.
- `scientific_answer` — agreement with curated experimental direction,
  plus calibrated interpretation.
- `evidence_communication` — figure / data consistency, methods
  traceability, figure readiness, and explicit limitations.

Each task names a `primary_score` and zero or more `secondary_scores`. The
per-task `weighted_total` is `0.8 * primary + 0.2 * mean(secondaries)` when
secondaries are populated, otherwise `primary` alone — both reach 1.0 at
perfect performance.

The run-level axis score is the mean of per-task values across the tasks
where that axis is in scope, returning `null` when no task scores the axis.
`overall_score` is the mean of `weighted_total` across all tasks.

## Dataset Layout

```text
benchmarks/mdagentbench/
  dataset.json
  schemas/
    task.schema.json
    submission_manifest.schema.json
    score.schema.json
  tasks/<task_id>/
    task.json
    input/             # agent-readable inputs (PDB, configs, references)
    truth/             # scorer-only ground truth — DO NOT READ FROM AGENT
    scorer/            # LLM judge prompt template (v1.x automation)
```

Pilot tasks (v1.0):

| Task | Short Name | Family | Primary | Mode | Intent |
|---|---|---|---|---|---|
| T01_engine_smoke | Engine smoke MD | Execution / Engine Reliability | execution | lite | Run tiny chignolin MD in explicit TIP3P water and prove the engine can emit a finite, reloadable trajectory. |
| T02_prep_metalloenzyme_guardrail | Metal guardrail refusal | System Preparation & Guardrails | preparation | dry_run | Refuse unsafe Zn metalloenzyme preparation with `manifest.status="failed"`, the expected structured guardrail code, and no prepared structure artifact. |
| T03_prep_ligand_pose_t4l_benzene | Ligand-pose preparation | System Preparation & Guardrails | preparation | lite | Build T4L L99A + benzene while preserving the crystal ligand pose within RMSD tolerance. |
| T04_exec_short_protein_md | Short protein MD | Execution / Engine Reliability | execution | lite | Prepare, equilibrate, and run >=100 ps explicit-water T4 lysozyme MD with trajectory and solvent-topology integrity checks. |
| T05_exec_restart_continue | Restart continuation | Execution / Engine Reliability | execution | lite | Split chignolin MD into restart chunks and verify step/frame continuity. |
| T06_answer_stability_t4l_l99a | Stability direction answer | Scientific Answer vs Experimental Truth | scientific_answer | plan_only | Predict whether T4L L99A stabilizes or destabilizes relative to WT. |
| T07_answer_ppi_hotspot_barnase_d39a | Binding hotspot answer | Scientific Answer vs Experimental Truth | scientific_answer | plan_only | Predict whether barnase D39A weakens binding in the barnase-barstar complex. |
| T08_communicate_t4l_dynamics | Figure/metrics communication | Evidence & Methods Communication | evidence_communication | dry_run | Produce dynamics figures and captions whose numeric claims match `metrics.json`. |
| T09_study_t4l_wt_vs_l99a_methods | Study methods package | Evidence & Methods Communication | evidence_communication | dry_run | Package a WT-vs-L99A study design with Methods, provenance, and evidence. |

For design rationale and system-selection notes, see
[`docs/research/mdagentbench_v1_design.md`](../research/mdagentbench_v1_design.md).

## Submission Contract

Every evaluated system writes a `submission/` directory:

```text
submission/
  manifest.json          # required
  metrics.json           # required by most tasks
  evidence_report.json   # required by most tasks
  provenance.json        # required (md5, scripts, tools_used)
  decision_log.jsonl     # optional but recommended
  figures/               # required by T08
  methods.md             # required by T09
  prepared_structure.pdb # required by T03
```

Only the artifacts are scored. The scorer never reads chat transcripts,
tool calls, or private runner logs. Provenance md5 references are recomputed
on the scorer side.

Execution tasks may submit trajectory and topology artifacts either at the
legacy task-specified `../work/...` paths or through `manifest.outputs`:

```json
{
  "outputs": {
    "trajectories": ["mdcrow/traj.dcd"],
    "topology": ["mdcrow/topology.pdb"]
  }
}
```

This manifest-driven path is intended for external agents with their own file
registries, such as MDCrow's `ckpt/paths_registry.json`. They do not need a
MDCrow-specific adapter as long as the final `submission/` files point to
reloadable artifacts.

Machine-readable schemas live under `benchmarks/mdagentbench/schemas/`:

- `task.schema.json` for task contracts.
- `submission_manifest.schema.json` for `submission/manifest.json`.
- `score.schema.json` for scorer output.

External agents should treat these schemas, the task's `required_outputs`, and
manifest artifact paths as the stable interface.

## Scorer Runtime

Run benchmark validation/scoring commands either entirely inside the
`mdclaw:latest` container (Mode A) or entirely inside a `mdclaw` conda env
(Mode B). **Never mix scorer runtimes inside one run.**

This runtime is for listing tasks, validating submissions, scoring, and
summarizing. The agent or MD program under test can run elsewhere, including a
GROMACS installation, a standalone OpenMM script, another container, or an LLM
runner, as long as it writes the required `submission/` files.

```bash
# Mode A — container self-contained
docker run --rm -v "$PWD:/work" -w /work mdclaw:latest \
  mdclaw init_benchmark_run --output-dir benchmark_runs --run-id <id>

# Mode B — conda self-contained
conda run -n mdclaw mdclaw init_benchmark_run \
  --output-dir benchmark_runs --run-id <id>

# bin/mdclaw wrapper auto-selects Mode B when a 'mdclaw' conda env exists,
# otherwise falls back to singularity → docker.
```

## Developer Validation

Use the `mdclaw` conda environment for benchmark framework checks:

```bash
# Unit, schema, scorer, and all-task dry-run coverage
conda run -n mdclaw pytest tests/test_benchmark -v

# Benchmark CLI discovery should be clean and not warn about unrelated tools
conda run -n mdclaw mdclaw list_benchmark_tasks
```

`tests/test_benchmark/test_all_task_dryrun.py` uses the synthetic submissions
from `tests/fixtures/benchmark/fake_submissions.py` to validate, score, and
summarize all nine tasks in both `honest` and `wrong` modes. It intentionally
locks down partial and failed statuses for synthetic artifacts, so changes to
scorer strictness are visible in review without requiring real MD compute.

## Per-task Workflow

```bash
# 1. The agent under test reads task.json + input/ and builds submission/.
# 2. Validate:
mdclaw validate_benchmark_submission \
  --task-file benchmarks/mdagentbench/tasks/T01_engine_smoke/task.json \
  --submission-dir benchmark_runs/<run_id>/tasks/T01_engine_smoke/submission

# 3. Score:
mdclaw score_benchmark_submission \
  --task-file benchmarks/mdagentbench/tasks/T01_engine_smoke/task.json \
  --submission-dir benchmark_runs/<run_id>/tasks/T01_engine_smoke/submission \
  --run-id <run_id> \
  --output-file benchmark_runs/<run_id>/tasks/T01_engine_smoke/score.json

# 4. Aggregate:
mdclaw summarize_benchmark_run --run-dir benchmark_runs/<run_id>
```

The runner appends durable records to:

- `benchmark_runs/runs.jsonl`
- `benchmark_runs/summaries.jsonl`

Both are last-write-wins on `run_id` so re-summarizing replaces stale rows
instead of stacking duplicates.

## Structured LLM Judge

Human judges are not part of the benchmark. Qualitative scoring is wired but
manual in v1.0:

1. Read `<task_dir>/scorer/llm_judge_prompt.json`.
2. Combine with the agent's submission and call an LLM externally.
3. Save the structured response and pass it via `--llm-judge-file`.

`mdclaw run_llm_judge` will land in v1.x. With `--judge-mode deterministic`
(default) the secondary axes return `null`; they are not silently zeroed.

## Generic Submission Template

For external agents that need a starting directory, use the generic template
tool. It does not require an MDClaw `job_dir`:

```bash
mdclaw create_benchmark_submission_template \
  --task-id T06_answer_stability_t4l_l99a \
  --run-id <run_id> \
  --output-dir benchmark_runs/<run_id>/tasks/T06_answer_stability_t4l_l99a/submission \
  --agent-name my-agent \
  --backend-name gromacs \
  --harness-name external-script
```

The template is intentionally conservative (`manifest.status="partial"`). Fill
in task-specific metrics, evidence, and artifacts before scoring.

## Migration from v0.1

v0.1 (`MDAgentBench-Lite-v0.1`) and its 30-task lite skeleton are removed.
Existing `benchmark_runs/` directories from v0.1 remain on disk but their
`summary.json` is not forward-compatible (different aggregation math and
weighted-total formula). Re-run with v1.0 to obtain comparable scores.
