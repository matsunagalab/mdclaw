---
name: md-benchmark
description: "Run MDPrepBench and MDStudyBench tasks with prompt-driven MD agents and deterministic scorer commands. Use for benchmark runs, agent submissions, and comparing MD agents."
---

# MD Benchmark

MDPrepBench and MDStudyBench evaluate prompt-driven MD agents under the
MDAgentBench family. The agent may use MDClaw, MDCrow, GROMACS, Amber, OpenMM
scripts, or another backend, but scoring is always artifact-based and
script-driven.

Use the suite that matches the task:

- `benchmarks/mdprepbench`: preparation-only tasks (`P01`-`P25`), focused on
  source retrieval, preparation, topology artifacts, minimization evidence, and
  preparation provenance.
- `benchmarks/mdstudybench`: scientific question / study tasks (`S01`-`S03`),
  focused on comparative MD evidence, analysis metrics, methods drafts,
  provenance, decision logs, and calibrated scientific answers.

For MDClaw commands, do not use the external GNU `timeout` wrapper. macOS does
not ship `timeout`; rely on the task time limit, tool/runtime errors, and
MDClaw's internal timeout handling instead.

## Rule

Give the agent the task prompt and a submission directory. The prompt is the
problem statement; it names public sources such as PDB IDs, UniProt accessions,
DOIs, URLs, protocols, and required outputs. Retrieval and provenance are part
of the evaluated behavior.

Agent-facing files:

- `<task_dir>/prompt.md`
- For external agents, prefer an exported public package from
  `mdclaw export_benchmark_public_package`; give the agent only
  `prompt.md`, `submission_contract.json`, and `submission_checklist.md`.

Canonical harness/scorer metadata:

- `benchmarks/mdprepbench/tasks/<task_id>/task.json`
- `benchmarks/mdstudybench/tasks/<task_id>/task.json`

Never expose:

- canonical `task.json` to the benchmark agent
- `<task_dir>/truth/`
- `<task_dir>/scorer/`

No fake trajectories, fake metrics, fake citations, or guessed conclusions.
Treat canonical `task.json` as runner/scorer metadata, not a solution recipe.
Harness code may read `failure_policy`, `time_limit_minutes`, required outputs,
and scoring checks before deciding whether a task can be blocked; agents should
not read it. The runner must not inject task-specific MDClaw command-line
arguments, geometry values, selected chains, model numbers, or workflow knobs
that are not present in the public prompt/submission contract. Those choices
belong to the evaluated agent and must be justified in its submission.

## Minimum Attempt Policy

If `failure_policy.blocked_by_missing_input_allowed=false` and
`failure_policy.insufficient_information_allowed=false`, do not submit
`manifest.status="blocked"` just because the run may be slow or inconvenient.
Attempt the required stages until one of these happens:

- the task succeeds
- the task reaches the task time limit
- a concrete MDClaw/tool/runtime failure stops progress

For prep tasks, attempt source retrieval, preparation, explicit solvation by
default, topology build, and a short minimization / finite-energy check. Use an
implicit topology only when the prompt explicitly asks for implicit/no-solvent
handling; in that case, do not retain crystallographic or bulk ions as explicit
particles for implicit solvent. Vacuum/no-solvent is a separate explicit prompt
choice and may keep explicit ions. Full equilibration and production are not part of the prep battery. For
execution tasks outside the prep battery, attempt the MD work requested by the
prompt. For restart tasks, run the requested chunks and attempt the
concatenation/continuity checks. For MDStudyBench comparative answer tasks, run
the requested systems before reporting an effect direction, list real trajectory
artifacts in `manifest.outputs.trajectories`, and mirror the quantitative MD
analysis in `metrics.md_analysis` and `evidence_report.evidence.md_metrics`.
For MDStudyBench dry-run evidence-bundle tasks, do not invent trajectories;
submit the requested methods, decision log, evidence report, and study/report
provenance evidence.

Before writing `manifest.status="blocked"`, record enough evidence to prove
that the task was actually attempted:

- exact `mdclaw ...` commands or sub-agent actions attempted
- deepest stage reached: `source`, `prep`, `solv`, `topo`, `eq`, `prod`,
  `analysis`, or `report`
- exit code or timeout status
- stdout/stderr or log file paths
- walltime
- concrete blocker and the next command that would have been run

Write this evidence in `provenance.json`, `evidence_report.json`, and a
decision log when useful. If no required stage was attempted, the submission is
not a valid MDClaw benchmark attempt.

## Full Benchmark Claim

Do not call a run a full benchmark unless every selected task is validated and
scored, and the MD execution / comparative-answer tasks were
genuinely attempted. If long MD tasks only received blocked placeholders, report
the run as partial or blocked-only, not full.

## MDClaw Agent

Prepare an MDClaw benchmark run from the repository root with:

