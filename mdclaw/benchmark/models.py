"""Pydantic v2 models for MDAgentBench v1.0.

These types are the single source of truth for task / submission / score
shapes. JSON files on disk are parsed through these models; this gives us
real schema validation (vs. the v0.1 hand-coded validator) and fail-fast
behavior on malformed input.

The schema_version is locked to ``"1.0"`` for this release. A future v1.1 with
backward-compatible extensions can add Literal["1.0", "1.1"] without breaking
existing callers.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enumerated string types

SchemaVersion = Literal["1.0"]

ScoreAxis = Literal[
    "preparation",
    "execution",
    "scientific_answer",
    "evidence_communication",
]

TaskCategory = Literal[
    "engine_sanity",
    "system_preparation",
    "experimental_ground_truth",
    "publication_ready_evidence",
]

ExecutionMode = Literal["lite", "dry_run", "plan_only"]
JudgeMode = Literal["deterministic", "llm_judge"]

SubmissionStatus = Literal["completed", "partial", "failed", "blocked"]
ScoreStatus = Literal["passed", "partial", "failed"]

DeterministicCheckType = Literal[
    "required_files",
    "forbidden_files",
    "json_equals",
    "json_max",
    "json_min",
    "json_min_length",
    "json_allowed_values",
    "trajectory_rescan",
    "topology_solvent_rescan",
    "structure_component_rescan",
    "pdb_residue_state",
    "rmsd_recompute",
    "metrics_caption_consistency",
]

# Artifact-integrity checks run before deterministic checks and produce
# warnings (and optionally a reject-phase clamp). They look at the bytes of
# files the agent submitted, not at the JSON values inside them, so they are
# the layer that catches "manifest says completed but methods.md is the
# template stub" — a failure mode that string-equality checks miss.
IntegrityCheckType = Literal[
    "artifact_min_bytes",
    "template_markers",
    "markdown_structure",
    "evidence_completeness",
    "citation_pool",
    "figures_are_png",
    "status_artifact_floor",
    "manifest_artifact_floor",
]

IntegrityPolicy = Literal["warn", "reject"]


SCORE_AXES: tuple[ScoreAxis, ...] = (
    "preparation",
    "execution",
    "scientific_answer",
    "evidence_communication",
)


# ---------------------------------------------------------------------------
# Task contract


class FailurePolicy(BaseModel):
    blocked_by_missing_input_allowed: bool = False
    insufficient_information_allowed: bool = False


class DeterministicCheck(BaseModel):
    """One deterministic, re-runnable check.

    Each check_type interprets the optional fields differently. The scoring
    layer dispatches on ``check_type`` and applies the relevant fields.
    """

    model_config = ConfigDict(extra="forbid")

    check_id: str
    check_type: DeterministicCheckType
    weight: float = Field(ge=0.0, le=1.0, default=1.0)

    # Common: which submission file to read
    json_file: Optional[str] = None
    json_path: Optional[str] = None

    # required_files / required_outputs lists relative paths under submission/
    required_outputs: Optional[list[str]] = None
    # forbidden_files / forbidden_outputs lists paths that must not exist.
    forbidden_outputs: Optional[list[str]] = None

    # json_equals / json_max / json_min thresholds
    equals: Any = None
    max_value: Optional[float] = None
    min_value: Optional[float] = None

    # json_min_length
    min_length: Optional[int] = None

    # json_allowed_values
    allowed_values: Optional[list[Any]] = None

    # trajectory_rescan
    trajectory_path: Optional[str] = None
    topology_path: Optional[str] = None
    trajectory_manifest_path: Optional[str] = None
    topology_manifest_path: Optional[str] = None
    require_min_frames: Optional[int] = None

    # topology_solvent_rescan
    required_solvent_type: Optional[str] = None
    water_residue_names: Optional[list[str]] = None
    min_water_residues: Optional[int] = None

    # structure_component_rescan
    min_residue_counts: Optional[dict[str, int]] = None
    max_residue_counts: Optional[dict[str, int]] = None
    exact_residue_counts: Optional[dict[str, int]] = None

    # pdb_residue_state
    structure_path: Optional[str] = None
    structure_manifest_path: Optional[str] = None
    residue_chain: Optional[str] = None
    residue_number: Optional[str] = None
    insertion_code: str = ""
    required_residue_name: Optional[str] = None
    required_atom_names: Optional[list[str]] = None
    forbidden_atom_names: Optional[list[str]] = None

    # rmsd_recompute
    reference_pdb: Optional[str] = None  # relative to task dir; scorer-only is OK
    selection: Optional[str] = None  # mdtraj selection string
    align_selection: Optional[str] = None  # superpose target
    tolerance_angstrom: float = 0.05

    # metrics_caption_consistency: numeric tolerance (relative)
    relative_tolerance: float = 0.01


class GroundTruthCheck(BaseModel):
    """Compares an agent-submitted scalar against a curator-held truth file.

    truth_file is read by the scorer from ``<task_dir>/truth/...``; the agent
    is *not* allowed to read it (skill rule + structural separation).
    """

    model_config = ConfigDict(extra="forbid")

    check_id: str
    truth_file: str = "truth/experimental_truth.json"
    truth_path: str  # JSON path inside truth_file, e.g., "expected_direction"
    submission_file: str = "evidence_report.json"
    submission_path: str  # JSON path inside submission_file
    allowed_values: Optional[list[Any]] = None
    weight: float = Field(ge=0.0, le=1.0, default=1.0)


class IntegrityCheck(BaseModel):
    """An artifact-level check that verifies the bytes on disk, not JSON values.

    Each check_type interprets the optional fields differently; the
    integrity layer dispatches on ``check_type``. Failures produce warning
    strings that the scoring layer either records as ``integrity_warnings``
    (warn policy) or uses to clamp scores to zero (reject policy).
    """

    model_config = ConfigDict(extra="forbid")

    check_id: str
    check_type: IntegrityCheckType
    weight: float = Field(ge=0.0, le=1.0, default=1.0)

    # artifact_min_bytes: relative path under submission/ and minimum byte size
    path: Optional[str] = None
    min_bytes: Optional[int] = None

    # template_markers: substrings that mark unfilled template content
    forbid_markers: Optional[list[str]] = None

    # markdown_structure: minimum H2 count and required section titles
    min_h2: Optional[int] = None
    required_sections: Optional[list[str]] = None

    # evidence_completeness: required keys under evidence_report.evidence
    required_keys: Optional[list[str]] = None

    # citation_pool: relative path (from task_dir) to the allowed pool JSON
    allowed_pool_file: Optional[str] = None
    citation_field: Optional[str] = None  # JSON path inside evidence_report.json

    # figures_are_png: min bytes per figure and which manifest field lists them
    min_figure_bytes: Optional[int] = None
    figures_manifest_path: Optional[str] = None

    # status_artifact_floor: floors enforced only when manifest.status == "completed"
    # (e.g. {"prepared_structure.pdb": 5000, "methods.md": 1024})
    status_floor: Optional[dict[str, int]] = None

    # manifest_artifact_floor: read a manifest list path such as
    # outputs.trajectories and require at least min_count existing artifacts,
    # each with min_bytes. Used when JSON metrics alone are not sufficient.
    manifest_path: Optional[str] = None
    min_count: Optional[int] = None


class TaskScoring(BaseModel):
    deterministic_checks: list[DeterministicCheck] = Field(default_factory=list)
    ground_truth_checks: list[GroundTruthCheck] = Field(default_factory=list)
    llm_judge_rubrics: list[str] = Field(default_factory=list)
    integrity_checks: list[IntegrityCheck] = Field(default_factory=list)
    # "warn" only records warnings; "reject" clamps weighted_total to 0 on any
    # integrity failure. v1.0.x ships with "warn"; later releases flip to
    # "reject" once submissions in the wild have been migrated.
    integrity_policy: IntegrityPolicy = "warn"


class TaskReference(BaseModel):
    note: Optional[str] = None
    source: Optional[str] = None
    doi: Optional[str] = None
    url: Optional[str] = None


class Task(BaseModel):
    """Task contract.

    Note the deliberate absence of any ``truth.expected_*`` fields here:
    ground truth lives in ``<task_dir>/truth/`` and is scorer-only.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: SchemaVersion = "1.0"
    task_id: str
    category: TaskCategory
    primary_score: ScoreAxis
    secondary_scores: list[ScoreAxis] = Field(default_factory=list)
    execution_mode: ExecutionMode
    time_limit_minutes: int = Field(gt=0, default=180)
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)
    required_outputs: list[str] = Field(default_factory=list)
    not_scored_here: list[ScoreAxis] = Field(default_factory=list)
    capability_tags: list[str] = Field(default_factory=list)
    environment_type: Optional[str] = None
    requires_tools: list[str] = Field(default_factory=list)
    evaluation_target: Optional[str] = None
    prep_battery_priority: Optional[int] = None
    public_source: Optional[str] = None
    scoring: TaskScoring = Field(default_factory=TaskScoring)
    task_intent: str
    references: list[TaskReference] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Submission contract (manifest.json shape)


