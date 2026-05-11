---
name: MD Benchmark
description: "Run MDAgentBench v1.0 evaluations for MDClaw or external MD agents. Use when creating benchmark runs, producing task submissions, validating/scoring submissions, or summarizing benchmark results."
---

# MD Benchmark

Read `skills/common/preamble.md` and `skills/common/tool-output.md` before
acting.

Use this skill to evaluate MD agents with the artifact-based MDAgentBench
v1.0 contract. The benchmark scores only submitted files, not chat history,
tool calls, or harness-specific logs.

MDClaw is only one possible backend. External agents, GROMACS workflows,
standalone OpenMM scripts, and lab-specific automation can participate if they
read only the public task files and write the standard `submission/` artifacts.
Use `mdclaw` commands below as the scorer/validator interface, not as a
requirement on the agent under test.

For automated comparisons across many harnesses and OpenRouter model slugs,
read and follow `skills/md-benchmark-openrouter/SKILL.md` in addition to this
skill. The long-form guide is `docs/benchmark/openrouter-harness-matrix.md`.

## Core Contract

Each task has:

- `task.json` — public task contract the agent may read.
- `input/` — agent-readable input files (PDB, mutation requests, MD
  protocols).
- `truth/` — **scorer-only** ground truth. The agent MUST NOT read this
  directory.
- `submission/` — artifact directory the agent produces.
- `score.json` — deterministic + ground-truth + (optional) LLM-judge score.
- `summary.json` — run-level aggregate produced by `summarize_benchmark_run`.

Default checked-in dataset: `benchmarks/mdagentbench/` (9 tasks,
schema_version `1.0`).

## Task Families And Intent Map

Use this map before reading an individual `task.json`. The family names are
human-facing intent labels; the scorer still reads `primary_score` and
`secondary_scores` from the task contract.

| Family | Agent Goal | Public Inputs To Inspect | Expected Submission Evidence | Tasks |
|---|---|---|---|---|
| System Preparation & Guardrails | Build MD-ready inputs when chemistry is supported; refuse when parameterization would be unsafe. | PDB files, ligand references, prep requests. | `prepared_structure.pdb`, topology artifacts, guardrail code, ligand RMSD metrics. | T02, T03 |
| Execution / Engine Reliability | Prove the MD engine can run, save, reload, and continue short simulations without corrupt trajectories. | PDB files, solvent specs, MD/restart protocols. | Trajectories, checkpoints/states, finite-energy/no-NaN metrics, restart-continuity metrics. | T01, T04, T05 |
| Scientific Answer vs Experimental Truth | Answer a mutation or binding-effect question using the allowed public evidence, with confidence and limitations. | Structure, mutation request, allowed citation pool. | `evidence_report.effect.direction`, confidence, limitations, provenance. | T06, T07 |
| Evidence & Methods Communication | Package analysis and methods so another scientist can audit the numeric claims and workflow choices. | Analysis request, study brief, structures or generated trajectories. | Figures, captions, `metrics.json`, `methods.md`, study provenance, limitations. | T08, T09 |

Task short names:

- `T01_engine_smoke`: Engine smoke MD.
- `T02_prep_metalloenzyme_guardrail`: Metal guardrail refusal.
- `T03_prep_ligand_pose_t4l_benzene`: Ligand-pose preserving preparation.
- `T04_exec_short_protein_md`: Short end-to-end protein MD.
- `T05_exec_restart_continue`: Restart continuation correctness.
- `T06_answer_stability_t4l_l99a`: Stability direction answer.
- `T07_answer_ppi_hotspot_barnase_d39a`: Binding hotspot direction answer.
- `T08_communicate_t4l_dynamics`: Figure/metrics communication.
- `T09_study_t4l_wt_vs_l99a_methods`: Study methods package.

## Honesty Contract (read first)

The scorer trusts files, not narratives. Fabricated values, template
placeholders left in place, and `manifest.status="completed"` on work that
was never actually run are the worst failure modes for this benchmark —
worse than honestly reporting that the task was not solved.

