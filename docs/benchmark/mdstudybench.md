# MDStudyBench

MDStudyBench is the study-level suite in the MDAgentBench benchmark family.
The current dataset is `MDStudyBench-v0.2` under `benchmarks/mdstudybench/`.

MDPrepBench asks whether an agent can make an MD-ready system. MDStudyBench asks
whether an agent can turn a scientific question into comparative MD evidence,
analysis, calibrated conclusions, and an auditable study bundle.

This suite is intentionally small. Add tasks only when they cover a distinct
scientific-answer pattern with reliable hidden truth and a clear evidence
contract; broad preparation coverage belongs in MDPrepBench.

## Current Scope

All four tasks are uniform-load comparative-MD scientific-answer tasks: each runs
two short MD systems and answers a direction. The battery deliberately spans
different effect directions so a constant "mutations are destabilizing /
loss-of-function" prior cannot win — it includes a stabilizing mutation and a
ligand-affinity trend alongside the destabilizing and weakened-binding cases.

| Task | Truth direction | Focus |
|---|---|---|
| S01_stability_t4l_l99a | destabilizing | T4 lysozyme L99A cavity mutation, stability calibration |
| S02_ppi_hotspot_barnase_d39a | weakened_binding | Barnase-barstar barstar-D39A interface hotspot |
| S03_stability_nuclease_h124l | stabilizing | Staphylococcal nuclease H124L (a stabilizing mutation) |
| S04_affinity_t4l_l99a_alkylbenzene | stronger_binding | T4L L99A apolar cavity, benzene vs n-butylbenzene affinity |

Note: in PDB 1BRS, barnase is chain A and barstar is chain D/E/F. The D39 hotspot
is on **barstar** (barnase residue 39 is a lysine), so S02 mutates barstar.

## Scoring

StudyBench is scored by the same engine as MDPrepBench (`mdclaw.benchmark`), with
a study-specific check set. Like the prep suite, it is **artifact-as-truth**: the
scientific answer is bound to recomputed evidence, not to self-reported JSON.

Every task (S01–S04) scores the `scientific_answer` axis from three graded
components, so a correct answer must be both right AND grounded in the agent's
own comparative MD, not just a recall of the textbook direction:

- **Ground-truth direction** (weight 0.35) — `evidence_report.effect.direction`
  must equal the hidden experimental direction.
- **Direction grounding** (weight 0.35, `direction_grounding`) — the scorer
  recomputes the task's discriminating observable (Cα RMSF near the mutation, or
  an interface / ligand-cavity contact count) from the two submitted
  trajectories and checks that the sign of (variant − reference) is consistent
  with the direction the agent *claimed*. This is an **internal-consistency**
  check: it compares the claim against the agent's own MD, NOT against the hidden
  truth, so a data-faithful conclusion that disagrees with the literature still
  earns this credit. If the observable separation is small relative to its
  block-average noise (`|Δ| < inconclusive_sigma·σ`) the result is treated as
  inconclusive and given neutral partial credit rather than being penalized, so
  honest "the MD is inconclusive" reporting is not punished.
- **Observable recompute consistency** (weight 0.30,
  `observable_recompute_consistency`) — the `wt_value`/`mutant_value` the agent
  reports in `evidence_report.observables` must match the scorer's recomputed
  values within `tolerance_fraction`; fabricated or copied numbers degrade this
  score toward 0.
- **Hard-fail gates (weight 0)** — these clamp `weighted_total` to 0 on a
  `completed` submission if they fail, but do not inflate the score when they
  pass:
  - `trajectory_rescan` (x2): the WT and mutant trajectories
    (`outputs.trajectories[0]`/`[1]`) must load against their topologies
    (`outputs.topology[0]`/`[1]`) with real frames and no NaN coordinates.
  - `paired_mutation_topology`: the two topologies must differ by exactly the
    expected single substitution (e.g. LEU→ALA, ASP→ALA, HIS→LEU; or one swapped
    ligand residue for the affinity task). Water and counterions are ignored, and
    the check is chain-agnostic.
- **Integrity layer (`integrity_policy: reject`)** — evidence byte floor,
  template markers, evidence completeness, trajectory artifact floor and
  signatures, citation pool membership, and harness-backed provenance execution
  evidence across `source/prep/prod/analysis/report`.
- **LLM judge (mandatory, auto-run)** feeds the secondary `evidence_communication`
  axis. `mdclaw run_llm_judge` (Claude sonnet by default) is run automatically on
  the host by `score_benchmark_run` / `run_benchmark_agent` for every study task.
  Because the numeric truth is now verified deterministically (grounding +
  recompute-consistency), the judge scores only the qualitative rubrics
  `reasoning_logic` (the written argument correctly connects the reported
  observables to the conclusion), `confidence_calibration`, and
  `overclaim_detection`. The scorer itself stays offline; the judge is a separate
  host step. A study task scored without its judge is marked **incomplete** (it
  does not count as passed).

Net effect: a literature guess with no real comparative MD, garbage/copied
trajectories, or a wrong/absent mutation scores **0** (hard-fail gates) — exactly
the gamed submissions the `study_literature_guess_no_md` baseline demonstrates.
A weaker gaming attempt that runs real MD but only recalls the textbook direction
without grounding it (its own observable points the other way and its reported
numbers are fabricated) is capped near **0.35** — it keeps only the
literature-match weight and loses both grounding and recompute-consistency.

