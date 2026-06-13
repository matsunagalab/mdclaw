# MDPrepBench

MDPrepBench is the preparation-focused suite in the MDAgentBench benchmark
family. The current dataset is `MDPrepBench-v0.1`.

For the full suite split, see `benchmarks/README.md`. For study-level
scientific question tasks, see `docs/benchmark/mdstudybench.md`.

The benchmark is agent-agnostic. An evaluated agent receives only the public
prompt and submission contract, then writes a standard submission directory.
Prep battery v0.1 requires an OpenMM topology bundle for completed submissions
so the scorer can reload the system and rescan finite energy. The scorer reads
the submitted artifacts and private task metadata; it does not inspect chat
transcripts or MDClaw-internal state.

The runner is not a hidden solution script. It may create run directories,
launch agents, enforce time limits, and call validation/scoring, but it must not
inject task-specific MDClaw command-line arguments or preparation parameters
that are absent from the public prompt and `submission_contract.json`.
`prepare_benchmark_run` writes agent-facing `agent_tasks.json` /
`task_instructions.json` separately from harness-facing `harness_tasks.json` /
`harness_instructions.json`; only the agent-facing files should be handed to
the evaluated agent.

When using the `md-benchmark` skill, a short user instruction is enough:

```text
MDPrepBenchを run_id=prep_full_run で実行して評価して
```

The prepared run contains one short `agent_prompt.md` per task. Give that file
to the evaluated agent; keep harness/scorer files for the evaluator only. The
evaluated task agent should solve only that task. Suite-level batching belongs
to the harness/operator, not to a benchmark-wide solver script inside the
submission. `run_id` is only a label; do not infer smoke-test shortcuts or task
subsets from words in it.

## How To Run The Benchmark

There are four operator flows. All are scored by the same neutral MDClaw
scorer; only the solver differs. For held-out evaluation, follow the
public/private workspace split in `docs/benchmark/evaluation-workflow.md`:
export a public package for the solver, run the agent without evaluator
material, then score later with the private evaluator package.

**1. Automated agent runner.** Use this for repeated Pi / Claude Code / Codex
measurements. The runner exports public/private packages, creates a solver
workspace, runs one external agent command per task, records
`harness_execution.json`, scores with the private package, and writes
`summary.json`:

```bash
mdclaw run_benchmark_agent \
  --output-dir benchmark_runs \
  --run-id pi_20260613_p01 \
  --dataset-dir benchmarks/mdprepbench \
  --task-ids P01_prep_simple_monomer_t4l \
  --agent-name pi
```

The automated runner is agent-neutral by default. It does not require the
solver to use MDClaw skills, and `tooling_condition` is only a run-summary
grouping label. Add `--tooling-condition mdclaw-skills+cli`,
`--tooling-condition mdclaw-cli-only`, or `--tooling-condition mdclaw-free`
only when that label accurately describes the solver. The label never changes
the score; direct OpenMM/PDBFixer, MDCrow, MDClaw CLI-only, and MDClaw-skill
runs are all judged by the same artifact checks.

The automated runner defaults to 30 minutes per task. Increase
`--max-walltime-minutes-per-task` for slow local MD or exploratory debugging
runs.

The built-in profiles also set an explicit model unless `--agent-model` is
provided: Pi uses `deepseek-cloudflare/deepseek-v4-flash`, Claude Code uses
`sonnet`, and Codex uses `gpt-5.4-mini`. The resolved model is written to
`run_config.json`, `summary.json`, and each task's `agent_run.json`.

For comparison, the runner also records harness-owned skill context in
`solver_context`: `none`, `skill-system`, `skill-text-injected`, or `unknown`.
Read it from `run_config.json`, `attestation.json`, `summary.json`, or each
task's `agent_run.json`; do not rely on agent-written `submission/provenance.json`
alone to decide whether skills were visible to the solver.
By default, `run_benchmark_agent` treats MDClaw CLI use without MDClaw skill
context as a run-condition violation. Use `--solver-context skill-system` for a
real skill-system run, `skill-text-injected` when the skill text is injected
into the prompt, or `--mdclaw-cli-policy allow` only for an intentional
`mdclaw-cli-only` ablation.

