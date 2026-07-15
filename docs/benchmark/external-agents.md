# External Agents And Programs

MDPrepBench and MDStudyBench share an artifact-based benchmark contract. Your
agent does not need to use MDClaw. It may use OpenMM scripts, Amber, GROMACS,
MDCrow, another workflow manager, or a custom LLM runner upstream. For
MDPrepBench v0.3, a completed submission must export an OpenMM `system.xml` +
`topology.pdb` + `state.xml` bundle so the scorer can reload and rescan the
system. MDStudyBench tasks instead require study evidence such as comparative
trajectories, analysis metrics, methods drafts, provenance, and decision logs.
The scorer only compares files under `submission/` against the canonical task
contract and scorer-only truth files.

For the recommended held-out workflow, see
`docs/benchmark/evaluation-workflow.md`. In short: give the solver only the
public package, keep the private evaluator package in a separate workspace, then
score after the solver has produced `submission/` and the harness has recorded
runtime evidence.

For repeated command-line measurements, use the automated runner instead of
hand-writing the workspaces:

```bash
mdclaw run_benchmark_agent \
  --dataset-dir benchmarks/mdprepbench \
  --run-id pi_p01_001 \
  --task-ids P01_prep_simple_monomer_t4l \
  --agent-name pi
```

The same runner works for Claude Code and Codex by changing only
`--agent-name` to `claude-code` or `codex`. The built-in profiles include the
usual non-interactive approval-bypass flags and keep Pi sessions isolated under
the run directory. Use `--agent-profile pi-user` to let Pi use normal user-wide
discovery, or `--agent-command` for a fully custom template.

The automated runner defaults to 30 minutes per task. Increase
`--max-walltime-minutes-per-task` for slow local MD or exploratory debugging
runs.

The built-in profiles pass an explicit model flag by default. The defaults are
`spark1-vllm/deepseek-v4-flash` for Pi, `sonnet` for Claude Code, and
`gpt-5.4-mini` for Codex. Use `--agent-model <model>` to override the model for
that run. Custom `--agent-command` templates can include `{{agent_model}}` to
receive the same resolved value.

The runner sets an opt-in MDClaw CLI logging hook so agent-issued `mdclaw`
commands become measured `harness_execution.json` records. Agents that never
invoke the MDClaw CLI should run the exposed stage-recording wrapper, or use
their own runner adapter, so strict stage-level provenance can pass.

The runner does not inject extra solver instructions and does not award or
deduct points based on skill visibility. Use `--tooling-condition
mdclaw-free`, `mdclaw-cli-only`, or `mdclaw-skills+cli` only to describe the
solver condition for later comparison; scoring is based on artifacts, metrics,
and execution evidence.

There is intentionally no MDPrepBench-specific skill. All solvers see the same
public prompt, contract, checklist, `tools/validate_submission.py` preflight,
and optional stage recorder. MDClaw skills may be exposed only as the normal
MDClaw workflow context for a declared `mdclaw-skills+cli` condition, not as a
benchmark-specific recipe.

To compare skill-assisted and skill-free runs, use the harness-owned
`solver_context` field in `run_config.json`, `attestation.json`,
`summary.json`, or `tasks/<task_id>/agent_run.json`. It records whether the
runner exposed no skill context, a real agent skill system, or injected skill
text into the prompt. MDPrepBench submissions do not repeat this harness-owned
declaration.
Use `--agent-skills-dir skills` when the automated runner should expose the
MDClaw skills through the agent's normal discovery mechanism. The runner copies
the source into `skills/`, `.agents/skills/`, `.claude/skills/`,
`.codex/skills/`, and writes `package.json` for Pi. For Pi, use
`--agent-profile pi-user`; the default `pi` profile is intentionally
skill-free and passes `--no-skills`.
By default, the runner flags MDClaw CLI use without MDClaw skill context as a
run-condition violation. That makes the standard comparison clean:
`mdclaw-free` means no MDClaw CLI, while `mdclaw-skills+cli` means both are
available. Pass `--mdclaw-cli-policy allow` only when deliberately measuring a
CLI-only ablation.

For direct OpenMM/PDBFixer, MDCrow, or other non-MDClaw commands, use the
runner-provided stage wrapper named in `task_instructions.json` under
`stage_recording` or in `$MDCLAW_BENCHMARK_STAGE_WRAPPER`, for example:
`$MDCLAW_BENCHMARK_STAGE_WRAPPER --stage topo -- conda run -n mdclaw python build.py`.
Those measured records are folded into the scorer-side
`harness_execution.json` just like MDClaw CLI hook records.

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

Export the private evaluator package separately and keep it out of the solver
workspace:

```bash
mdclaw export_benchmark_private_package \
  --dataset-dir benchmarks/mdprepbench \
  --output-dir benchmark_private/mdprepbench

mdclaw export_benchmark_private_package \
  --dataset-dir benchmarks/mdstudybench \
  --output-dir benchmark_private/mdstudybench
```