Hard rules — violating any of these invalidates the submission:

1. **No fabricated numbers.** Every value under `metrics.json`,
   `evidence_report.json`, and any caption must come from a file the agent
   actually produced in this run. Do not write "typical Chignolin energy"
   or library-recalled numbers (a small-model agent has already done this
   once — a 10 ps NVT scoring run was reduced to hardcoded
   `final_potential_energy_kcalmol: -1238.92`, with no trajectory on disk).
   If you have no number, omit the field or set it to `null` and explain
   in `evidence_report.limitations`.
2. **No placeholder artifacts.** A 57-byte `methods.md` template, a
   53-byte stub `prepared_structure.pdb`, or a text file named
   `figure.png` is a violation. Either replace the placeholder with the
   real artifact, or remove the file from `manifest.outputs` and drop
   `manifest.status` to `partial` / `blocked`.
3. **`manifest.status` reflects reality, not effort:**
   - `completed` — every required output is real, validated, and produced
     by this run.
   - `partial` (× 0.6) — some required outputs are real, others are
     missing. Use when work was done but incomplete.
   - `blocked` (× 0) — execution did not run or failed irrecoverably.
     The scorer returns zero; that is the correct outcome.
   - `failed` — reserved for **intentional** structured refusal that the
     task contract asks for (e.g. T02 metal guardrail). Do not use
     `failed` to mean "my run crashed."
4. **`evidence_report.limitations` is mandatory** whenever `status` is not
   `completed`. State exactly what was not run and why.

The scorer re-runs deterministic checks (md5, trajectory load, RMSD
recompute, caption-vs-metrics consistency) and v1.0.x adds artifact
integrity checks (file size floors, PNG magic bytes, citation pools,
template-marker detection). Lying in `metrics.json` produces mismatches
and a lower score than an honest `blocked`.

## Anti-patterns (do not do this)

Observed real failure modes from agents. Treat these as bugs, not shortcuts:

- **"I'll write my own OpenMM script instead of using MDClaw tools."**
  Custom one-off scripts silently fail (no output, wrong forcefield/water
  pairing, missing periodic box). The MDClaw DAG tools
  (`prepare_complex`, `solvate_structure`, `build_amber_system`,
  `run_equilibration`, `run_production`) exist so the agent does not have
  to re-derive these workflows. Use them first — see § 3a below.
- **"The run was silent / produced `(No output)` — I'll fill in plausible
  numbers from training data."** Never. A silent run means the run did
  not happen. Set `manifest.status="blocked"` and record the failure in
  `decision_log.jsonl` and `evidence_report.limitations`.
- **"I'll keep the submission template defaults and flip status to
  `completed`."** Template files (53-byte PDB, 57-byte `methods.md`,
  default placeholders) are partial-by-design. Either replace them with
  real outputs or leave `status="partial"`.
- **"I'll write `figure_rmsd.png` as a text caption file."** Figures must
  be real raster/vector image files. The v1.0.x integrity layer checks
  PNG magic bytes; a text file with a `.png` extension is rejected.
- **"T06/T07/T09 are plan-only, so I can answer purely from memory."**
  Even plan-only tasks require the answer to be grounded in the public
  inputs and citation pool listed in `input/references.json`. Record
  citations under `evidence_report.evidence.citations`; the scorer
  cross-checks them against the allowed pool.

## Other Critical Rules

(The Honesty Contract above takes precedence over everything in this section.)

- Before producing a submission, read `task.json` and the task's `input/`
  directory only. **Do not read `truth/`, `scorer/`, or `expected/`.**
- The agent under test may use any MD engine or workflow. Scoring only depends
  on the files listed in `manifest.json` plus the task contract.
- Curator-fixed inputs: every task ships its own concrete PDB and config
  files in `input/`. Do not select different cases.