For agents that do not call the MDClaw CLI, the runner provides a neutral
`record_stage.py` wrapper in each task workspace and exposes it as
`stage_recording` in `task_instructions.json` and as
`$MDCLAW_BENCHMARK_STAGE_WRAPPER`. Use it to record measured source, prep,
topology, and minimization commands for strict provenance.

For Claude Code or Codex, change only `--agent-name`. The built-in profiles
include the non-interactive approval-bypass flags used for benchmark runs:

```bash
mdclaw run_benchmark_agent \
  --output-dir benchmark_runs \
  --run-id claude_20260613_p01 \
  --dataset-dir benchmarks/mdprepbench \
  --task-ids P01_prep_simple_monomer_t4l \
  --agent-name claude-code
```

```bash
mdclaw run_benchmark_agent \
  --output-dir benchmark_runs \
  --run-id codex_20260613_p01 \
  --dataset-dir benchmarks/mdprepbench \
  --task-ids P01_prep_simple_monomer_t4l \
  --agent-name codex
```

By default, `pi`, `claude-code`, and `codex` select MDClaw-skill reference
profiles (`pi-mdclaw-skill`, `claude-code-mdclaw-skill`, and
`codex-mdclaw-skill`). Use `--agent-profile codex-plain`,
`--agent-profile claude-code-plain`, or `--agent-profile pi-plain` for
skill-free checks, add `--agent-model <model>` for a model override, or pass
`--agent-command` for a fully custom invocation.

**2. Manual MDClaw self-run (`mdclaw-skills+cli`).** Prepare a workspace, solve each
task, then score:

```bash
mdclaw prepare_benchmark_run \
  --output-dir benchmark_runs --run-id 20260613_mdclaw_ref \
  --dataset-dir benchmarks/mdprepbench

# For each task, hand the evaluated agent only:
#   benchmark_runs/20260613_mdclaw_ref/tasks/<task_id>/agent_prompt.md
# The agent writes tasks/<task_id>/submission/, then:

mdclaw score_benchmark_run \
  --run-dir benchmark_runs/20260613_mdclaw_ref \
  --dataset-dir benchmarks/mdprepbench
```

`prepare_benchmark_run` writes `attestation.json` (public-package hash,
`tooling_condition`) and `score_benchmark_run` produces `summary.json` with the
per-axis scores, the per-capability profile, `tooling_condition`, and
`verified`.

**3. MDClaw-free agent (e.g. MDCrow, `mdclaw-free`).** Init with
`mdclaw init_benchmark_run --tooling-condition mdclaw-free`, hand the agent only
the exported public `prompt.md`, package its own OpenMM triple with
`mdclaw package_openmm_submission` or the standalone
`benchmarks/tools/package_submission.py`, then `score_benchmark_run`. Full
recipe: `docs/benchmark/mdcrow-runner.md`.

**4. Weak baselines (discrimination check).**
`benchmarks/baselines/naive_pdbfixer_prep.py` (no-MDClaw floor) and
`json_only_no_run.py` (fabrication, must score zero). See
`benchmarks/baselines/README.md`.

Compare runs by grouping `summary.json` records on `tooling_condition` and
reading the capability profile. See `docs/benchmark/fairness-protocol.md` for
conditions, attestation, and the `verified` flag, and
`docs/benchmark/capability-coverage.md` for the task-to-check map.

## Current Scope

The current task set replaces the former mixed benchmark's preparation tasks.
Scientific question answering and study-bundle tasks live separately in
`benchmarks/mdstudybench/` as MDStudyBench; see
`docs/benchmark/mdstudybench.md`. MDPrepBench asks whether an agent can convert
messy public structural inputs into minimizable MD-ready systems with clear
provenance.

Public benchmark tasks do **not** require MDClaw-specific guardrail codes.
MDClaw guardrail behavior belongs in ordinary MDClaw unit/regression tests.

