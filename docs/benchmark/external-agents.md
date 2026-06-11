# External Agents And Programs

MDPrepBench and MDStudyBench share an artifact-based benchmark contract. Your
agent does not need to use MDClaw. It may use OpenMM scripts, Amber, GROMACS,
MDCrow, another workflow manager, or a custom LLM runner upstream. For
MDPrepBench v0.1, a completed submission must export an OpenMM `system.xml` +
`topology.pdb` + `state.xml` bundle so the scorer can reload and rescan the
system. MDStudyBench tasks instead require study evidence such as comparative
trajectories, analysis metrics, methods drafts, provenance, and decision logs.
The scorer only compares files under `submission/` against the canonical task
contract and scorer-only truth files.

## What To Read

Give the agent the prompt as the task. The prompt names the public structures,
identifiers, protocols, and outputs it must handle; retrieving those public
sources is part of the evaluated behavior.

For external agents, first export the public package:

```bash
mdclaw export_benchmark_public_package \
  --dataset-dir benchmarks/mdprepbench \
  --output-dir benchmark_public/mdprepbench

mdclaw export_benchmark_public_package \
  --dataset-dir benchmarks/mdstudybench \
  --output-dir benchmark_public/mdstudybench
```

The evaluated agent should read only files from the selected public package:

- `benchmark_public/mdprepbench/tasks/<task_id>/prompt.md`: plain-language
  public prompt suitable for handing directly to an MD agent.
- `benchmark_public/mdprepbench/tasks/<task_id>/submission_contract.json`:
  public output contract containing required outputs, time limit, execution
  mode, failure policy, `submission_blueprint`, and machine-readable metric
  requirements. It contains no scoring checks or held-back truth.
- `benchmark_public/mdprepbench/tasks/<task_id>/submission_checklist.md`:
  per-task pre-submission checklist derived from the public contract.

Use the analogous `benchmark_public/mdstudybench/...` paths when running
MDStudyBench tasks. The StudyBench public contract uses the same
`submission_blueprint` / `submission_checklist.md` helpers, but without
MDPrepBench topology or minimization requirements.

A benchmark runner should pass those files plus the target submission directory
to the agent. It must not silently add task-specific command-line options such
as selected chains, model indices, membrane geometry, salt settings, or
preorientation flags unless those requirements are stated in the public prompt
or `submission_contract.json`. In MDClaw-generated run directories,
`agent_tasks.json`, each `task_instructions.json`, and each `agent_prompt.md`
are the files intended for the evaluated agent; `harness_tasks.json` and
`harness_instructions.json` are for validation/scoring only.

Repository-local development may read the canonical prompt at
`benchmarks/<suite>/tasks/<task_id>/prompt.md`, but do not hand the whole
canonical task directory to the evaluated agent.

Harness or evaluator code may also read:

- `benchmarks/<suite>/dataset.json`: task list, benchmark version, and
  family metadata.
- `benchmarks/<suite>/tasks/<task_id>/task.json`: runner/scorer contract,
  scoring axes, required outputs, time limits, and deterministic checks. It is
  not a solution script, should not contain task-specific execution commands,
  and should not be given to the agent under test.

Do not let the agent under test read:

- `benchmarks/<suite>/tasks/<task_id>/truth/`: scorer-only answers. For
  scientific-answer tasks, this contains the held-back experimental direction.
- `benchmarks/<suite>/tasks/<task_id>/scorer/`: judge prompts and
  evaluator material.
- `benchmarks/<suite>/tasks/<task_id>/task.json`: scorer contract with
  deterministic checks and scorer-only reference paths.

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
        minimized_structure.pdb
        minimization_report.json
        figures/
        methods.md
      score.json
  summary.json