- For T02-style **intentional** structured refusal: set
  `manifest.status="failed"` and emit `metrics.preparation.guardrail_code`
  from the allowed set; that earns full credit when the expected guardrail
  matches.
- The scorer re-runs computations (md5, trajectory load, RMSD recompute,
  caption ↔ metrics consistency). Submitted JSON values are
  cross-validated; they are not trusted blindly.

## Submission Layout

```text
submission/
  manifest.json          # required
  metrics.json           # required by most tasks
  evidence_report.json   # required by most tasks
  provenance.json        # required by all tasks
  decision_log.jsonl     # optional, useful for traceability
  methods.md             # required by T09
  figures/               # required by T08; manifest.outputs.figures must list each
  prepared_structure.pdb # required by T03
```

## Scorer Runtime

Run benchmark validation/scoring commands either entirely inside the
`mdclaw:latest` container (Mode A) or entirely inside a `mdclaw` conda env
(Mode B). **Never mix scorer runtimes inside one run.**

```bash
# Mode A — container
docker run --rm -v "$PWD:/work" -w /work mdclaw:latest \
  mdclaw init_benchmark_run --output-dir benchmark_runs --run-id <id>

# Mode B — conda
conda run -n mdclaw mdclaw init_benchmark_run \
  --output-dir benchmark_runs --run-id <id>

# bin/mdclaw wrapper auto-selects Mode B when a 'mdclaw' conda env exists,
# otherwise falls back to singularity → docker.
```

The benchmark CLI runs in pure Python (no MD compute), but it stays inside
the chosen runtime so the dependency closure (mdclaw, pydantic, mdtraj) is
always self-consistent. The agent/runtime being evaluated can be a separate
program or container; it only needs to produce the required `submission/`
directory.

## Workflow

1. List tasks and validate the dataset:

   ```bash
   mdclaw list_benchmark_tasks
   mdclaw validate_benchmark_task --task-file \
       benchmarks/mdagentbench/tasks/T01_engine_smoke/task.json
   ```

2. Initialize a run:

   ```bash
   mdclaw init_benchmark_run \
     --output-dir benchmark_runs \
     --run-id <YYYYMMDD>_<harness>_<id> \
     --execution-mode lite \
     --judge-mode deterministic \
     --backend-name <md-engine-or-workflow> \
     --harness-name <agent-runner> \
     --model-name <llm-or-agent-model>
   ```

3. For each task, read `task.json` + `input/`, then **do the actual work**
   using the canonical MDClaw pipeline (next subsection). Write
   `submission/` under
   `benchmark_runs/<run_id>/tasks/<task_id>/submission/`.

### 3a. Canonical MDClaw pipeline (use before writing custom code)

Before reaching for a hand-written OpenMM/GROMACS script, run the MDClaw
DAG tools. They produce the artifacts the scorer is looking for and apply
the enforced forcefield-water guardrails. The standard explicit-water
chain is:

```bash
# Create the job (one physical system per job_dir)
mdclaw create_node --job-dir <run_task_dir>/job --node-id prep_001 \
  --source-file <input/structure.pdb>

# Pipeline (each node writes artifacts the agent then references in submission/)
mdclaw --job-dir <jd> --node-id prep_001 prepare_complex --select-chains A
mdclaw --job-dir <jd> --node-id solv_001 solvate_structure --water-model tip3p
mdclaw --job-dir <jd> --node-id topo_001 build_amber_system \
  --forcefield ff14SB --water-model tip3p
mdclaw --job-dir <jd> --node-id eq_001  run_equilibration  --total-time-ns 0.1
mdclaw --job-dir <jd> --node-id prod_001 run_production    --simulation-time-ns 0.01
```

Map node outputs into the submission:

- `prep_001/artifacts/merged.pdb` → `submission/prepared_structure.pdb` (T03).
- `prod_001/artifacts/trajectory.dcd` + `topo_001/artifacts/topology.pdb`
  → list under `manifest.outputs.trajectories` and
  `manifest.outputs.topology` (T01/T04/T05).