## Family

| Family | What It Tests | Scored By | Tasks |
|---|---|---|---|
| Preparation Workflow Battery | Structure retrieval, chain/ligand selection, protonation, mutations, PTMs, glycans, nucleic acids, membranes, biological assemblies, ion concentration, topology build, minimization, and provenance. | File presence, JSON metadata checks, PDB residue/component rescans, ligand-pose RMSD recomputation, topology/minimization rescans, and artifact integrity checks. | P01-P25 |

The machine-readable scoring axis is still `preparation`. Secondary qualitative
axes can be added later via LLM judge payloads, but deterministic artifact
checks are the default.

## Dataset Layout

```text
benchmarks/mdprepbench/
  dataset.json
  schemas/
    task.schema.json
    submission_manifest.schema.json
    score.schema.json
  tasks/<task_id>/
    prompt.md          # public prompt for the agent under test
    task.json          # runner/scorer metadata; not given to agents
    truth/             # scorer-only reference material when needed
```

Export the agent-visible package before giving tasks to an external agent:

```bash
mdclaw export_benchmark_public_package \
  --dataset-dir benchmarks/mdprepbench \
  --output-dir benchmark_public/mdprepbench
```

The exported package contains only `dataset.json`, submission-facing schemas,
and per-task `prompt.md`, `submission_contract.json`, and
`submission_checklist.md`. It omits `task.json`, `truth/`, and `scorer/`.

Export the evaluator-only package into a separate repository or scorer
container mount before scoring held-out runs:

```bash
mdclaw export_benchmark_private_package \
  --dataset-dir benchmarks/mdprepbench \
  --output-dir benchmark_private/mdprepbench
```

The private package contains `task.json`, held-out `truth/`, scorer-only
references, and schemas. Do not mount it into the solver workspace. Score
submissions with this private package as `--dataset-dir` when the solver ran
against the public package.

See `docs/benchmark/evaluation-workflow.md` for the full step-by-step runbook,
including where to store solver outputs and `harness_execution.json`.

## Prep Tasks

| Task | Short Name | Public Anchor | Main Requirement |
|---|---|---|---|
| P01_prep_simple_monomer_t4l | Simple monomer | PDB 2LZM | Clean one protein chain and report explicit-solvent-ready prep. |
| P02_prep_1ake_chain_ap5 | Chain + ligand | PDB 1AKE | Include chain A and AP5 despite chain-label ambiguity. |
| P03_prep_ligand_pose_t4l_benzene | Ligand pose | PDB 181L | Preserve the protein+BNZ complex and crystallographic benzene pose. |
| P04_prep_multi_ligand_filter_3pwb | Ligand filtering | PDB 3PWB | Include requested ligands and exclude irrelevant heterogens. |
| P05_prep_dap_dehydrogenase_nadp | Charged cofactor | PDB 1DAP | Retain and document the deposited NDP/NADPH-like cofactor in chains C/F. |
| P06_prep_calmodulin_ca_ions | Supported ions | PDB 1CLL | Retain four Ca2+ ions as supported ions. |
| P07_prep_rna_crystallographic_ions | Ion triage | PDB 4RBQ | Prepare RNA while preserving designated K+ ions. |
| P08_prep_t4l_l99a_branch | Point mutation | PDB 2LZM | Branch WT to L99A without renumbering drift. |
| P09_prep_t4l_double_mutant | Multi-mutant | PDB 2LZM | Apply L99A and M102Q together. |
| P10_prep_bpti_disulfides | Disulfides | PDB 5PTI | Record canonical BPTI disulfides and exclude experimental deuterium with component disposition evidence. |
| P11_prep_site_protonation_t4l_glu11 | Protonation | PDB 2LZM | Preserve explicit A:11 GLH protonation. |
| P12_prep_restore_deposited_sep | Deposited PTM | PDB 5K9P | Restore deposited SEP and PTM provenance. |
| P13_prep_user_requested_sep | Requested PTM | PDB 1UBQ | Convert Ser20 to SEP on request. |
| P14_prep_glycoprotein_glycan | Glycan | PDB 6YA2 | Preserve N-linked glycans as glycans. |
| P15_prep_standard_dna | DNA | PDB 5MVQ | Prepare DNA without protein defaults. |
| P16_prep_standard_rna | RNA | PDB 4RBQ | Prepare RNA with RNA-compatible metadata. |
| P17_prep_dna_duplex_neutralization | DNA duplex | PDB 1BNA | Preserve both DNA chains and record neutralization. |
| P18_prep_membrane_mixed_lipids | Membrane | PDB 2LOP | Honor POPC:POPE:CHL1 = 2:1:1. |
| P19_prep_nmr_model_selection | Candidate selection | PDB 2K39 | Select a specified NMR model before prep. |
| P20_prep_terminal_capping | Terminal capping | PDB 5AWL | Honor requested N-terminal ACE and C-terminal NME caps. |
| P21_prep_cleanup_altloc_mse_numbering | PDB cleanup | PDB 4Q5T | Handle MSE, altloc, numbering, and missing residues. |
| P22_prep_forcefield_water_fidelity | FF/water fidelity | PDB 2LZM | Honor supported ff19SB + OPC request. |
| P23_prep_implicit_solvent_chignolin | Implicit solvent | PDB 5AWL | Avoid explicit water when implicit solvent is requested. |
| P24_prep_biological_assembly | Biological assembly | PDB 1STP / 2MS2 | Generate assembly 1 and map output chains to source auth/label/operator provenance. |
| P25_prep_kcl_ion_concentration | Ion concentration | PDB 5AWL | Honor 0.30 M KCl and neutrality. |

