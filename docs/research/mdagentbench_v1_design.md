# MDAgentBench v1.0 — Design Reference

> **v1.0.1 note**: This design reference was originally drafted before the
> openmmforcefields-unification refactor. The live contract is artifact-based
> and backend-neutral: agents submit a reloadable topology/trajectory pair via
> `manifest.outputs` or the legacy task-defined `../work/...` paths. MDClaw's
> curated topology path emits `system.system.xml` + `system.topology.pdb` +
> `system.state.xml`; external backends may submit equivalent artifacts as long
> as the scorer can reload them.

This document is the design reference for the v1.0 ground-up rebuild of
MDAgentBench. It supersedes the v0.1 pilot dataset and the
`mdclaw/benchmark_server.py` + `mdclaw/benchmark_schema.py` framework.

The v0.1 design retrospective (the four structural problems that motivated the
rebuild) is summarized in the implementation plan; this doc only captures the
v1.0 contract and the rationale for each design decision.

## Goals

1. **Tool-agnostic agent benchmark for MD workflows.** Any harness (Claude Code,
   Cursor, OpenCode, raw OpenMM scripts) can submit a `submission/` directory
   and be scored consistently against the same task contracts.
2. **Self-contained execution** in either a container (`mdclaw:latest`) or a
   conda env (`mdclaw`). No mixing.
3. **Experimental ground truth where it matters.** Every `scientific_answer`
   task is anchored to a published, reproducible experimental measurement.
4. **Hard to game.** Deterministic checks re-run the agent's claimed
   computation (re-load trajectories, re-hash files, re-compute RMSD) rather
   than trust submitted JSON.
5. **Honest partial credit.** `manifest.status="partial"` and intentional
   refusals (`"failed"`) carry distinct, well-defined scoring.

## Guiding constraints

- **Scientific axes need experimental truth** — agent judgment cannot be
  scored without an unambiguous published comparison. v1.0 uses T4
  lysozyme L99A (Eriksson et al. 1992) and barnase D39A
  (Schreiber & Fersht 1995) precisely because these two cases have
  large-magnitude, well-replicated direction signals.
- **Truth is held back from agents.** The scorer reads
  `<task>/truth/experimental_truth.json` directly; `task.json` carries no
  `expected_direction` or other ground-truth fields.
- **System selection is curator-fixed.** Agents do not pick their own PDB or
  mutation case. Every task ships a `task.json + input/*` bundle that fully
  specifies the system to be processed.
- **Self-containment is a hard architectural rule.** A run executes either
  entirely inside the `mdclaw:latest` container, or entirely inside a
  `mdclaw` conda env. The wrapper `bin/mdclaw` decides at invocation time
  and stays inside the chosen runtime; no host-vs-container shimming.

## Relation to established agent benchmarks

MDAgentBench follows the same structural pattern as established agent
benchmarks while specializing the environment and artifacts for molecular
dynamics:

- **SWE-bench style separation**: public task inputs are separate from hidden
  evaluator material. Here, agents read `task.json` and `input/`, while the
  scorer reads `truth/` and re-runs deterministic checks on submitted artifacts.
- **GAIA style capability layering**: tasks are simple to state but require
  multiple agent capabilities. MDAgentBench labels task modes as `plan_only`,
  `dry_run`, or `lite`, and v1.0.1 adds optional capability metadata so readers
  can distinguish LLM-heavy, artifact-heavy, and execution-heavy tasks.
- **WebArena / OSWorld style functional correctness**: the final state matters
  more than the action trace. MDAgentBench scores reloadable trajectories,
  recomputed RMSD, guardrail codes, metrics/caption consistency, and held-back
  scientific truth rather than chat transcripts or private tool logs.
- **AgentBench / ToolBench style tool-use evaluation**: the benchmark measures
  an agent using tools in an environment, not just a language model. The run
  metadata keeps `model`, `harness`, and `backend` separate so comparisons can
  hold two of the three fixed while varying the third.

The benchmark unit is the `submission/` directory. MDClaw, MDCrow, a raw OpenMM
script, GROMACS, or another lab agent can participate if it writes the standard
manifest, metrics, evidence, provenance, and task artifacts.