```

Required files vary by task. Evaluator or harness code can read
`task.json.required_outputs` before launching the agent. The common files are:

- `manifest.json`: `run_id`, `task_id`, `status`, and paths to submitted
  artifacts.
- `metrics.json`: deterministic values such as topology backend, force-field,
  minimization completion, finite energy / coordinate checks, ligand RMSD, or
  analysis summary values.
- `evidence_report.json`: preparation decisions, evidence, limitations, and
  any non-default chemistry choices.
- `provenance.json`: agent, runner, backend, model, scripts, raw outputs, and
  md5 records when available. Completed prep submissions must include a
  structured `command_log` or equivalent execution log covering source
  retrieval, preparation, topology export, and minimization; listing scripts
  alone is not enough.
- Task artifacts: `prepared_structure.pdb`, `minimized_structure.pdb`,
  OpenMM topology files, and `minimization_report.json` for the current prep
  battery.

Preparation artifacts can be supplied through `manifest.outputs` rather than
copied to a benchmark-specific fixed filename:

```json
{
  "outputs": {
    "prepared_structure": "ckpt_exports/prepared_structure.pdb",
    "topology": [
      "topology/system.xml",
      "topology/topology.pdb",
      "topology/state.xml"
    ],
    "minimized_structure": "topology/topology.pdb",
    "minimization_report": "minimization_report.json"
  }
}
```

Paths are resolved relative to the `submission/` directory and must stay inside
that directory. Absolute paths and `../` escapes are rejected. This keeps the
benchmark agent-independent while requiring a common OpenMM topology artifact
format for the current prep battery.

For MDStudyBench, completed scientific-answer tasks such as S01/S02 must list
real comparative trajectory artifacts in `manifest.outputs.trajectories` and
connect `metrics.md_analysis` to `evidence_report.effect.direction`. Dry-run
study-bundle tasks such as S03 do not require trajectories; they require the
methods draft, decision log, evidence report, and structured study/report
provenance evidence.

## What The Scorer Compares

The scorer does not read chat transcripts, tool-call logs, or private runner
state. It reads `task.json`, scorer-only `truth/` when needed, scorer helper
files, and your `submission/` files.

- Preparation tasks compare required artifacts, metadata,
  residue/component counts in submitted and minimized structures, site-specific
  residue states, ligand-pose RMSD when a scorer-side reference is provided,
  topology artifact completeness, and minimization evidence. Completed prep
  submissions must provide an OpenMM topology bundle, which is reloaded for
  finite-energy rescans.
- MDPrepBench v0.1 treats artifact integrity warnings as hard failures for
  completed submissions: unsafe manifest paths, template placeholder outputs,
  missing topology/minimization artifacts, and missing execution evidence clamp
  the score to zero.
- The current prep battery does not score MDClaw-specific guardrail codes.
  MDClaw guardrails are covered by ordinary MDClaw regression tests.

For leaderboard-style runs, JSON claims are never enough when a task asks for
MD preparation artifacts. The manifest must point at the real prepared
structure, topology artifacts, minimized structure, minimization report, and
any task-specific artifacts. The scorer may verify file existence, byte floors,
residue/component content, and derived metrics. Synthetic submissions generated
by benchmark tests are for scorer CI only and should not be interpreted as
agent performance.

External sources are allowed when the prompt names or implies public retrieval
(for example PDB IDs, UniProt accessions, DOIs, and public URLs). Record what
was retrieved in `provenance.json` and explain how it was used in
`evidence_report.json`. For artifact-backed tasks, a literature or PDB-page
claim without matching submitted artifacts should not receive leaderboard
credit.

## MDCrow-Style File Registries

MDCrow stores generated files in a checkpoint directory and tracks them through
file IDs in `ckpt/paths_registry.json`. The MD benchmark suites do not need a
MDCrow-specific adapter if the final submission records the relevant files in
the standard manifest.

A generic MDCrow-style workflow is:

1. Start the benchmark run with `harness_name="mdcrow"` and
   `backend_name="mdcrow-openmm"` or the backend actually used.
2. Give the agent the task prompt and a submission directory; do not expose
   `truth/` or `scorer/`.
3. Let MDCrow run normally and produce its checkpoint files.
4. Export or copy the relevant files under `submission/`, for example
   `submission/topology/topology.pdb` and `submission/minimization_report.json`.
5. Write `manifest.json` with `outputs.topology`, `outputs.minimized_structure`,
   and any task-specific outputs pointing at those files.
6. Write `metrics.json`, `evidence_report.json`, and `provenance.json` with the
   model, runner, backend, and any raw-output hashes available.

The same pattern applies to any other file-registry agent. The benchmark
contract is the `submission/` directory, not the internal registry.

## Standard Flow

Create or choose a run directory with your harness/admin script. The benchmark
agent should only receive the task prompt and a target submission directory;
do not expose `truth/` or `scorer/`.

```bash
mkdir -p benchmark_runs/20260516_external_prep_p11/tasks/P11_prep_site_protonation_t4l_glu11/submission
```

Run your agent or external program yourself. Give it the task prompt and a
target submission directory, for example:

```bash
python run_agent.py \
  --prompt-file benchmark_public/mdprepbench/tasks/P11_prep_site_protonation_t4l_glu11/prompt.md \
  --submission-contract benchmark_public/mdprepbench/tasks/P11_prep_site_protonation_t4l_glu11/submission_contract.json \
  --submission-dir benchmark_runs/20260516_external_prep_p11/tasks/P11_prep_site_protonation_t4l_glu11/submission