## Submission Contract

Every task requires a `submission/` directory with the slim core set
(`evidence_report.json` is optional unless a specific task's contract lists it):

```text
manifest.json
metrics.json
provenance.json
prepared_structure.pdb
minimized_structure.pdb
minimization_report.json
```

Every completed prep submission must also point `manifest.outputs.topology` to
an OpenMM topology bundle and `manifest.outputs.minimized_structure` to the
post-minimization structure. The OpenMM bundle must include the `system.xml`,
`topology.pdb`, and `state.xml` artifact triple under `outputs.topology` as a
JSON list, not a role-keyed object. Amber or GROMACS can still be used upstream,
but completed prep submissions must export an OpenMM-compatible artifact triple
for scoring.

For MDClaw DAG runs, the standard post-topology workflow is `topo -> min`; the
`min` node writes `minimized_structure.pdb`, `minimized.xml`, and
`minimization_report.json`. For benchmark submissions that are packaging an
existing topology bundle, `state.xml` is the source of post-build topology-time
minimized coordinates and `topology.pdb` supplies the topology. Export the
benchmark PDB artifact explicitly:

```bash
mdclaw export_state_pdb \
  --topology-pdb-file topology/topology.pdb \
  --state-xml-file topology/state.xml \
  --output-pdb-file minimized_structure.pdb
```

Record that command in `provenance.command_log`. Do not assume
`topology.pdb` itself is the minimized structure unless your workflow documents
that it was written with minimized coordinates. This topology-time minimization
evidence is separate from the standalone `min` node used before later
equilibration nodes in ordinary MDClaw DAGs.

The public `submission_contract.json` records the agent-facing metric paths
that must be populated in `metrics.json`, plus a `submission_blueprint` showing
the minimum manifest, metrics, minimization report, and provenance shape. For
example, P01 requires `preparation.source_pdb_id`,
`preparation.solvent_model`, and `preparation.topology_ready`.
When a task requires source/model selection, the public contract also includes
`candidate_selection_requirements`; satisfy those with `source_selection.json`
listed from `manifest.outputs.source_selection`, or with equivalent structured
`source_selection` evidence in provenance, metrics, or the evidence report.
All `manifest.outputs` paths must be relative paths under `submission/`;
absolute paths and parent-directory escapes are rejected. Completed prep
submissions must include structured provenance execution evidence, usually
`provenance.command_log`, covering source, prep, topology, and minimization.
Strict tasks also require a harness-owned `harness_execution.json` outside
`submission/`, with measured walltime for each required stage. A solver-written
`provenance.json` alone is not sufficient execution evidence.

Individual tasks may inspect specific paths inside `metrics.json`, component
counts in `prepared_structure.pdb` or the minimized structure, or scorer-side
references under `truth/`.
For example, P11 checks both
`metrics.preparation.requested_protonation_state == "GLH"` and the submitted
PDB residue state for chain A residue 11.

## Scoring

Scoring is deterministic and artifact-as-truth: the scorer detects OpenMM by
deserializing the `system.xml` + `topology.pdb` + `state.xml` triple (not by a
declared `topology.backend` label) and recomputes physical properties from the
artifact. `metrics.json` is a cross-checked declaration; a declared-vs-recomputed
mismatch is an integrity warning and the recomputed value scores.

Check types:

- `required_files` / `forbidden_files`
- `json_equals`, `json_min`, `json_min_length`, `json_allowed_values`
- `structure_component_rescan`
  (with task-defined residue-name aliases for backend-specific ion/lipid names)
- `pdb_residue_state`
- `rmsd_recompute`
- `candidate_selection_check`
- `topology_artifact_bundle`
- `openmm_system_load` and `openmm_energy_rescan`
- `forcefield_applied_rescan` (force field applied to every atom)
- `net_charge_check` (recomputed net charge / neutrality)
- `water_model_fingerprint` (3-site vs 4/5-site classification)
- `ion_concentration_recompute` (molarity from ion count + box volume)
- `minimization_report_check`
- `minimized_structure_component_rescan`
- artifact integrity checks such as byte floors, safe manifest paths, and
  provenance execution evidence

Scoring uses a small physical-validity gate plus graded per-capability partial
credit. The gate (system loads, finite energy, force field applied to every
atom, required minimized structure present) must pass or the task scores zero;
identity / fidelity / provenance checks then contribute weighted partial credit
that rolls up into a per-capability profile (`identity`, `physical_validity`,
`fidelity`, `provenance`). Integrity rejection stays hard: unsafe manifest
paths, fabricated or undersized required artifacts, and missing execution
evidence clamp the score to zero. Under the strict provenance policy, execution
evidence means both solver-side `provenance.command_log` and scorer-side
`harness_execution.json`; the latter must be written outside solver-writable
`submission/` by the benchmark harness. OpenMM topology artifacts are required
for completed submissions; native-only Amber/GROMACS reload adapters can be
added later. Each run also records a `tooling_condition`, an `attestation.json`,
and a `verified` flag (see `docs/benchmark/fairness-protocol.md`).

Modified DNA/RNA is intentionally outside the core prep battery because the
current standard topology path does not support MD-ready parameterization of
modified nucleotides. Those cases belong in MDClaw regression or optional
unsupported-chemistry handling tests, not in the core prep score.

Run validation and scoring with:

```bash
conda run -n mdclaw mdclaw validate_benchmark_submission \
  --task-file benchmarks/mdprepbench/tasks/P11_prep_site_protonation_t4l_glu11/task.json \
  --submission-dir benchmark_runs/<run_id>/tasks/P11_prep_site_protonation_t4l_glu11/submission

conda run -n mdclaw mdclaw score_benchmark_submission \
  --task-file benchmarks/mdprepbench/tasks/P11_prep_site_protonation_t4l_glu11/task.json \
  --submission-dir benchmark_runs/<run_id>/tasks/P11_prep_site_protonation_t4l_glu11/submission \
  --run-id <run_id> \
  --output-file benchmark_runs/<run_id>/tasks/P11_prep_site_protonation_t4l_glu11/score.json
```

## Developer Validation

```bash
conda run -n mdclaw pytest tests/test_benchmark -q
conda run -n mdclaw mdclaw --list-json
```

For design rationale and future scientific-task planning, see
[`suite_design.md`](suite_design.md).