```bash
mdclaw prepare_benchmark_run \
  --output-dir benchmark_runs \
  --run-id <run_id> \
  --dataset-dir benchmarks/mdprepbench \
  --execution-mode lite
```

The command writes `<run_dir>/agent_tasks.json` plus one
`task_instructions.json` per task. Each instruction points to the agent-safe
`prompt.md`, `submission_contract.json`, and the task's `submission/`
directory. Scoring metadata is written separately to `harness_tasks.json` and
`harness_instructions.json`; do not give those files to the evaluated agent.
Use `--task-ids P01_prep_simple_monomer_t4l P02_prep_1ake_chain_ap5` to run a
subset.

For StudyBench, select the study dataset explicitly:

```bash
mdclaw prepare_benchmark_run \
  --output-dir benchmark_runs \
  --run-id <run_id> \
  --dataset-dir benchmarks/mdstudybench \
  --execution-mode lite \
  --task-ids S01_stability_t4l_l99a
```

For MDClaw, launch one sub-agent per task and give it this prompt:

```text
You are the MDClaw benchmark agent for <task_id>.

Read <task_dir>/prompt.md as the task. Retrieve public sources named in the
prompt as needed.
If <task_dir>/submission_contract.json exists, read it and satisfy its
manifest_contract, submission_blueprint, metric_requirements, and
candidate_selection_requirements. If <task_dir>/submission_checklist.md exists,
use it as the final pre-submission self-check.
Do not read truth/ or scorer/.

Use MDClaw CLI tools and MDClaw skills to run real prep/minimization work when
the task asks for it. For StudyBench tasks, use `md-study` for the study plan
when the prompt asks a scientific question, then hand off to preparation,
equilibration, production, analysis, and evidence/reporting skills as needed.
Write the required benchmark submission files to <submission_dir>/.

If blocked outcomes are not allowed, do not stop because the task is long; run
until success, timeout, or a concrete tool/runtime failure.

For prep tasks, attempt source retrieval, preparation, explicit solvation unless
the prompt explicitly requests implicit/no-solvent or membrane handling,
topology build, and minimization evidence. Run topology from completed MDClaw
DAG artifacts only: explicit/membrane tasks use the completed `solv` parent,
implicit/vacuum tasks use the completed `prep` parent. Do not pass a raw/manual
PDB file directly to `build_amber_system` or `build_openmm_system`. For implicit
prep tasks, exclude explicit ions before topology; if those ions are
scientifically required, report the mode conflict instead of silently building
an implicit ion system. A prompt that explicitly asks for vacuum/no-solvent may
retain explicit ions. Explicit solvation/membrane tools first try the requested
salt concentration; if neutralization requires more ions, they may automatically
rerun packmol-memgen with `--salt_override` while keeping explicit-solvent mode
unchanged. Record the warning/metadata in provenance and metrics evidence.
For membrane prep, use `embed_in_membrane` defaults unless the prompt explicitly
specifies membrane geometry or pre-orientation; a
`packmol_packing_quality_failed` result means the packing is not MD-ready. If
the result also has
`recommended_next_action="retry_membrane_with_larger_box"`, preserve the prompt
lipid species and ratio, retry from the same prep parent with the
`retry_suggestion.suggested_parameters`, and record both attempts. The retry
suggestion should expand the lateral xy box; do not inflate z by changing
`leaflet` or `dist_wat` unless the prompt explicitly asks for it. If the prompt
explicitly fixed geometry, report the conflict instead of silently changing it.
Do not increase Packmol loop counts beyond the CLI suggestion just to make the
attempt run longer; benchmark attempts should fail fast with structured
evidence when packing remains unsuitable. When `embed_in_membrane` accepts
Packmol's `*_FORCED` output (`code="packmol_forced_output_accepted"`, the
default for membrane workflows), continue to `build_amber_system` and rely on
the topology-time minimization plus OpenMM energy rescan to decide whether the
forced layout is usable. Do not call the forced structure MD-ready until those
checks pass.
For nucleic-acid prep tasks such as P15/P16/P17, submit the
hydrogen-complete standard DNA/RNA prep artifact written by `prepare_complex`;
do not rely on topology-time hydrogen repair.
For terminal-capping tasks, use `prepare_complex --n-terminal-cap ACE` and/or
`--c-terminal-cap NME` according to the prompt. Use `--cap-termini` only when
both caps are requested. If the prompt specifies a non-default protein force
field, pass it through `--terminal-cap-forcefield` so prep-stage cap hydrogens
are rebuilt with the same protein template family.
For OpenMM / MDClaw submissions, write
manifest.status="completed" only after the required artifacts and minimization
evidence are complete. Put topology artifacts in
manifest.outputs.topology as a JSON list of paths, not as a role-keyed object:
["topology/system.xml", "topology/topology.pdb", "topology/state.xml"]. Also
write manifest.outputs.minimized_structure and
manifest.outputs.minimization_report. When the public contract sets
`required_topology_backend: openmm`, record the backend where the scorer reads
it: write `topology.backend = "openmm"` in metrics.json. The
topology_artifact_bundle / openmm_system_load / openmm_energy_rescan checks read
metrics.json at path `topology.backend`; if it is missing, every prep task fails
those three checks even when the artifact triple is valid and reloadable. Write
minimization_report.json with the canonical fields the scorer reads (under a
`minimization` object or at top level): `attempted=true`, `completed=true`,
`energy_is_finite=true`, `positions_are_finite=true`,
`atom_count_preserved=true`, plus numeric `energy_initial_kj_mol` and
`energy_final_kj_mol`. Do not use non-canonical key names such as
`energy_reloaded_state_kj_mol` / `energy_after_minimization_kj_mol` — the
minimization_report_check will not find them. Fill metrics.json paths listed in
submission_contract.json metric_requirements, especially task-specific
metrics.preparation.* entries. If the public contract lists
candidate_selection_requirements, satisfy them with `source_selection.json`
listed from manifest.outputs.source_selection or equivalent structured
source_selection evidence in provenance, metrics, or the evidence report. If
`prepare_complex` writes
component_disposition.json or excluded_components.json, copy those tool-owned
artifacts into the submission, list them in manifest.outputs when relevant, and
summarize their values in metrics/provenance; do not invent them by hand. If
`prepare_complex` writes `source_selection.json` for an NMR/model selection,
copy it into the submission and list it as `manifest.outputs.source_selection`.
Do not run full equilibration or production for prep tasks unless the prompt explicitly
asks for it. For restart tasks, run the requested chunks and attempt trajectory
concatenation/continuity checks. For StudyBench comparative answer tasks, run or
stage the requested WT/mutant or condition-pair systems, analyze task-relevant
observables, list real trajectory artifacts in `manifest.outputs.trajectories`,
and state `evidence_report.effect.direction` only after connecting the
submitted MD metrics to the conclusion. For StudyBench dry-run evidence-bundle
tasks, do not invent trajectories; submit the requested methods, decision log,
evidence report, and study/report provenance evidence.

Minimal completed prep manifest shape:

{
  "schema_version": "1.0",
  "task_id": "<task_id>",
  "status": "completed",
  "outputs": {
    "metrics": "metrics.json",
    "provenance": "provenance.json",
    "evidence_report": "evidence_report.json",
    "prepared_structure": "prepared_structure.pdb",
    "topology": [
      "topology/system.xml",
      "topology/topology.pdb",
      "topology/state.xml"
    ],
    "minimized_structure": "minimized_structure.pdb",
    "minimization_report": "minimization_report.json"
  }
}

Minimal scorer-readable metrics.json + minimization_report.json shape:

metrics.json:
{
  "schema_version": "1.0",
  "topology": { "backend": "openmm" },
  "preparation": { ...task-specific metric_requirements... }
}

minimization_report.json:
{
  "schema_version": "1.0",
  "minimization": {
    "attempted": true,
    "completed": true,
    "backend": "openmm",
    "energy_initial_kj_mol": <number>,
    "energy_final_kj_mol": <number>,
    "energy_is_finite": true,
    "positions_are_finite": true,
    "atom_count_preserved": true
  }
}

Do not fabricate. If blocked after real attempts, write
manifest.status="blocked" and explain the real blocker in evidence_report.json
and provenance/decision logs. Include public sources retrieved, commands or
sub-agent actions attempted, deepest stage reached, exit codes or timeout
status, log paths, walltime, and the next command that would have been run.
```

## MDCrow Agent

Placeholder: run MDCrow from the task prompt, then export the standard
`submission/` contract. Keep the scorer unchanged.

## Generic Agent

Placeholder: any agent is valid if it solves the prompt, retrieves public
sources as needed, and writes the standard `submission/` directory.

## Scorer

After the agent writes `submission/`, score only with scripts:

```bash
mdclaw validate_and_score_benchmark_submission \
  --task-file <canonical_task_dir>/task.json \
  --submission-dir <submission_dir> \
  --run-id <run_id> \
  --validation-output-file <run_task_dir>/validation.json \
  --output-file <run_task_dir>/score.json
```

For a run directory prepared by MDClaw, score all task submissions and write the
run summary with:

```bash
mdclaw score_benchmark_run \
  --run-dir <run_dir> \
  --dataset-dir benchmarks/mdprepbench
```

Use `--dataset-dir benchmarks/mdstudybench` for StudyBench runs.

Read the wrapper's normalized fields: `validation_success`, `score_status`,
`weighted_total`, and `benchmark_passed`. Do not infer benchmark pass/fail from
the wrapper's `success` field alone; `success` only means the evaluator wrapper
completed.