```

It should solve the prompt, retrieve public sources as needed, and write real
metrics, evidence, provenance, and artifacts. When the submission is genuinely
complete, set `manifest.status` to `completed`; otherwise use `partial`,
`blocked`, or intentional `failed` as appropriate.
For MDClaw-generated run directories, the shortest safe launch prompt is:

```text
Use the md-benchmark skill. Run the task in:
benchmark_runs/<run_id>/tasks/<task_id>/agent_prompt.md
```

Use `submission_contract.json` for machine-readable requirements. In particular,
`outputs.topology` is a list of artifact paths. For prep battery v0.1 this must
be an OpenMM bundle, usually
`["topology/system.xml", "topology/topology.pdb", "topology/state.xml"]`,
and task-specific `metric_requirements` list the `metrics.json` paths that must
be populated. If `candidate_selection_requirements` is non-empty, also submit
the requested source/model-selection evidence in `source_selection.json` or an
equivalent structured `source_selection` record in provenance, metrics, or the
evidence report. Use the exported `submission_checklist.md` as the final
agent-side self-check before validation.

Validate and score:

```bash
mdclaw validate_benchmark_submission \
  --task-file benchmarks/mdprepbench/tasks/P11_prep_site_protonation_t4l_glu11/task.json \
  --submission-dir benchmark_runs/20260516_external_prep_p11/tasks/P11_prep_site_protonation_t4l_glu11/submission

mdclaw score_benchmark_submission \
  --task-file benchmarks/mdprepbench/tasks/P11_prep_site_protonation_t4l_glu11/task.json \
  --submission-dir benchmark_runs/20260516_external_prep_p11/tasks/P11_prep_site_protonation_t4l_glu11/submission \
  --run-id 20260516_external_prep_p11 \
  --output-file benchmark_runs/20260516_external_prep_p11/tasks/P11_prep_site_protonation_t4l_glu11/score.json
```

## Minimal P11 Submission

For `P11_prep_site_protonation_t4l_glu11`, a completed external-agent
submission must include a prepared structure where chain A residue 11 is named
`GLH` and contains the `HE2` side-chain hydrogen, plus topology and
minimization evidence. The key deterministic checks are:

```text
submission/metrics.json: preparation.requested_protonation_state == "GLH"
submission/prepared_structure.pdb: residue A:11 is GLH with atom HE2
submission/manifest.json: outputs.topology points to OpenMM topology artifacts
submission/manifest.json: outputs.minimized_structure points to the minimized structure
submission/minimization_report.json: minimization.completed == true and finite energies/positions
```

Example `evidence_report.json`:

```json
{
  "schema_version": "1.0",
  "run_id": "20260516_external_prep_p11",
  "task_id": "P11_prep_site_protonation_t4l_glu11",
  "summary": "Prepared T4 lysozyme chain A with residue 11 set to GLH, built topology artifacts, and verified a short minimization.",
  "limitations": [
    "Example only; include full provenance, prepared/minimized structures, topology artifacts, and minimization evidence in real submissions."
  ]
}
```

This example illustrates the report shape only; it is not a valid leaderboard
submission by itself. The structures, topology artifacts, minimization evidence,
and matching metrics are required.

## Schemas

Machine-readable schemas are checked in under each suite, for example
`benchmarks/mdprepbench/schemas/` and `benchmarks/mdstudybench/schemas/`:

- `task.schema.json`: evaluator-side task contract.
- `submission_manifest.schema.json`: `submission/manifest.json` shape.
- `score.schema.json`: scorer output shape.

Use the schemas from the same suite as the task when building runners for other
agents or workflow systems.