class SubmissionOutputs(BaseModel):
    metrics: Optional[str] = "metrics.json"
    provenance: Optional[str] = "provenance.json"
    evidence_report: Optional[str] = "evidence_report.json"
    decision_log: Optional[str] = None
    methods: Optional[str] = None
    figures: list[str] = Field(default_factory=list)
    topology: list[str] = Field(default_factory=list)
    trajectories: list[str] = Field(default_factory=list)
    checkpoints: list[str] = Field(default_factory=list)
    prepared_structure: Optional[str] = None


class SubmissionError(BaseModel):
    stage: Optional[str] = None
    code: Optional[str] = None
    message: str = ""


class SubmissionManifest(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: SchemaVersion = "1.0"
    run_id: str = ""
    task_id: str
    status: SubmissionStatus = "completed"
    outputs: SubmissionOutputs = Field(default_factory=SubmissionOutputs)
    limitations: list[str] = Field(default_factory=list)
    errors: list[SubmissionError] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Score result


class CheckResult(BaseModel):
    check_id: str
    check_type: Optional[str] = None
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    weight: float = Field(ge=0.0, le=1.0, default=1.0)
    message: str = ""


class LLMJudgeResult(BaseModel):
    enabled: bool = False
    judge_model: Optional[str] = None
    temperature: float = 0.0
    rubric_version: str = "1.0"
    prompt_hash: Optional[str] = None
    raw_response_file: Optional[str] = None
    scores: dict[str, float] = Field(default_factory=dict)
    violations: list[dict] = Field(default_factory=list)


class RuntimeRecord(BaseModel):
    walltime_minutes: float = 0.0
    tokens: int = 0
    gpu_hours: float = 0.0


class Score(BaseModel):
    schema_version: SchemaVersion = "1.0"
    run_id: str = ""
    task_id: str
    primary_score: ScoreAxis
    status: ScoreStatus
    weighted_total: float = Field(ge=0.0, le=1.0)
    scores: dict[str, Optional[float]] = Field(default_factory=dict)
    deterministic_checks: list[CheckResult] = Field(default_factory=list)
    ground_truth_checks: list[CheckResult] = Field(default_factory=list)
    llm_judge: LLMJudgeResult = Field(default_factory=LLMJudgeResult)
    runtime: RuntimeRecord = Field(default_factory=RuntimeRecord)
    integrity_warnings: list[str] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Run-level config


class BackendInfo(BaseModel):
    """MD engine or workflow used by the agent under test.

    Examples: ``mdclaw``, ``openmm-script``, ``gromacs``, ``amber``,
    ``synthetic``. This is separate from the scorer runtime.
    """

    name: str = "unknown"
    version: str = ""
    container: str = ""


class HarnessInfo(BaseModel):
    """Agent runner or orchestration harness.

    Examples: ``cursor``, ``claude-code``, ``opencode``,
    ``external-python-script``, or a lab-specific runner.
    """

    name: str = "unknown"
    version: str = ""
    adapter: str = ""


class ModelInfo(BaseModel):
    """LLM or non-LLM agent model used by the harness, when applicable."""

    name: str = "unknown"
    provider: str = "unknown"
    version: str = ""


class BudgetSpec(BaseModel):
    max_walltime_minutes_per_task: int = 180
    max_gpu_hours: float = 0.0
    max_tokens_per_task: int = 0
    max_simulation_ns: float = 0.0


class RunConfig(BaseModel):
    schema_version: SchemaVersion = "1.0"
    benchmark_version: str = "MDAgentBench-prep-v0.1"
    run_id: str
    created_at: str
    execution_mode: ExecutionMode = "lite"
    judge_mode: JudgeMode = "deterministic"
    backend: BackendInfo = Field(default_factory=BackendInfo)
    harness: HarnessInfo = Field(default_factory=HarnessInfo)
    model: ModelInfo = Field(default_factory=ModelInfo)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    task_ids: list[str] = Field(default_factory=list)


class RunSummary(BaseModel):
    schema_version: SchemaVersion = "1.0"
    benchmark_version: str = "MDAgentBench-prep-v0.1"
    run_id: str
    created_at: str
    execution_mode: ExecutionMode = "lite"
    judge_mode: JudgeMode = "deterministic"
    backend: BackendInfo = Field(default_factory=BackendInfo)
    harness: HarnessInfo = Field(default_factory=HarnessInfo)
    model: ModelInfo = Field(default_factory=ModelInfo)
    n_tasks: int = 0
    n_failed_tasks: int = 0
    overall_score: float = 0.0
    scores: dict[str, Optional[float]] = Field(default_factory=dict)
    task_scores: list[dict] = Field(default_factory=list)
    runtime: dict = Field(default_factory=dict)