## What v1.0 measures and does not measure

MDAgentBench v1.0 primarily measures **agent workflow performance**: choosing
reasonable MD actions, producing auditable artifacts, refusing unsafe
parameterization, running or describing short simulations, and communicating
limitations. It can support LLM comparisons only when the harness and backend
are held fixed. It can support harness comparisons only when the model and
backend are held fixed. It can support backend comparisons only when the agent
and model are held fixed.

The modes intentionally emphasize different components:

- `plan_only`: LLM knowledge, literature reasoning, and calibrated scientific
  answer formation.
- `dry_run`: artifact packaging, guardrail judgment, provenance, and methods
  communication without expensive MD.
- `lite`: tool execution, MD engine reliability, trajectory emission, and
  restart handling.

v1.0 does not try to benchmark long production science, 1 ns+ statistical
convergence, HPC scheduling quality, or truth-trajectory analysis correctness.
Those belong in v1.1+ tasks so v1.0 scores remain comparable.

## Target system selection

Four protein systems anchor the v1.0 dataset; each was chosen for the depth of
its experimental record and the clarity of its expected direction.

| System | PDB | Atoms | Role | Experimental anchor |
|---|---|---|---|---|
| Chignolin (CLN025) | 5AWL | ~250 | Engine smoke (T01, T05) | Honda et al. 2008 (β-hairpin folding) |
| T4 lysozyme WT | 2LZM | ~1.6k protein | Stability scaffold (T04, T06, T08, T09) | Matthews lab thermodynamic series |
| T4L L99A | 1L90 | ~1.6k protein | Destabilizing reference (T03, T06) | Eriksson 1992, ΔΔG ≈ +5 kcal/mol |
| T4L L99A + benzene | 181L | ~1.6k + ligand | Cavity-bound ligand pose (T03) | Morton et al. 1995 internal cavity |
| Barnase-barstar | 1BRS | ~1.7k complex | PPI scaffold (T07) | Schreiber & Fersht 1995 alanine scan |
| Barnase D39A (in-silico) | derived from 1BRS | ~1.7k | Hotspot mutation (T07) | Same reference, ΔΔG_bind +4–5 kcal/mol |
| Carbonic anhydrase II | 2CBA | ~2.5k + Zn | Metalloenzyme guardrail (T02) | Standard Zn-coordinating enzyme |

Why these systems, not 8 unrelated proteins:

- **T4L is reused across 5 tasks** (T03, T04, T06, T08, T09). Same prep can be
  inspected from multiple axes, which makes harness comparison direct rather
  than confounded by per-protein difficulty.
- **Chignolin (~250 atoms) is small enough** to run on CPU in seconds, lowering
  the participation barrier (no GPU required for engine smoke).
- **Barnase-barstar** is the canonical PPI alanine-scan target; D39A on
  barnase has Kd ratios spanning 3–4 orders of magnitude across the cited
  source pools.
- **Carbonic anhydrase Zn** site is unambiguously a case where standard
  GAFF/ff14SB parameterization is unsafe, making the guardrail-refusal
  expectation crystal clear.
- Coverage: monomeric protein + cavity-bound ligand + PPI complex +
  metalloenzyme. Glycoproteins, modified nucleic acids, and membrane proteins
  are deferred to a future `MDAgentBench-Advanced` because building reliable
  experimental ground truth for them is a separate, larger project.

## Task suite v1.0 (9 tasks)

Each task fully specifies its inputs in `input/`, holds back its truth in
`truth/`, and runs scoring in two layers: deterministic checks (file presence,
JSON paths, re-computed values) and optional LLM-judge rubrics (deferred to a
later v1.x release; deterministic-only mode treats secondary axes as `null`).

### T01 — engine_smoke (chignolin)
- **Primary axis**: `execution`
- **Secondary axes**: none
- **Inputs**: `5AWL.pdb`, `solvent_spec.json` (TIP3P, 0.15 M NaCl)
- **Task intent**: 10 ps Langevin NVT on chignolin in explicit TIP3P water.
  Agent reports finite energy and no NaN.
