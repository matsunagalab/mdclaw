# MDPrepBench

MDPrepBench is the preparation-focused suite in the MDAgentBench benchmark
family. The current dataset is `MDPrepBench-v0.3`.

For the full suite split, see `benchmarks/README.md`. For study-level
scientific question tasks, see `docs/benchmark/mdstudybench.md`.

The benchmark is agent-agnostic. An evaluated agent receives only the public
prompt and submission contract, then writes a standard submission directory.
MDPrepBench v0.3 requires a raw OpenMM topology bundle for completed submissions
so the scorer can reload the system and rescan physical validity. The scorer reads
the submitted artifacts and private task metadata; it does not inspect chat
transcripts or MDClaw-internal state.

Prefer artifact-derived checks over self-report whenever a property is visible
in the submitted files. Current prep tasks rescan the OpenMM artifact triple and
PDB structures for water/ion content, solvent regime, residue identities,
lipid ratios, nucleic-acid content, disulfide-like SG pairs, net charge, ion
concentration, minimization sanity, and geometry. The evaluator generates
`manifest.json`, `metrics.json`, `provenance.json`, and minimization summaries;
agent-authored metadata is not part of the preparation contract.

The runner is not a hidden solution script. It may create run directories,
launch agents, enforce time limits, and call validation/scoring, but it must not
inject task-specific MDClaw command-line arguments or preparation parameters
that are absent from the public prompt and `submission_contract.json`.
`prepare_benchmark_run` writes agent-facing `agent_tasks.json` /
`task_instructions.json` separately from harness-facing `harness_tasks.json` /
`harness_instructions.json`; only the agent-facing files should be handed to
the evaluated agent.

Benchmark operator requests can stay short:

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

The automated runner defaults to 30 minutes per task. A positive
`--max-walltime-minutes-per-task` is an explicit cap. A value of `0` selects
the task's declared limit; the MDStudyBench batch wrapper uses this mode by
default. A Study cap may shorten but not extend its declared limit. The
effective value is written to each task's
`task_instructions.json` as `runtime_budget`.

If an agent exits cleanly before the public submission preflight passes,
`run_benchmark_agent` makes one tool-neutral retry by default. Ordinary
contract failures get a finalize-only prompt capped at 10 minutes. A Study
task instead gets a continuation prompt and the remaining task walltime, so it
can inspect durable DAG state and finish analysis/reporting after production.
Local Study child processes that remain in the
harness-owned process group are supervised first; detached processes are not
allowed. The runner does not choose artifacts or call an MDClaw-specific
packager on the agent's behalf. Set `--finalization-retries 0` to disable the
retry. Each attempt is recorded in `agent_run.json`,
`harness_execution.json`, and `finalization.json`.

The built-in profiles also set an explicit model unless `--agent-model` is
provided: Pi uses `spark1-vllm/deepseek-v4-flash`, Claude Code uses
`sonnet`, and Codex uses `gpt-5.4-mini`. The resolved model is written to
`run_config.json`, `summary.json`, and each task's `agent_run.json`.

For comparison, the runner also records harness-owned skill context in
`solver_context`: `none`, `skill-system`, `skill-text-injected`, or `unknown`.
Read it from `run_config.json`, `attestation.json`, `summary.json`, or each
task's `agent_run.json`; MDPrepBench submissions do not contain this metadata.
Pass `--agent-skills-dir skills` to copy a skill root into the solver workspace
for agent discovery. The runner writes `skills/`, `.agents/skills/`,
`.claude/skills/`, `.codex/skills/`, and a `package.json` with
`pi.skills=["./skills"]`, so the same option covers Codex/generic agents,
Claude Code, and Pi. For Pi, combine it with `--agent-profile pi-user`; the
default `pi` profile is a skill-free ablation and passes `--no-skills`.
By default, `run_benchmark_agent` treats MDClaw CLI use without declared MDClaw
skill context as a run-condition violation. Use `--solver-context
skill-system` for a real stage-skill run, `skill-text-injected` when external
harness text is injected into the prompt, or `--mdclaw-cli-policy allow` only
for an intentional `mdclaw-cli-only` ablation.