The evaluated agent should read only files from the selected public package:

- `benchmark_public/mdprepbench/tasks/<task_id>/prompt.md`: plain-language
  public prompt suitable for handing directly to an MD agent.
- `benchmark_public/mdprepbench/tasks/<task_id>/submission_contract.json`:
  public output contract containing required outputs, time limit, execution
  mode, failure policy, raw artifact requirements, harness requirements, and
  lifecycle. It contains no scorer checks or held-back truth.
- `benchmark_public/mdprepbench/tasks/<task_id>/submission_checklist.md`:
  per-task pre-submission checklist derived from the public contract.
- `benchmark_public/mdprepbench/tools/validate_submission.py`: tool-neutral
  public preflight. Run it against the exact `submission_dir` and the task's
  `submission_contract.json` before the agent exits.

Use the analogous `benchmark_public/mdstudybench/...` paths when running
MDStudyBench tasks. StudyBench retains its manifest-oriented
`submission_blueprint`; MDPrepBench does not expose one.

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

For real benchmark measurements, prefer separate solver and evaluator
workspaces. The private evaluator package first appears in the evaluator
workspace after solving is complete.

Agent handoff is complete only when final artifacts are in the exact
`submission_dir` and the public preflight passes. The automated runner records
incomplete or failed handoffs in `finalization.json` and includes
`contract_status`, `harness_status`,
`failure_class`, and `harness_evidence_status` in `summary.json`; this keeps
scientific artifact score separate from harness/runtime failures such as
background processes or running MDClaw DAG nodes.

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
        topology/
          system.xml
          topology.pdb
          state.xml
        prepared_structure.pdb
        <task-specific raw artifacts>
      score.json
  summary.json
```

Required raw files vary by task. External agents should read the exported
`submission_contract.json` and use its `required_outputs` field, not the private
`task.json.required_outputs` field. For MDPrepBench preparation tasks, the
benchmark evaluator derives the common metadata files:

- `manifest.json`: generated paths to normalized artifacts.
- `metrics.json`: generated objective metadata such as OpenMM backend and
  single-point energy sanity.
- `provenance.json`: generated raw-output md5 hashes and normalization metadata.
- `minimized_structure.pdb`: exported from `topology/state.xml` by the evaluator.
- `minimization_report.json`: generated from the submitted OpenMM bundle.
- Harness execution evidence is recorded outside `submission/` by the benchmark
  stage wrapper. Strict scoring requires a harness-owned
  `harness_execution.json` with measured walltime for the required stages.
  Solver command logs and walltime estimates are not part of MDPrepBench v0.3.
- Task artifacts: `prepared_structure.pdb`, OpenMM topology files, and any
  task-specific raw files named in `submission_contract.json`.

For preparation tasks, the raw artifact layout is:

```text
submission/
  topology/
    system.xml
    topology.pdb
    state.xml
  prepared_structure.pdb
  <task-specific raw artifacts>
```

The evaluator exports `minimized_structure.pdb` from `topology/state.xml`.
If you want to inspect the same PDB locally, you can run:

```bash
mdclaw export_state_pdb \
  --topology-pdb-file topology/topology.pdb \
  --state-xml-file topology/state.xml \
  --output-pdb-file minimized_structure.pdb