- **Truth**: none (procedural).
- **Deterministic checks**:
  1. `execution.completed == true`, `finite_energy == true`, `no_nan == true`
     (read from `metrics.json`).
  2. **Trajectory re-scan**: scorer loads the submitted DCD/XTC, confirms
     `n_frames > 0` and that no frame contains NaN coordinates.
  3. **Solvent topology re-scan**: scorer loads `manifest.outputs.topology.0`
     and confirms the submitted topology contains at least 100 explicit water
     residues (`HOH` / `WAT` / equivalent names).

### T02 — prep_metalloenzyme_guardrail (2CBA)
- **Primary axis**: `preparation`
- **Secondary axes**: none
- **Inputs**: `2CBA.pdb`
- **Task intent**: Agent is asked to prepare an MD-ready system. Correct
  behavior is to **refuse** with a structured guardrail code rather than
  silently parameterize Zn under GAFF.
- **Truth**: `expected_guardrail.json` with allowed code `metal_containing_ligand_blocked`.
- **Deterministic checks**:
  1. `manifest.status == "failed"` AND
     `metrics.preparation.guardrail_code` ∈ allowed values.
  2. Provenance includes a real call to `detect_metal_ions` (verified by
     md5 + tool name in the decision log).
  3. No `prepared_structure.pdb` is emitted (correct refusal does not produce
     a system).

### T03 — prep_ligand_pose_t4l_benzene (181L)
- **Primary axis**: `preparation`
- **Secondary axes**: `evidence_communication`
- **Inputs**: `181L.pdb`, `ligand_reference.pdb` (crystal-coordinates-only
  benzene, extracted from 181L), `prep_request.json` (force field choice,
  solvation specification, ion concentration).
- **Task intent**: Build an MD-ready system from 181L (T4L L99A + benzene),
  then report `ligand_heavy_atom_rmsd_angstrom` between the prepared (post
  build/min/equil) ligand and the reference crystal pose.
- **Truth**: none (the reference pose is in `input/` because it must be
  visible to the agent for the comparison to be defined).
- **Deterministic checks**:
  1. `submission/prepared_structure.pdb` exists.
  2. `submission/manifest.json` records backend-equivalent topology/system
     artifacts, such as MDClaw's OpenMM XML triple or another reloadable
     topology representation.
  3. `metrics.preparation.ligand_heavy_atom_rmsd_angstrom <= 0.5` AND scorer
     re-computes the same RMSD using `mdtraj` against
     `input/ligand_reference.pdb` and confirms the agent's value matches
     within ±0.05 Å.

### T04 — exec_short_protein_md (T4L WT)
- **Primary axis**: `execution`
- **Secondary axes**: `evidence_communication`
- **Inputs**: `2LZM.pdb`, `prep_request.json` (similar to T03), MD parameters
  (`md_protocol.json`: equilibration schedule, production length 100 ps NVT).
- **Task intent**: End-to-end short explicit-water MD of T4L WT. Equilibrate
  then run at least 100 ps NVT production. A 1 ns version is intentionally
  deferred to v1.1+.
- **Truth**: none (procedural).
- **Deterministic checks**:
  1. `execution.{completed, finite_energy, no_nan} == true`.
  2. Scorer loads the submitted production trajectory, confirms ≥ 50 frames
     and no NaN.
  3. `metrics.execution.simulated_time_ps >= 100` and matches what the
     scorer reads from the trajectory file metadata.
  4. Scorer loads `manifest.outputs.topology.0` and confirms the submitted
     topology contains at least 1000 explicit water residues.

### T05 — exec_restart_continue (chignolin)
- **Primary axis**: `execution`
- **Secondary axes**: `evidence_communication`
- **Inputs**: `5AWL.pdb`, `restart_protocol.json` (split: 5 ps + 5 ps).
- **Task intent**: Run chunk 1, save state.xml, run chunk 2 from that state,
  produce concatenated trajectory.