For agents that do not call the MDClaw CLI, the runner provides a neutral
`record_stage.py` wrapper in each task workspace and exposes it as
`stage_recording` in `task_instructions.json` and as
`$MDCLAW_BENCHMARK_STAGE_WRAPPER`. Use it to record measured source, prep,
topology, and minimization commands for the harness audit.

For Claude Code or Codex skill-system runs, set `--agent-name` and expose the
skills directory. The built-in profiles include the non-interactive
approval-bypass flags used for benchmark runs:

```bash
mdclaw run_benchmark_agent \
  --output-dir benchmark_runs \
  --run-id claude_20260613_p01 \
  --dataset-dir benchmarks/mdprepbench \
  --task-ids P01_prep_simple_monomer_t4l \
  --agent-name claude-code \
  --agent-skills-dir skills \
  --tooling-condition mdclaw-skills+cli
```

```bash
mdclaw run_benchmark_agent \
  --output-dir benchmark_runs \
  --run-id codex_20260613_p01 \
  --dataset-dir benchmarks/mdprepbench \
  --task-ids P01_prep_simple_monomer_t4l \
  --agent-name codex \
  --agent-skills-dir skills \
  --tooling-condition mdclaw-skills+cli
```

For Pi with skills enabled:

```bash
mdclaw run_benchmark_agent \
  --output-dir benchmark_runs \
  --run-id pi_skills_20260613_p01 \
  --dataset-dir benchmarks/mdprepbench \
  --task-ids P01_prep_simple_monomer_t4l \
  --agent-name pi \
  --agent-profile pi-user \
  --agent-skills-dir skills \
  --tooling-condition mdclaw-skills+cli
```

By default, `pi`, `claude-code`, and `codex` select plain non-interactive
profiles that read only the generated task prompt. Add `--agent-model <model>`
for a model override, or pass `--agent-command` for a fully custom invocation.

To run the full MDPrepBench suite for Pi, Claude Code, and Codex and score each
run, use the operator script:

```bash
conda run -n mdclaw python benchmarks/tools/run_mdprepbench_all_agents.py \
  --output-dir benchmark_runs \
  --run-id-prefix 20260613_mdprepbench_all \
  --jobs 5 --gpus 4
```

Add `--agent-skills-dir skills` for a skill-enabled comparison. The wrapper
then selects `pi-user` for Pi unless a per-agent profile override is supplied;
without the flag, the existing plain/ablation defaults remain unchanged.

This creates three runs:
`20260613_mdprepbench_all_pi`,
`20260613_mdprepbench_all_claude_code`, and
`20260613_mdprepbench_all_codex`, plus an
`*_all_agents_operator_summary.json` file. `--jobs N` runs N tasks per agent
concurrently and `--gpus M` (when > 0) round-robins `CUDA_VISIBLE_DEVICES`
across them; both pass through to `mdclaw run_benchmark_agent`, which still
scores each agent as one run. The agents themselves run sequentially. Omit
`--run-id-prefix` to use a timestamped prefix. For a quick command check without
launching agents, add `--dry-run`; for smoke tests, add `--task-ids <task_id>`.

Each completed agent run is also audited without changing its benchmark score:

- `tasks/<task_id>/workflow_audit.json` records completion, entry/re-entry
  handling, `create_node -> explain_node -> stage tool` compliance, skill and
  CLI discovery usage, wrong/suspect tool calls, raw-artifact completeness,
  runner-owned harness evidence, and reason-coded estimated extra tool calls.
- `workflow_audit_summary.json` aggregates completion/pass rates, artifact and
  evidence completeness, entry and true re-entry denominators, node-lifecycle
  success, wrong-tool reasons, and tool-call overhead across P01-P40.
- `*_all_agents_operator_summary.json` links the audit and embeds its aggregate.

`true_reentry_success_rate` is `null` when every task starts from a fresh
bootstrap. `entry_protocol_success_rate` still checks the required
post-bootstrap `inspect_job`. `estimated_extra_tool_call_count` is explicitly a
reason-coded heuristic (duplicate calls, repeated global lists, truncated CLI
discovery, and failed MDClaw invocations), not a claim about the minimum number
of scientifically necessary operations. Re-audit an existing run with:

```bash
python benchmarks/tools/audit_mdprepbench_run.py \
  benchmark_runs/<run_id>
```