- Real energies / temperatures from the run → `metrics.json`.

Detailed runbooks live in `skills/md-prepare/SKILL.md`,
`skills/md-equilibration/SKILL.md`, `skills/md-production/SKILL.md`.
Only write a custom script if the canonical pipeline cannot represent
the task — and record the justification in `decision_log.jsonl`.

4. Validate before scoring:

   ```bash
   mdclaw validate_benchmark_submission \
     --task-file benchmarks/mdagentbench/tasks/<task_id>/task.json \
     --submission-dir benchmark_runs/<run_id>/tasks/<task_id>/submission
   ```

5. Score:

   ```bash
   mdclaw score_benchmark_submission \
     --task-file benchmarks/mdagentbench/tasks/<task_id>/task.json \
     --submission-dir benchmark_runs/<run_id>/tasks/<task_id>/submission \
     --run-id <run_id> \
     --output-file benchmark_runs/<run_id>/tasks/<task_id>/score.json
   ```

6. Summarize:

   ```bash
   mdclaw summarize_benchmark_run --run-dir benchmark_runs/<run_id>
   ```

   `runs.jsonl` and `summaries.jsonl` are appended with last-write-wins
   semantics on `run_id`.

## Pre-submission Self-check

Run this checklist **before** flipping `manifest.status` to `completed` and
calling `score_benchmark_submission`. If any item fails, downgrade to
`partial` or `blocked` rather than ignoring it.

For every task:

- [ ] `manifest.json` references only files that actually exist on disk
  under `submission/`.
- [ ] No file under `submission/` is the template default. Sanity-check
  sizes: a real `prepared_structure.pdb` is typically > 5 KB; a real
  `methods.md` is typically > 1 KB; a real figure (`.png`/`.svg`) is not
  a text file.
