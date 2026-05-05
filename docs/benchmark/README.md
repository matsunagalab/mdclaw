# MDAgentBench v1.0

MDAgentBench is a tool-agnostic benchmark contract for molecular dynamics
agents. It evaluates MDClaw as one backend, but the submission format is just
files on disk, so Claude Code, Cursor, OpenCode, raw OpenMM scripts, GROMACS
workflows, or other harnesses can be compared with the same scorer.

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
- Self-contained execution in either the `mdclaw:latest` container or a
  `mdclaw` conda env. `bin/mdclaw` auto-selects.

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

| Task | Primary | Target system | Source |
|---|---|---|---|
| T01_engine_smoke | execution | Chignolin (5AWL) | Honda 2008 |
| T02_prep_metalloenzyme_guardrail | preparation | Carbonic anhydrase (2CBA) | — |
| T03_prep_ligand_pose_t4l_benzene | preparation | T4L L99A + benzene (181L) | Morton 1995 |
| T04_exec_short_protein_md | execution | T4 lysozyme WT (2LZM) | — |
| T05_exec_restart_continue | execution | Chignolin (5AWL) | — |
| T06_answer_stability_t4l_l99a | scientific_answer | T4L L99A vs WT | Eriksson 1992 |
| T07_answer_ppi_hotspot_barnase_d39a | scientific_answer | Barnase D39A | Schreiber & Fersht 1995 |
| T08_communicate_t4l_dynamics | evidence_communication | T4L WT trajectory | — |
| T09_study_t4l_wt_vs_l99a_methods | evidence_communication | T4L WT vs L99A study | Eriksson 1992 |

## Submission Contract

Every harness writes a `submission/` directory:

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
tool calls, or harness logs. Provenance md5 references are recomputed
on the scorer side.

## Self-contained execution

Run benchmark commands either entirely inside the `mdclaw:latest` container
(Mode A) or entirely inside a `mdclaw` conda env (Mode B). **Never mix.**

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

## Per-task Workflow

```bash
# 1. Read task.json + input/, build submission/.
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

## MDClaw Adapter

`export_mdclaw_submission` creates a partial-status submission skeleton from
an MDClaw `job_dir`:

```bash
mdclaw export_mdclaw_submission \
  --job-dir job_2lzm \
  --task-id T04_exec_short_protein_md \
  --run-id <run_id> \
  --output-dir benchmark_runs/<run_id>/tasks/T04_exec_short_protein_md/submission
```

The skeleton has `manifest.status="partial"`. The agent must still fill in
task-specific deterministic metrics and evidence claims.

## Migration from v0.1

v0.1 (`MDAgentBench-Lite-v0.1`) and its 30-task lite skeleton are removed.
Existing `benchmark_runs/` directories from v0.1 remain on disk but their
`summary.json` is not forward-compatible (different aggregation math and
weighted-total formula). Re-run with v1.0 to obtain comparable scores.