- **Truth**: none (procedural).
- **Deterministic checks**:
  1. `execution.restart_steps_contiguous == true`.
  2. `analysis.concat_frames_match_sources == true`.
  3. Scorer cross-checks: loads `traj_chunk1.dcd` + `traj_chunk2.dcd`,
     concatenates with `mdtraj.join`, and verifies the submitted
     `traj_concat.dcd` has identical frame coordinates (within float epsilon).

### T06 — answer_stability_t4l_l99a
- **Primary axis**: `scientific_answer`
- **Secondary axes**: `evidence_communication`
- **Inputs**: `2LZM.pdb` (WT), `mutation_request.json: {"mutation": "L99A"}`,
  `references.json` (allowed citation pool: FireProtDB / S669 / Eriksson 1992).
- **Task intent**: Predict the direction of ΔΔG for T4L L99A relative to WT
  with calibrated confidence and explicit limitations.
- **Truth (in `truth/`, scorer-only)**:
  ```json
  {"expected_direction": "destabilizing",
   "ddg_kcal_per_mol_min": 3.0, "ddg_kcal_per_mol_max": 6.0,
   "source": "Eriksson et al. 1992 doi:10.1126/science.1553543"}
  ```
- **Ground-truth check**: `evidence_report.effect.direction` ∈
  {"destabilizing", "stabilizing", "neutral"} AND equal to truth.
- **Secondary** (LLM judge, deferred): confidence calibration, overclaim
  detection.

### T07 — answer_ppi_hotspot_barnase_d39a
- **Primary axis**: `scientific_answer`
- **Secondary axes**: `evidence_communication`
- **Inputs**: `1BRS.pdb`, `mutation_request.json: {"chain": "A", "mutation": "D39A"}`,
  `references.json` (SKEMPI / ASEdb / Schreiber 1995).
- **Task intent**: Predict the direction of ΔΔG_bind for barnase D39A.
- **Truth (in `truth/`, scorer-only)**:
  ```json
  {"expected_direction": "weakened_binding",
   "ddg_bind_kcal_per_mol_min": 3.0, "ddg_bind_kcal_per_mol_max": 6.0,
   "source": "Schreiber & Fersht 1995 J. Mol. Biol. 248:478"}
  ```

### T08 — communicate_t4l_dynamics
- **Primary axis**: `evidence_communication`
- **Secondary axes**: none
- **Inputs**: `2LZM.pdb` and `analysis_request.json` (RMSD reference frame,
  RMSF selection, contact cutoff).
- **Task intent**: Produce RMSD, per-residue RMSF, and CA–CA contact figures
  with captions and methods that are arithmetically traceable to the metrics.
- **Truth**: none in v1.0. The deterministic scorer checks that figure captions
  are internally consistent with submitted `metrics.json`. A future v1.1 task
  should add a curator-held reference trajectory and expected metrics.
- **Deterministic checks**:
  1. `manifest.outputs.figures` length ≥ 3.
  2. Each referenced figure file exists.
  3. Caption strings (in `evidence_report.figure_captions[].caption`) cite the
     same numeric values as in `metrics.json` (string-match within tolerance).

### T09 — study_wt_mutant_methods (T4L WT vs L99A)
- **Primary axis**: `evidence_communication`
- **Secondary axes**: `scientific_answer`
- **Inputs**: `2LZM.pdb`, `mutation_request.json`, `study_brief.md` (curator
  description of what the study should cover).
- **Task intent**: Package a WT-vs-L99A comparison study (methods.md +
  evidence_report + provenance.study.roles ≥ 2). No production MD required;
  the study is a methods package referencing existing literature.
- **Truth (shared with T06)**: `expected_direction: "destabilizing"`.
- **Deterministic checks**:
  1. `submission/methods.md` exists with non-trivial length.
  2. `provenance.study.roles` length ≥ 2 and each entry has `role`, `label`,
     `executed: false` (since this is a methods-only study).
  3. `evidence_report.effect.direction` matches truth (secondary
     scientific_answer score).

## Framework architecture

`mdclaw/benchmark/` (new package replacing `benchmark_server.py` +
`benchmark_schema.py`):

