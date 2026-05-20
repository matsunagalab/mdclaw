---
name: md-benchmark
description: "Run MDAgentBench tasks with prompt-driven MD agents and deterministic scorer commands. Use for benchmark runs, agent submissions, and comparing MD agents."
---

# MD Benchmark

MDAgentBench evaluates a prompt-driven MD agent. The agent may use MDClaw,
MDCrow, GROMACS, Amber, OpenMM scripts, or another backend, but scoring is
always artifact-based and script-driven.

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
  `prompt.md` and `submission_contract.json`.

Canonical harness/scorer metadata:

- `benchmarks/mdagentbench/tasks/<task_id>/task.json`

Never expose:

- canonical `task.json` to the benchmark agent
- `<task_dir>/truth/`
- `<task_dir>/scorer/`

No fake trajectories, fake metrics, fake citations, or guessed conclusions.
Treat canonical `task.json` as runner/scorer metadata, not a solution recipe.
Harness code may read `failure_policy`, `time_limit_minutes`, required outputs,
and scoring checks before deciding whether a task can be blocked; agents should
not read it.

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
concatenation/continuity checks. For comparative answer tasks, run the requested
systems before reporting an effect direction.

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
  --dataset-dir benchmarks/mdagentbench \
  --execution-mode lite
```

The command writes `<run_dir>/agent_tasks.json` plus one
`task_instructions.json` per task. Each instruction points to the agent-safe
`prompt.md`, `submission_contract.json`, and the task's `submission/`
directory. Use `--task-ids P01_prep_simple_monomer_t4l P02_prep_1ake_chain_ap5`
to run a subset.

For MDClaw, launch one sub-agent per task and give it this prompt:

```text
You are the MDClaw benchmark agent for <task_id>.

Read <task_dir>/prompt.md as the task. Retrieve public sources named in the
prompt as needed.
If <task_dir>/submission_contract.json exists, read it and satisfy its
manifest_contract and metric_requirements.
Do not read truth/ or scorer/.

Use MDClaw CLI tools and MDClaw skills to run real prep/minimization work when
the task asks for it.
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
manifest.outputs.minimization_report. Fill metrics.json paths listed in
submission_contract.json metric_requirements, especially task-specific
metrics.preparation.* entries. If `prepare_complex` writes
component_disposition.json or excluded_components.json, copy those tool-owned
artifacts into the submission, list them in manifest.outputs when relevant, and
summarize their values in metrics/provenance; do not invent them by hand. Do not
run full equilibration or production for prep tasks unless the prompt explicitly
asks for it. For restart tasks, run the requested chunks and attempt trajectory
concatenation/continuity checks. For comparative answer tasks, run the requested
systems before reporting an effect direction.

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
  --dataset-dir benchmarks/mdagentbench
```

Read the wrapper's normalized fields: `validation_success`, `score_status`,
`weighted_total`, and `benchmark_passed`. Do not infer benchmark pass/fail from
the wrapper's `success` field alone; `success` only means the evaluator wrapper
completed.