**2. Manual MDClaw self-run (`mdclaw-skills+cli`).** Prepare a workspace, solve each
task, then score:

```bash
mdclaw prepare_benchmark_run \
  --output-dir benchmark_runs --run-id 20260613_mdclaw_ref \
  --dataset-dir benchmarks/mdprepbench

# For each task, hand the evaluated agent only:
#   benchmark_runs/20260613_mdclaw_ref/tasks/<task_id>/agent_prompt.md
# The agent writes tasks/<task_id>/submission/ and records real commands through
# the task-local record_stage.py wrapper named in task_instructions.json, then:

mdclaw score_benchmark_run \
  --run-dir benchmark_runs/20260613_mdclaw_ref \
  --dataset-dir benchmarks/mdprepbench
```

`prepare_benchmark_run` writes `attestation.json` (public-package hash,
`tooling_condition`), per-task `record_stage.py` wrappers, and scorer-side
`harness_tasks.json`. The wrappers append measured records to
`tasks/<task_id>/harness_execution.jsonl`; `score_benchmark_run` materializes
that JSONL as the scorer-owned `harness_execution.json` before scoring. If the
agent does not use the wrapper, strict harness checks fail.
`score_benchmark_run` produces
`summary.json` with the per-axis scores, the per-capability profile,
`tooling_condition`, and `verified`.

**3. MDClaw-free agent (e.g. MDCrow, `mdclaw-free`).** Init with
`mdclaw init_benchmark_run --tooling-condition mdclaw-free`, hand the agent only
the exported public `prompt.md`, and ask it to place raw OpenMM artifacts in
`submission/`: `topology/system.xml`, `topology/topology.pdb`,
`topology/state.xml`, `prepared_structure.pdb`, and task-specific raw files.
`score_benchmark_run` normalizes those artifacts into `manifest.json`,
`metrics.json`, `provenance.json`, md5 hashes, `minimized_structure.pdb`, and
`minimization_report.json` before scoring. The standalone
`benchmarks/tools/package_submission.py` and MDClaw package commands remain
optional helpers, not eligibility requirements. Full recipe:
`docs/benchmark/mdcrow-runner.md`.

**4. Weak baselines (discrimination check).**
`benchmarks/baselines/naive_pdbfixer_prep.py` (no-MDClaw floor) and
`empty_submission.py` (missing-artifact floor, must be rejected). See
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
messy public structural inputs into minimizable MD-ready systems whose raw
artifacts can be independently verified.

Public benchmark tasks do **not** require MDClaw-specific guardrail codes.
MDClaw guardrail behavior belongs in ordinary MDClaw unit/regression tests.

## Family