```
mdclaw/benchmark/
  __init__.py        # re-exports TOOLS dict for the registry
  models.py          # pydantic v2 BaseModels: Task, Submission, Score, RunConfig
  validation.py      # validate_task / validate_submission (uses pydantic + cross-checks)
  scoring.py         # _run_deterministic_check, _aggregate_axes, _weighted_total
  integrity.py       # _hash_file, trajectory_rescan, manifest_metrics_consistency
  judge.py           # LLM-judge plumbing: read --llm-judge-file, validate, merge into score
  run.py             # init_benchmark_run, summarize_benchmark_run, run-level records
  cli.py             # tool dispatch, --list metadata, registry hookup
```

### Pydantic models (signatures)

```python
# models.py
from typing import Literal, Optional
from pydantic import BaseModel, Field

SchemaVersion = Literal["1.0"]
TaskCategory = Literal[
    "engine_sanity", "system_preparation", "experimental_ground_truth",
    "publication_ready_evidence",
]
ScoreAxis = Literal[
    "preparation", "execution", "scientific_answer", "evidence_communication",
]
SubmissionStatus = Literal["completed", "partial", "failed", "blocked"]
ExecutionMode = Literal["lite", "dry_run", "plan_only"]
JudgeMode = Literal["deterministic", "llm_judge"]


class DeterministicCheck(BaseModel):
    check_id: str
    check_type: Literal["required_files", "json_equals", "json_max",
                        "json_min_length", "json_allowed_values",
                        "trajectory_rescan", "rmsd_recompute",
                        "metrics_caption_consistency"]
    weight: float = Field(ge=0.0, le=1.0)
    # ... type-specific params (json_path, target_file, tolerance, ...)


class GroundTruthCheck(BaseModel):
    check_id: str
    truth_field: str  # e.g., "expected_direction"
    submission_path: str  # e.g., "evidence_report.effect.direction"
    weight: float = Field(ge=0.0, le=1.0)


class TaskScoring(BaseModel):
    deterministic_checks: list[DeterministicCheck] = []
    ground_truth_checks: list[GroundTruthCheck] = []
    llm_judge_rubrics: list[str] = []  # rubric names; deferred to v1.x


class Task(BaseModel):
    schema_version: SchemaVersion = "1.0"
    task_id: str
    category: TaskCategory
    primary_score: ScoreAxis
    secondary_scores: list[ScoreAxis] = []
    execution_mode: ExecutionMode
    time_limit_minutes: int
    required_outputs: list[str]
    inputs_dir: str = "input"  # relative to task dir
    truth_dir: str = "truth"   # relative to task dir
    scoring: TaskScoring
    task_intent: str
    references: list[dict] = []
    # explicitly NO `truth` field here — held back in truth_dir


class SubmissionOutputs(BaseModel):
    metrics: Optional[str] = "metrics.json"
    provenance: Optional[str] = "provenance.json"
    evidence_report: Optional[str] = "evidence_report.json"
    decision_log: Optional[str] = "decision_log.jsonl"
    methods: Optional[str] = None
    figures: list[str] = []
    topology: list[str] = []
    trajectories: list[str] = []
    prepared_structure: Optional[str] = None


class SubmissionManifest(BaseModel):
    schema_version: SchemaVersion = "1.0"
    run_id: str
    task_id: str
    status: SubmissionStatus
    outputs: SubmissionOutputs
    limitations: list[str] = []
    errors: list[dict] = []


class CheckResult(BaseModel):
    check_id: str
    passed: bool
    score: float
    message: str


class Score(BaseModel):
    schema_version: SchemaVersion = "1.0"
    run_id: str
    task_id: str
    primary_score: ScoreAxis
    status: Literal["passed", "partial", "failed"]
    weighted_total: float
    scores: dict[ScoreAxis, Optional[float]]  # null when not evaluable
    deterministic_checks: list[CheckResult]
    ground_truth_checks: list[CheckResult]
    llm_judge: dict
    runtime: dict
    errors: list[dict] = []
```

### Aggregation arithmetic