- [ ] Every numeric field in `metrics.json` traces back to a file the
  agent produced in this run (not training-data recall, not a value
  copied from another task's example).
- [ ] If `manifest.status != "completed"`,
  `evidence_report.limitations` lists what was not run and why.

Additionally for execution tasks (T01/T04/T05):

- [ ] `manifest.outputs.trajectories` and `manifest.outputs.topology`
  point at files that `mdtraj.load()` can open. The scorer reloads them.
- [ ] `metrics.execution.no_nan` is `true` only after the agent actually
  ran a NaN scan over the trajectory.

For T03 (ligand preparation): `prepared_structure.pdb` contains both the
protein and the ligand with reasonable coordinates.

For T06 / T07 (scientific answer): `evidence_report.evidence.citations`
draws from `input/references.json` (`allowed_source_pools` + `primary_reference.doi`).
The v1.0.x integrity layer cross-checks this pool.

For T08 / T09 (communication): every caption number appears in
`metrics.json` with the same value, and `methods.md` has at least two
H2 sections (`## Methods`, `## Limitations`).

`mdclaw validate_benchmark_submission` catches some — but not all — of
these. Validation success does not mean honesty.

## Failure Recovery

When a pipeline step fails (silent OpenMM, missing forcefield, OOM,
container error, ligand parameterization refused, …):

1. **Do not** fabricate metrics or fall back to "typical values."
2. Capture the failure in `decision_log.jsonl`:

   ```json
   {"event": "execution_failed", "node": "prod_001",
    "stderr_tail": "...", "next_action": "mark blocked"}
   ```

3. Set `manifest.status` according to what is actually on disk:
   - Nothing usable → `blocked`.
   - Some real artifacts (prep succeeded, prod failed) → `partial`,
     and list only the real artifacts under `manifest.outputs`.
4. Populate `evidence_report.limitations` with a one-line root cause and
   what would be needed to recover (e.g. "OpenMM CUDA platform
   unavailable in container; would re-run with `--platform CPU`").
5. Re-run `validate_benchmark_submission` to confirm the partial / blocked
   manifest is internally consistent, then score. A `blocked` submission
   with honest evidence scores zero — which is the correct outcome, not
   something to hide.

## Pilot tasks (v1.0)

| Task | Primary axis | Mode | Target system |
|---|---|---|---|
| T01_engine_smoke | execution | lite | Chignolin (5AWL), explicit TIP3P water |
| T02_prep_metalloenzyme_guardrail | preparation | dry_run | Carbonic anhydrase II (2CBA) |
| T03_prep_ligand_pose_t4l_benzene | preparation | lite | T4L L99A + benzene (181L) |
| T04_exec_short_protein_md | execution | lite | T4 lysozyme WT (2LZM), explicit TIP3P water |
| T05_exec_restart_continue | execution | lite | Chignolin (5AWL) |
| T06_answer_stability_t4l_l99a | scientific_answer | plan_only | T4 lysozyme L99A |
| T07_answer_ppi_hotspot_barnase_d39a | scientific_answer | plan_only | Barnase D39A on 1BRS |
| T08_communicate_t4l_dynamics | evidence_communication | dry_run | T4 lysozyme WT |
| T09_study_t4l_wt_vs_l99a_methods | evidence_communication | dry_run | T4L WT vs L99A |

Five tasks reuse T4 lysozyme as a shared scaffold so scoring is comparable
across harnesses.

## Structured LLM Judge (optional, deferred to v1.x automation)

Deterministic mode is the default. To add qualitative scoring:

1. An external evaluator reads `<task_dir>/scorer/llm_judge_prompt.json`,
   combines it with the agent's submission, calls an LLM, and saves the
   structured response (a JSON object with `enabled`, `judge_model`,
   `temperature`, `scores`, `violations`).
2. Pass that file via `--llm-judge-file` to `score_benchmark_submission`.
3. Score axes that have a populated secondary will reflect the judge values;
   axes without a judge entry remain `null`.

A `mdclaw run_llm_judge` automation tool will land in v1.x. For now the
judge file must be produced externally.

## External Agent Quick Guide

For external agents and programs, the shortest contract is:

1. Read `benchmarks/mdagentbench/dataset.json` to choose a task.
2. Read only `tasks/<task_id>/task.json` and `tasks/<task_id>/input/`.
3. Do not read `truth/` or `scorer/`.
4. Write `benchmark_runs/<run_id>/tasks/<task_id>/submission/`.
5. Validate, score, and summarize with the `mdclaw` benchmark commands.

The detailed external-user guide is `docs/benchmark/external-agents.md`.
For OpenRouter-backed harness/model matrix runs, use
`docs/benchmark/openrouter-harness-matrix.md`.

## Generic Submission Template

For non-MDClaw agents, create a scaffold without a `job_dir`:

```bash
mdclaw create_benchmark_submission_template \
  --task-id <task_id> \
  --run-id <run_id> \
  --output-dir benchmark_runs/<run_id>/tasks/<task_id>/submission \
  --agent-name <agent-name> \
  --backend-name <openmm|gromacs|other> \
  --harness-name <runner>
```

Then fill task-specific metrics, evidence, and artifacts before scoring.

## Optional MDClaw Job Adapter

When a completed MDClaw `job_dir` already exists, start with:

```bash
mdclaw export_mdclaw_submission \
  --job-dir <job_dir> \
  --task-id <task_id> \
  --run-id <run_id> \
  --output-dir benchmark_runs/<run_id>/tasks/<task_id>/submission
```

The adapter writes a partial-status skeleton (manifest, metrics, provenance,
evidence_report). Fill in task-specific deterministic values yourself.

## Dataset Maintenance

The v1.0 dataset is curator-authored and lives at
`benchmarks/mdagentbench/`. Schema files are generated from the pydantic
models:

```bash
mdclaw write_benchmark_schemas --output-dir benchmarks/mdagentbench/schemas
```

`create_pilot_benchmark` is now a no-op when the dataset already exists; it
returns success and does not regenerate task contracts.
