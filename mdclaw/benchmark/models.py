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
    "json_equals",
    "json_max",
    "json_min",
    "json_min_length",
    "json_allowed_values",
    "trajectory_rescan",
    "rmsd_recompute",
    "metrics_caption_consistency",
]


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


class TaskInputs(BaseModel):
    """Curator-supplied inputs. Empty lists mean the task does not require
    that kind of artifact (NOT 'agent picks one'). v1.0 every task ships
    a fully-specified set."""

    structures: list[str] = Field(default_factory=list)
    ligands: list[str] = Field(default_factory=list)
    trajectories: list[str] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)


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
    require_min_frames: Optional[int] = None

    # rmsd_recompute
    reference_pdb: Optional[str] = None  # relative to task input dir
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


class TaskScoring(BaseModel):
    deterministic_checks: list[DeterministicCheck] = Field(default_factory=list)
    ground_truth_checks: list[GroundTruthCheck] = Field(default_factory=list)
    llm_judge_rubrics: list[str] = Field(default_factory=list)


class TaskReference(BaseModel):
    note: Optional[str] = None
    source: Optional[str] = None
    doi: Optional[str] = None
    url: Optional[str] = None


class Task(BaseModel):
    """v1.0 task contract.

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
    inputs: TaskInputs = Field(default_factory=TaskInputs)
    required_outputs: list[str] = Field(default_factory=list)
    not_scored_here: list[ScoreAxis] = Field(default_factory=list)
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
    name: str = "mdclaw"
    version: str = ""
    container: str = ""


class HarnessInfo(BaseModel):
    name: str = "unknown"
    version: str = ""
    adapter: str = ""


class ModelInfo(BaseModel):
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
    benchmark_version: str = "MDAgentBench-v1.0"
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
    benchmark_version: str = "MDAgentBench-v1.0"
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