| Family | What It Tests | Scored By | Tasks |
|---|---|---|---|
| Preparation Workflow Battery | Structure retrieval, chain/ligand selection, protonation, mutations, PTMs, glycans, nucleic acids, membranes, biological assemblies, ion concentration, metal cofactors (zinc, non-zinc Mn/Ca/Mg), custom ligand parameterization, protein-protein, protein-DNA, and protein-RNA complexes, side-chain reconstruction, topology build, and minimization. | Raw file presence, PDB residue/component rescans, ligand-pose RMSD recomputation, topology/minimization rescans, and artifact integrity checks. | P01-P40 |

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
| P01_prep_simple_monomer_t4l | Simple monomer | PDB 2LZM | Clean one protein chain and build an explicit-solvent-ready system. |
| P02_prep_1ake_chain_ap5 | Chain + ligand | PDB 1AKE | Include chain A and AP5 despite chain-label ambiguity. |
| P03_prep_ligand_pose_t4l_benzene | Ligand pose | PDB 181L | Preserve the protein+BNZ complex and crystallographic benzene pose. |
| P04_prep_multi_ligand_filter_3pwb | Ligand filtering | PDB 3PWB | Include requested ligands and exclude irrelevant heterogens. |
| P05_prep_dap_dehydrogenase_nadp | Charged cofactor | PDB 1DAP | Retain the deposited NDP/NADPH-like cofactors in chains C/F. |
| P06_prep_calmodulin_ca_ions | Supported ions | PDB 1CLL | Retain four Ca2+ ions as supported ions. |
| P07_prep_rna_crystallographic_ions | Ion triage | PDB 4RBQ | Prepare RNA while preserving designated K+ ions. |
| P08_prep_t4l_l99a_branch | Point mutation | PDB 2LZM | Branch WT to L99A without renumbering drift. |
| P09_prep_t4l_double_mutant | Multi-mutant | PDB 2LZM | Apply L99A and M102Q together. |
| P10_prep_bpti_disulfides | Disulfides | PDB 5PTI | Preserve canonical BPTI disulfides and exclude experimental deuterium. |
| P11_prep_site_protonation_t4l_glu11 | Protonation | PDB 2LZM | Preserve explicit A:11 GLH protonation. |
| P12_prep_restore_deposited_sep | Deposited PTM | PDB 5K9P | Restore deposited SEP in the raw structures. |
| P13_prep_user_requested_sep | Requested PTM | PDB 1UBQ | Convert Ser20 to SEP on request. |
| P14_prep_glycoprotein_glycan | Glycan | PDB 6YA2 | Preserve N-linked glycans as glycans. |
| P15_prep_standard_dna | DNA | PDB 5MVQ | Prepare DNA without protein defaults. |
| P16_prep_standard_rna | RNA | PDB 4RBQ | Prepare RNA with an RNA-compatible force field. |
| P17_prep_dna_duplex_neutralization | DNA duplex | PDB 1BNA | Preserve both DNA chains and neutralize the system. |
| P18_prep_membrane_mixed_lipids | Membrane | PDB 2LOP | Honor POPC:POPE:CHL1 = 2:1:1. |
| P19_prep_nmr_model_selection | Candidate selection | PDB 2K39 | Select a specified NMR model before prep. |
| P20_prep_terminal_capping | Terminal capping | PDB 5AWL | Honor requested N-terminal ACE and C-terminal NME caps. |
| P21_prep_cleanup_altloc_mse_numbering | MSE cleanup | PDB 4Q5T | Convert deposited MSE to MET and exclude other unrequested nonstandard residues. |
| P22_prep_forcefield_water_fidelity | OPC fidelity | PDB 2LZM | Honor the requested 4-site OPC water model. |
| P23_prep_implicit_solvent_chignolin | Implicit solvent | PDB 5AWL | Avoid explicit water when implicit solvent is requested. |
| P24_prep_biological_assembly | Biological assembly | PDB 1STP / 2MS2 | Generate assembly 1 with the expected raw coordinates and chain count. |
| P25_prep_kcl_ion_concentration | Ion concentration | PDB 5AWL | Honor 0.30 M KCl and neutrality. |

## Submission Contract

Every preparation task requires a `submission/` directory containing only the
raw physical artifacts listed by its public contract:

```text
topology/system.xml
topology/topology.pdb
topology/state.xml
prepared_structure.pdb
task-specific raw artifacts
```

The OpenMM bundle is the scoring contract. Amber or GROMACS can still be used
upstream, but completed prep submissions must export an OpenMM-compatible
`system.xml` + `topology.pdb` + `state.xml` triple for scoring. The evaluator
normalizes this raw directory into `manifest.json`, `metrics.json`,
`provenance.json`, raw-output md5 hashes, `minimized_structure.pdb`, and
`minimization_report.json`.

For MDClaw DAG runs, the standard post-topology workflow is `topo -> min`.
Package the completed `min` state as `topology/state.xml`; the evaluator derives
the minimized PDB and minimization report from that state and the submitted
topology.

The public `submission_contract.json` records raw artifact requirements,
harness requirements, its checklist, and `submission_lifecycle`, including the
exact handoff rule:
finish work outside `submission_dir`, copy completed raw artifacts into
`submission_dir`, run the public `tools/validate_submission.py` preflight, and
exit only after preflight passes; the harness records incomplete or failed
handoffs. This is deliberately tool-neutral; MDPrepBench does not
provide or require a benchmark-specific MDClaw skill.

MDPrepBench does not accept solver-authored structured metadata. The scorer
recomputes scientific and physical facts from artifacts. Agents place only raw
physical artifacts and task-specific raw files in `submission/`; the evaluator
writes relative `manifest.outputs`, normalized metrics, provenance hashes, and
minimization reports. Strict tasks also require a harness-owned
`harness_execution.json` outside `submission/`, with measured walltime for each
required stage. Solver-written command logs and walltime estimates are not part
of the v0.3 submission contract.