```

Raw paths are resolved relative to the `submission/` directory and must stay
inside that directory. Absolute paths and `../` escapes are rejected during
normalization. This keeps the benchmark agent-independent while requiring a
common OpenMM topology artifact format for the current prep battery.

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

- Preparation tasks compare required raw artifacts,
  residue/component counts in submitted and minimized structures, site-specific
  residue states, ligand-pose RMSD when a scorer-side reference is provided,
  topology artifact completeness, and minimization evidence. Completed prep
  submissions must provide an OpenMM topology bundle, which is reloaded for
  finite-energy rescans.
- Artifact-as-truth: OpenMM is detected by deserializing the
  `system.xml` + `topology.pdb` + `state.xml` triple. Force-field application,
  net charge, water-model fingerprint, and ion molarity are recomputed from
  those raw artifacts; prep agents do not submit declared metrics or backend
  labels.
- Graded scoring: a small physical-validity gate (system loads, finite energy,
  force field applied to every atom, required minimized structure present) must
  pass or the task scores zero. Identity/fidelity/harness-audit checks then give
  weighted partial credit, rolled up into a per-capability profile.
- Integrity rejection stays hard: unsafe paths, fabricated or undersized
  required artifacts, and missing harness execution evidence clamp the score to
  zero.
- The current prep battery does not score MDClaw-specific guardrail codes.
  MDClaw guardrails are covered by ordinary MDClaw regression tests.
- The contract never requires any MDClaw-specific field, so MDClaw-free agents
  (MDCrow, plain OpenMM/pdbfixer scripts) are scored identically. Put the
  agent's own OpenMM System/Topology/State artifacts under `submission/topology/`
  and let the evaluator normalize them. The standalone
  `benchmarks/tools/package_submission.py` and `mdclaw package_openmm_submission`
  are optional helpers, not required scorer inputs. See
  `docs/benchmark/mdcrow-runner.md` and `docs/benchmark/fairness-protocol.md`.

For leaderboard-style runs, JSON claims are never enough when a task asks for
MD preparation artifacts. The solver must place the real prepared structure,
OpenMM topology artifacts, and any task-specific raw artifacts in
`submission/`; the evaluator then writes the normalized manifest, metrics,
provenance hashes, minimized structure, and minimization report. The scorer may
verify file existence, byte floors, residue/component content, and derived
metrics. Synthetic submissions generated
by benchmark tests are for scorer CI only and should not be interpreted as
agent performance.

External sources are allowed when the prompt names or implies public retrieval
(for example PDB IDs, UniProt accessions, DOIs, and public URLs). MDPrepBench
credits the resulting raw physical artifacts, not an agent-authored account of
the retrieval. A literature or PDB-page claim without matching submitted
artifacts receives no leaderboard credit.

## MDCrow-Style File Registries

MDCrow stores generated files in a checkpoint directory and tracks them through
file IDs in `ckpt/paths_registry.json`. The MD benchmark suites do not need a
MDCrow-specific adapter if the final submission contains the raw OpenMM
artifacts at the public contract paths.

A generic MDCrow-style workflow is:

1. Start the benchmark run with `harness_name="mdcrow"` and
   `backend_name="mdcrow-openmm"` or the backend actually used.
2. Give the agent the task prompt and a submission directory; do not expose
   `truth/` or `scorer/`.
3. Let MDCrow run normally and produce its checkpoint files.
4. Export or copy the relevant files under `submission/`, for example
   `submission/topology/system.xml`, `submission/topology/topology.pdb`,
   `submission/topology/state.xml`, and `submission/prepared_structure.pdb`.
5. Copy any task-specific raw artifacts named by `submission_contract.json`.
6. Stop. The benchmark evaluator writes normalized metadata and hashes.

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

It should solve the prompt, retrieve public sources as needed, and write the
required raw artifacts. The evaluator generates preparation metadata and status
after validating those artifacts.
For MDClaw-generated run directories, the shortest safe launch prompt is:

```text
Run the task in:
benchmark_runs/<run_id>/tasks/<task_id>/agent_prompt.md
```

Use `submission_contract.json` for machine-readable requirements. In particular,
the raw preparation topology must be an OpenMM bundle, usually
`["topology/system.xml", "topology/topology.pdb", "topology/state.xml"]`,
and task-specific artifact requirements describe any additional files such as a
WT parent structure. The evaluator generates normalized metadata and recomputes
model/assembly choice, component presence, neutrality, water model, and ion
molarity from submitted artifacts whenever possible. Use the exported
`submission_checklist.md` as the final agent-side self-check before validation.

Run the exported raw preflight, then score the prepared run:

```bash
python benchmark_public/mdprepbench/tools/validate_submission.py \
  --submission-dir benchmark_runs/20260516_external_prep_p11/tasks/P11_prep_site_protonation_t4l_glu11/submission \
  --submission-contract benchmark_public/mdprepbench/tasks/P11_prep_site_protonation_t4l_glu11/submission_contract.json

mdclaw score_benchmark_run \
  --run-dir benchmark_runs/20260516_external_prep_p11 \
  --dataset-dir benchmarks/mdprepbench
```

## Minimal P11 Submission

For `P11_prep_site_protonation_t4l_glu11`, a completed external-agent
submission must include a prepared structure where chain A residue 11 is named
`GLH` and contains the `HE2` side-chain hydrogen, plus the raw OpenMM bundle.
The key deterministic checks are:

```text
submission/prepared_structure.pdb: residue A:11 is GLH with atom HE2
submission/topology/system.xml: OpenMM System XML
submission/topology/topology.pdb: OpenMM topology atom/residue records
submission/topology/state.xml: post-minimization OpenMM State XML
normalized minimization_report.json: finite energies/positions derived by evaluator
```

No `evidence_report.json`, solver command log, or walltime estimate belongs in a
MDPrepBench v0.3 submission. The evaluator derives normalized reports and the
harness records timing.

## Schemas

Machine-readable schemas are checked in under each suite, for example
`benchmarks/mdprepbench/schemas/` and `benchmarks/mdstudybench/schemas/`:

- `task.schema.json`: evaluator-side task contract.
- `submission_manifest.schema.json`: evaluator/Study submission manifest shape;
  it is not included in prep-only public packages.
- `score.schema.json`: scorer output shape.

Use the schemas from the same suite as the task when building runners for other
agents or workflow systems.