### Time budget

Every task carries a uniform `time_limit_minutes: 1440` (24 h wall-clock). The
prompts prescribe no simulation length: the agent must plan the production
length and any replicate count itself so the full workflow fits in 24 h. This MD
planning is part of the task.

## Submission contract

Unlike MDPrepBench (where the evaluator normalizes raw OpenMM artifacts), study
agents author the submission files themselves and they are scored as written.

Each task (`completed`) submits:

```text
submission/
  manifest.json          # status + outputs (paths relative to submission/)
  metrics.json           # md_analysis.* quantitative comparison
  provenance.json        # command_log
  evidence_report.json   # effect.direction, evidence.citations, evidence.md_metrics, limitations
  trajectories/          # WT then mutant production trajectories
  topology/              # WT then mutant topologies (index-aligned with trajectories)
```

`manifest.outputs.trajectories` and `manifest.outputs.topology` must each list
the WT/reference system first and the mutant/variant second so the scorer can
reload and verify the paired comparison. The exported `submission_contract.json`
carries `required_manifest_output_fields` listing these keys.

A scorer-side `harness_execution.json` (kept outside `submission/`) supplies the
trusted workflow-stage evidence; solver-written `provenance.json` is an audit
trail, not the timing source.

## How to run

Export the agent-visible package, then run and score with the shared framework
(the same scorer judges every entrant):

```bash
# 1. public package (prompt + contract + checklist only)
mdclaw export_benchmark_public_package \
  --dataset-dir benchmarks/mdstudybench \
  --output-dir benchmark_public/mdstudybench

# 2. private evaluator package (adds task.json + truth/ for scoring)
mdclaw export_benchmark_private_package \
  --dataset-dir benchmarks/mdstudybench \
  --output-dir benchmark_private/mdstudybench

# 3. prepare a run (all four tasks need real comparative MD of two systems)
mdclaw prepare_benchmark_run \
  --output-dir benchmark_runs \
  --run-id <run_id> \
  --dataset-dir benchmarks/mdstudybench \
  --execution-mode lite

# 4. score one submission directory
mdclaw score_benchmark_submission \
  --task-file benchmarks/mdstudybench/tasks/<task_id>/task.json \
  --submission-dir <run_dir>/tasks/<task_id>/submission
```

### Batch run across agents (Pi / Claude Code / Codex)

Run and score every task for each agent in one shot, like MDPrepBench:

```bash
python benchmarks/tools/run_mdstudybench_all_agents.py \
  --output-dir benchmark_runs \
  --run-id-prefix <prefix> \
  --agents pi claude-code codex
```

This wraps `mdclaw run_benchmark_agent` per agent (built-in agent profiles) and
writes an operator summary. It accepts the same `--jobs N` / `--gpus M`
parallelism flags as the MDPrepBench wrapper (study tasks are long, so parallel
GPUs help). Smoke-test the wiring with `--dry-run` and a task subset
(`--task-ids S01_stability_t4l_l99a`).

### Time limits

`run_benchmark_agent` enforces a per-task walltime and kills the agent process
group on timeout. The study batch runner defaults to
`--max-walltime-minutes-per-task 0`, which means "use each task's declared
`time_limit_minutes`" (all four tasks = 1440 min / 24 h). Pass an explicit
positive value to override with a smaller fixed cap for quick local smoke runs.

The prompts deliberately prescribe no target simulation length: the agent gets a
24 h wall-clock budget and must plan the production length and any replicate
count itself (this MD planning is part of the task). Compute note: every task
requires real comparative MD of two systems (S02 a solvated complex; S04 needs a
parameterized ligand), so provision a GPU that can turn around meaningful
WT-versus-mutant sampling for two systems within 24 h.

Reference runner under `benchmarks/baselines/` establishes the discrimination
floor: `study_literature_guess_no_md.py` (must score 0 — a confident literature
direction with fake trajectories and no real mutation).

## Dataset layout

```text
benchmarks/mdstudybench/
  dataset.json
  schemas/{task,submission_manifest,score}.schema.json
  task_specs/                  # compact maintenance source (see task_specs/README.md)
    defaults.json
    tasks/<task_id>.json
  scripts/generate_tasks.py
  tasks/<task_id>/
    prompt.md                  # public prompt for the agent under test
    task.json                  # runner/scorer metadata; not given to agents
    truth/                     # scorer-only experimental direction + citation pool
```

## Developer validation

```bash
# regenerate canonical task.json from compact specs (and check for drift)
conda run -n mdclaw python benchmarks/mdstudybench/scripts/generate_tasks.py
conda run -n mdclaw python benchmarks/mdstudybench/scripts/generate_tasks.py --check

# study scorer + anti-gaming + lifecycle coverage
conda run -n mdclaw pytest tests/test_benchmark -q
```

See `docs/benchmark/suite_design.md` for the suite-level design rationale and
`docs/benchmark/evaluation-workflow.md` for the shared evaluation workflow.