Automated runs additionally write `finalization.json` per task and aggregate
`contract_diagnostics` / `harness_diagnostics` in `summary.json`. The
`weighted_total` or per-task `scientific_score` remains the artifact score;
`contract_status`, `harness_status`, `failure_class`, and
`harness_evidence_status` report whether the harness saw a complete, auditable
handoff or an unresolved control-plane failure such as `background_processes`
or `incomplete_running_work`. Study children that finish under harness
supervision and active DAGs completed through the continuation attempt are not
reported as failures. `agent_finalization_retry_count` records whether a
finalization or continuation retry was needed.

Individual tasks inspect submitted structures, OpenMM bundle contents, and
scorer-side references under `truth/`. For example, P11 checks the submitted PDB
residue state for chain A residue 11; P18/P19 verify NMR model selection by
coordinate RMSD against scorer-private references; P24 verifies assembly 1 by
coordinate RMSD plus submitted chain count; P25 recomputes neutrality and KCl
molarity from the OpenMM bundle.

## Scoring

Scoring is deterministic and artifact-as-truth: the scorer detects OpenMM by
deserializing the `system.xml` + `topology.pdb` + `state.xml` triple (not by a
declared `topology.backend` label) and recomputes physical properties from the
artifact. `metrics.json` is evaluator-generated metadata; MDPrepBench preparation
facts are scored from artifacts where possible.

Check types:

- `required_files` / `forbidden_files`
- `structure_component_rescan`
  (with task-defined residue-name aliases for backend-specific ion/lipid names)
- `minimized_structure_component_rescan`
- `pdb_residue_state`
- `disulfide_bond_rescan`
- `nucleic_content_rescan`
- `solvent_regime_rescan`
- `rmsd_recompute`
- `assembly_identity_check`
- `topology_artifact_bundle`
- `openmm_system_load` and `openmm_energy_rescan`
- `forcefield_applied_rescan` (force field applied to every atom)
- `net_charge_check` (recomputed net charge / neutrality)
- `water_model_fingerprint` (3-site vs 4/5-site classification)
- `ion_concentration_recompute` (molarity from ion count + box volume)
- `minimization_report_check`
- artifact integrity checks such as byte floors, safe normalized paths, and
  harness execution evidence

Scoring uses a small physical-validity gate plus graded per-capability partial
credit. The gate (system loads, finite energy, force field applied to every
atom, required minimized structure present) must pass or the task scores zero;
identity / fidelity / harness-audit checks then contribute weighted partial credit
that rolls up into a per-capability profile (`identity`, `physical_validity`,
`fidelity`, `provenance`). Integrity rejection stays hard: unsafe paths,
fabricated or undersized required artifacts, and missing execution
evidence clamp the score to zero. Execution evidence comes only from scorer-side
`harness_execution.json`, written outside solver-writable `submission/` by the
benchmark harness. OpenMM topology artifacts are required
for completed submissions; native-only Amber/GROMACS reload adapters can be
added later. Each run also records a `tooling_condition`, an `attestation.json`,
and a `verified` flag (see `docs/benchmark/fairness-protocol.md`).

Modified DNA/RNA is intentionally outside the core prep battery because the
current standard topology path does not support MD-ready parameterization of
modified nucleotides. Those cases belong in MDClaw regression or optional
unsupported-chemistry handling tests, not in the core prep score.

Run the exported raw preflight, then score the prepared run:

```bash
python benchmark_public/mdprepbench/tools/validate_submission.py \
  --submission-dir benchmark_runs/<run_id>/tasks/P11_prep_site_protonation_t4l_glu11/submission \
  --submission-contract benchmark_public/mdprepbench/tasks/P11_prep_site_protonation_t4l_glu11/submission_contract.json

mdclaw score_benchmark_run \
  --run-dir benchmark_runs/<run_id> \
  --dataset-dir benchmarks/mdprepbench
```

## Developer Validation

```bash
conda run -n mdclaw pytest tests/test_benchmark -q
conda run -n mdclaw mdclaw --list-json
```

For design rationale and future scientific-task planning, see
[`suite_design.md`](suite_design.md).