- **Per-task `weighted_total`**:
  ```
  if secondary_scores:
      weighted_total = 0.8 * primary + 0.2 * mean(secondary_scores)
  else:
      weighted_total = primary
  ```
  Both formulas yield 1.0 at perfect performance.

- **Run-level axis aggregation** (replaces broken `_aggregate_scores`):
  ```python
  for axis in SCORE_AXES:
      tasks_for_axis = [t for t in tasks if t.primary == axis or axis in t.secondaries]
      values = [t.scores[axis] for t in tasks_for_axis if t.scores[axis] is not None]
      summary.scores[axis] = mean(values) if values else None
  ```
  A perfect agent reaches 1.0 on every populated axis.

- **`overall_score`**: unchanged, `mean(weighted_total over all tasks)`.

### Status semantics (load-bearing)

- `completed`: deterministic + ground_truth scores apply at face value.
- `partial`: every axis score multiplied by 0.6.
- `blocked`: weighted_total = 0 (failure_policy still controls whether this
  is an allowed outcome).
- `failed`: full score iff a ground_truth or guardrail check confirms an
  intentional refusal (T02 case); otherwise weighted_total = 0.

### Integrity layer

- **md5 verification**: every `provenance.raw_outputs[].md5` and
  `provenance.scripts[].md5` is recomputed by `_hash_file` and must match.
  Mismatch → `manifest.errors` entry, weighted_total -= 0.2.
- **Trajectory rescan**: any task with
  `metrics.execution.{finite_energy, no_nan}` claims is required to ship a
  trajectory referenced in `manifest.outputs.trajectories`. The scorer loads
  it with `mdtraj` and re-runs the NaN check; mismatch → those metrics are
  forced false.
- **Metrics ↔ manifest consistency**:
  `metrics.execution.completed=true` AND empty
  `manifest.outputs.trajectories` → score penalty -0.2 and warning logged.
- **Caption ↔ metrics consistency** (T08):
  `evidence_report.figure_captions[].caption` numeric strings (extracted via
  regex) must match `metrics.analysis.*` values within 1 % relative tolerance.

### Runtime modes

- `MDCLAW_RUNTIME=container`: `bin/mdclaw` dispatches every command to
  `docker run` / `singularity exec` against `mdclaw:latest`. Container is
  self-contained; the agent's helper scripts (`run_md.py`, etc.) also execute
  inside the same container via the same wrapper.
- `MDCLAW_RUNTIME=conda`: `bin/mdclaw` invokes `conda run -n mdclaw
  python -m mdclaw._cli "$@"` for every command. Helper scripts use
  `conda run -n mdclaw python script.py`. No host shimming.
- `auto` (default): SIF or singularity available → `singularity`; conda env
  `mdclaw` available → `conda`; otherwise → `docker`.
- The scoring CLI commands are NOT placed in `NATIVE_TOOLS`; they always run
  inside the chosen runtime to preserve self-containment.

## Migration

`benchmarks/mdagentbench/` is replaced atomically. Old runs in
`benchmark_runs/` remain on disk but their `summary.json` files are not
forward-compatible (different aggregation formula and axis math); a one-line
note in the dataset's `dataset.json` records the v1.0 cutover.

`benchmarks/mdagentbench_lite_v0_1/` is deleted (no callers in the codebase
or tests).

`tests/test_benchmark_server.py` is deleted; replaced by `tests/test_benchmark/`
(directory).

`mdclaw/benchmark_server.py` and `mdclaw/benchmark_schema.py` are deleted;
replaced by `mdclaw/benchmark/` package.

`mdclaw/_registry.py` updates `"benchmark": "mdclaw.benchmark"`.
`mdclaw/__init__.py` updates `__all__` similarly.

## Out of scope for v1.0

- LLM judge automation (`mdclaw run_llm_judge` tool that calls Claude API).
  Plumbing is wired but the judge file must still be supplied externally.
- Glycoprotein, modified nucleic acid, membrane protein test cases. These go
  into `MDAgentBench-Advanced` once experimental ground truth bundles are
  curated.
- Cross-run leaderboard / diff tools.
- Time/budget enforcement (currently decorative; left as v1.x).
