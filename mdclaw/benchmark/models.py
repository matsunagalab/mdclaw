"""Pydantic v2 models for the MD benchmark suites.

These types are the single source of truth for task / submission / score
shapes. JSON files on disk are parsed through these models; this gives us
real schema validation (vs. the v0.1 hand-coded validator) and fail-fast
behavior on malformed input.

The schema_version is locked to ``"1.0"`` for this release. A future v1.1 with
backward-compatible extensions can add Literal["1.0", "1.1"] without breaking
existing callers.
"""

from __future__ import annotations

import math
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from mdclaw.benchmark.datasets import DEFAULT_BENCHMARK_VERSION


if not hasattr(BaseModel, "model_validate"):
    from pydantic import ValidationError
    from pydantic.error_wrappers import ErrorWrapper

    def _v1_model_config(cls) -> dict[str, Any]:
        field = getattr(cls, "__fields__", {}).get("model_config")
        default = getattr(field, "default", None)
        return default if isinstance(default, dict) else {}

    def _v1_strip_model_config_field(cls, obj: Any) -> Any:
        if isinstance(obj, dict) and "model_config" in getattr(cls, "__fields__", {}):
            obj = dict(obj)
            obj.pop("model_config", None)
        return obj

    def _v1_forbidden_extras(cls, obj: Any) -> list[str]:
        if not isinstance(obj, dict):
            return []
        if _v1_model_config(cls).get("extra") != "forbid":
            return []
        allowed = set(getattr(cls, "__fields__", {})) - {"model_config"}
        return sorted(set(obj) - allowed)

    def _model_validate(cls, obj: Any, *args: Any, **kwargs: Any) -> BaseModel:
        del args, kwargs
        extras = _v1_forbidden_extras(cls, obj)
        if extras:
            errors = [
                ErrorWrapper(ValueError("extra fields not permitted"), loc=field)
                for field in extras
            ]
            raise ValidationError(errors, cls)
        obj = _v1_strip_model_config_field(cls, obj)
        try:
            return cls.parse_obj(obj)
        except ValidationError:
            raise
        except ValueError as exc:
            # v2 semantic hooks raise ValueError from __init__.  Pydantic v2
            # wraps those as ValidationError; preserve the same public
            # contract for the repository's supported Pydantic-v1 runtime.
            raise ValidationError([ErrorWrapper(exc, loc="__root__")], cls) from exc

    def _model_validate_json(cls, json_data: str | bytes,
                             *args: Any, **kwargs: Any) -> BaseModel:
        del args, kwargs
        import json as _json

        return cls.model_validate(_json.loads(json_data))

    def _model_dump(self: BaseModel, *args: Any, **kwargs: Any) -> dict[str, Any]:
        exclude = kwargs.pop("exclude", None)
        if "model_config" in getattr(type(self), "__fields__", {}):
            if exclude is None:
                exclude = {"model_config"}
            elif isinstance(exclude, set):
                exclude = {*exclude, "model_config"}
            elif isinstance(exclude, dict):
                exclude = {**exclude, "model_config": True}
        return self.dict(*args, exclude=exclude, **kwargs)

    def _model_dump_json(self: BaseModel, *args: Any, **kwargs: Any) -> str:
        exclude = kwargs.pop("exclude", None)
        if "model_config" in getattr(type(self), "__fields__", {}):
            if exclude is None:
                exclude = {"model_config"}
            elif isinstance(exclude, set):
                exclude = {*exclude, "model_config"}
            elif isinstance(exclude, dict):
                exclude = {**exclude, "model_config": True}
        return self.json(*args, exclude=exclude, **kwargs)

    def _model_copy(
        self: BaseModel,
        *,
        update: Optional[dict[str, Any]] = None,
        deep: bool = False,
    ) -> BaseModel:
        return self.copy(update=update, deep=deep)

    def _model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return cls.schema(*args, **kwargs)

    BaseModel.model_validate = classmethod(_model_validate)  # type: ignore[attr-defined]
    BaseModel.model_validate_json = classmethod(_model_validate_json)  # type: ignore[attr-defined]
    BaseModel.model_dump = _model_dump  # type: ignore[attr-defined]
    BaseModel.model_dump_json = _model_dump_json  # type: ignore[attr-defined]
    BaseModel.model_copy = _model_copy  # type: ignore[attr-defined]
    BaseModel.model_json_schema = classmethod(_model_json_schema)  # type: ignore[attr-defined]

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
EvaluationProtocol = Literal["grounded_correct_v1", "grounded_correct_v2"]

# How much MDClaw tooling the *solver* used. The shared MDClaw scorer judges
# every entrant regardless of condition; this only describes the solve side.
# - mdclaw-skills+cli: full MDClaw (skills + CLI tools).
# - mdclaw-cli-only:   MDClaw CLI tools, no skills.
# - mdclaw-free:       the solver imports/calls no MDClaw at all.
ToolingCondition = Literal[
    "mdclaw-skills+cli",
    "mdclaw-cli-only",
    "mdclaw-free",
    "unknown",
]

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
    "paired_mutation_topology",
    "topology_solvent_rescan",
    "structure_component_rescan",
    "topology_component_rescan",
    "unexpected_residue_rescan",
    "disulfide_bond_rescan",
    "nucleic_content_rescan",
    "residue_ratio_rescan",
    "solvent_regime_rescan",
    "pdb_no_deuterium_atoms",
    "pdb_residue_state",
    "rmsd_recompute",
    "assembly_identity_check",
    "candidate_selection_check",
    "artifact_provenance_text",
    "topology_artifact_bundle",
    "openmm_system_load",
    "openmm_energy_rescan",
    "forcefield_applied_rescan",
    "net_charge_check",
    "water_model_fingerprint",
    "ion_concentration_recompute",
    "minimization_report_check",
    "minimized_structure_component_rescan",
    "structure_geometry_quality",
    "metrics_caption_consistency",
    "direction_grounding",
    "observable_recompute_consistency",
    "v2_entity_condition_comparison",
]

# Capability axis a deterministic check contributes to. The scorer groups
# per-check results by capability to produce a capability profile, and the
# physical-validity subset acts as a hard gate (see scoring._HARD_FAIL_CHECK_TYPES).
CheckCapability = Literal[
    "identity",
    "physical_validity",
    "fidelity",
    "provenance",
]

# Default capability for each deterministic check_type when a task does not set
# one explicitly. "identity" = right components built; "physical_validity" =
# the system is a sane MD system; "fidelity" = honored an explicit request;
# "provenance" = self-reported rationale/trace.
DEFAULT_CHECK_CAPABILITY: dict[str, str] = {
    "required_files": "provenance",
    "forbidden_files": "identity",
    "json_equals": "fidelity",
    "json_max": "fidelity",
    "json_min": "fidelity",
    "json_min_length": "provenance",
    "json_allowed_values": "fidelity",
    "trajectory_rescan": "physical_validity",
    "paired_mutation_topology": "physical_validity",
    "topology_solvent_rescan": "identity",
    "structure_component_rescan": "identity",
    "topology_component_rescan": "physical_validity",
    "unexpected_residue_rescan": "identity",
    "disulfide_bond_rescan": "identity",
    "nucleic_content_rescan": "identity",
    "residue_ratio_rescan": "fidelity",
    "solvent_regime_rescan": "fidelity",
    "pdb_no_deuterium_atoms": "identity",
    "pdb_residue_state": "identity",
    "rmsd_recompute": "fidelity",
    "assembly_identity_check": "identity",
    "candidate_selection_check": "fidelity",
    "artifact_provenance_text": "provenance",
    "topology_artifact_bundle": "physical_validity",
    "openmm_system_load": "physical_validity",
    "openmm_energy_rescan": "physical_validity",
    "forcefield_applied_rescan": "physical_validity",
    "net_charge_check": "physical_validity",
    "water_model_fingerprint": "fidelity",
    "ion_concentration_recompute": "fidelity",
    "minimization_report_check": "physical_validity",
    "minimized_structure_component_rescan": "identity",
    "structure_geometry_quality": "physical_validity",
    "metrics_caption_consistency": "provenance",
    "direction_grounding": "fidelity",
    "observable_recompute_consistency": "fidelity",
    "v2_entity_condition_comparison": "fidelity",
    "minimized_structure_required": "physical_validity",
}

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
    "trajectory_file_signature",
    "submission_artifact_hashes",
    "openmm_minimized_state_consistency",
    "provenance_execution_evidence",
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

    # Capability axis this check contributes to in the capability profile.
    # When None, the scorer falls back to DEFAULT_CHECK_CAPABILITY[check_type].
    capability: Optional[CheckCapability] = None

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

    # paired_mutation_topology: load the submitted wild-type topology
    # (topology_manifest_path, default outputs.topology.0) and mutant topology
    # (mutant_topology_manifest_path, default outputs.topology.1) and require the
    # residue-name multisets to differ by exactly one
    # wild_type_residue_name -> required_residue_name substitution. Chain-agnostic
    # so it does not depend on how the solver labeled chains.
    mutant_topology_manifest_path: Optional[str] = None
    wild_type_residue_name: Optional[str] = None

    # topology / minimization prep checks
    required_topology_backend: Optional[str] = None
    topology_backend_json_file: Optional[str] = None
    topology_backend_json_path: Optional[str] = None
    required_topology_artifacts: Optional[list[str]] = None
    min_topology_artifact_count: Optional[int] = None
    system_xml_manifest_path: Optional[str] = None
    topology_pdb_manifest_path: Optional[str] = None
    state_xml_manifest_path: Optional[str] = None
    minimization_report_path: Optional[str] = None
    minimization_report_manifest_path: Optional[str] = None
    minimized_structure_manifest_path: Optional[str] = None
    max_rescan_minimization_steps: Optional[int] = None

    # topology_solvent_rescan
    required_solvent_type: Optional[str] = None
    water_residue_names: Optional[list[str]] = None
    min_water_residues: Optional[int] = None

    # structure_component_rescan
    min_residue_counts: Optional[dict[str, int]] = None
    max_residue_counts: Optional[dict[str, int]] = None
    exact_residue_counts: Optional[dict[str, int]] = None
    residue_aliases: Optional[dict[str, list[str]]] = None
    # unexpected_residue_rescan: allowlist non-standard residues expected by
    # the task and reject unrelated HETATM-style components in the artifact.
    allowed_nonstandard_residue_names: Optional[list[str]] = None
    ignored_residue_names: Optional[list[str]] = None
    allow_standard_residues: bool = True
    allow_water_residues: bool = True
    allow_ion_residues: bool = True
    # Ignore residues with fewer than this many atoms when counting components.
    # Lets lipid checks reject small residues (water/ions) whose names can
    # collide with truncated lipid aliases.
    min_residue_atom_count: Optional[int] = None

    # residue_ratio_rescan
    required_residue_ratio: Optional[dict[str, int]] = None

    # disulfide_bond_rescan
    min_disulfide_count: Optional[int] = None
    disulfide_distance_cutoff_angstrom: float = 2.4

    # nucleic_content_rescan
    required_nucleic_acid_type: Optional[str] = None
    min_nucleic_residue_count: Optional[int] = None
    min_nucleic_chain_count: Optional[int] = None
    exact_nucleic_chain_count: Optional[int] = None

    # solvent_regime_rescan
    required_solvent_regime: Optional[str] = None
    max_water_residues: Optional[int] = None
    lipid_residue_names: Optional[list[str]] = None
    min_lipid_residues: Optional[int] = None

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
    image_molecules_before_rmsd: bool = False

    # assembly_identity_check
    assembly_id_json_file: Optional[str] = None
    assembly_id_json_path: Optional[str] = None
    required_assembly_id: Optional[str] = None
    chain_identity_json_file: Optional[str] = None
    chain_identity_json_path: Optional[str] = None
    min_chain_count: Optional[int] = None
    exact_chain_count: Optional[int] = None
    min_mapping_entries: Optional[int] = None
    min_distinct_output_chains: Optional[int] = None
    required_mapping_fields: Optional[list[str]] = None
    required_operator_ids: Optional[list[str]] = None
    require_output_chains_in_structure: bool = False
    require_unique_output_chains: bool = False
    # When the chain identity map tags entries with ``molecule_type``, count only
    # polymer chains (protein/peptide/nucleic/dna/rna) toward chain-count checks
    # so that cofactor/ligand chains carrying their own IDs are not penalized.
    # Falls back to counting every mapped chain when no entry is tagged.
    count_polymer_chains_only: bool = True

    # candidate_selection_check
    source_selection_manifest_path: Optional[str] = None
    source_selection_path: Optional[str] = None
    required_candidate_id: Optional[str] = None
    required_model_rank: Optional[int] = None
    require_selection_reason: bool = False

    # artifact_provenance_text
    text_files: Optional[list[str]] = None
    required_text_groups: Optional[list[list[str]]] = None

    # metrics_caption_consistency: numeric tolerance (relative)
    relative_tolerance: float = 0.01

    # ----- artifact-as-truth recompute checks (v1.1) -----
    # These re-derive physical properties from the OpenMM artifact triple and
    # treat metrics.json as a declaration to cross-check, not as truth.

    # forcefield_applied_rescan: require every particle to carry NonbondedForce
    # parameters (charge/sigma/epsilon) and the system to have >= this many forces.
    min_force_count: Optional[int] = None

    # net_charge_check: sum NonbondedForce particle charges and assert the total
    # is near-integer; if require_neutral, assert near-zero. Optional declared
    # cross-check via charge_json_file/charge_json_path.
    require_neutral: bool = False
    target_net_charge: Optional[float] = None
    charge_tolerance: float = 0.05
    charge_json_file: Optional[str] = None
    charge_json_path: Optional[str] = None

    # water_model_fingerprint: classify water by particles-per-water and
    # virtual-site presence (3-site vs 4/5-site), then cross-check against the
    # requested family. sites_per_water is the requested particles-per-water.
    required_water_model: Optional[str] = None
    sites_per_water: Optional[int] = None

    # ion_concentration_recompute: count cation/anion residues in the topology
    # and read box vectors from state.xml to compute molarity, comparing to the
    # requested value within molar_tolerance and (optionally) asserting neutrality.
    cation_residue_names: Optional[list[str]] = None
    anion_residue_names: Optional[list[str]] = None
    target_molar: Optional[float] = None
    molar_tolerance: float = 0.05
    min_ion_count: Optional[int] = None

    # structure_geometry_quality: recompute geometric sanity from the OpenMM
    # bundle (system.xml sigma + bonds/exceptions and state.xml coordinates).
    # A well-minimized system passes; severe clashes/outliers indicate a bad
    # starting structure that a finite-energy check alone would not catch.
    # - clash_overlap_fraction: two non-bonded atoms clash when their center
    #   distance is below this fraction of (sigma_i + sigma_j)/2 * 2^(1/6).
    # - max_clashes / max_bond_length_outliers / max_angle_outliers /
    #   max_cis_nonproline: tolerated counts before the check fails.
    # - bond_length_tolerance_fraction / angle_tolerance_degrees: outlier
    #   thresholds relative to the ideal geometry from the force field.
    # - check_chirality: flag D-amino-acid CA centers (inverted chirality).
    clash_overlap_fraction: float = 0.6
    max_clashes: int = 0
    max_bond_length_outliers: Optional[int] = None
    bond_length_tolerance_fraction: float = 0.35
    max_angle_outliers: Optional[int] = None
    angle_tolerance_degrees: float = 35.0
    max_cis_nonproline: Optional[int] = None
    check_chirality: bool = False

    # Multiple-accepted-answer variants for pdb_residue_state (deterministic,
    # no LLM judge). When set, the residue passes if it matches ANY of the
    # allowed residue names and (optionally) ANY of the accepted atom-name sets.
    # This lets judgment-type tasks (protonation/tautomer/capping) accept more
    # than one valid preparation without falling back to a subjective judge.
    allowed_residue_names: Optional[list[str]] = None
    accepted_atom_name_sets: Optional[list[list[str]]] = None

    # Per-check hard-fail override. When True, a failing check clamps the
    # completed submission to 0 like the built-in physical-validity gate, even
    # if its check_type is not in scoring._HARD_FAIL_CHECK_TYPES. Lets a task
    # promote a task-specific check (e.g. a required protonation state) to a
    # gate without making that check_type a gate for every task.
    hard_fail: bool = False

    # ----- study-level scientific-answer checks (MDStudyBench) -----
    # These recompute a single per-task "discriminating observable" from the
    # submitted comparative trajectories (outputs.trajectories[0/1] loaded
    # against outputs.topology[0/1]) and use it to (a) reward a conclusion that
    # is grounded in the agent's OWN data (direction_grounding) and (b) verify
    # that the numbers the agent reports are the real recomputed values
    # (observable_recompute_consistency). Neither reads the hidden truth: the
    # literature direction is scored separately by a GroundTruthCheck.
    #
    # observable_metric selects how the observable is computed from each system:
    #   - "ca_rmsf": mean C-alpha RMSF over ``observable_selection``.
    #   - "contact_count": mean number of heavy-atom contacts within
    #     ``contact_cutoff_nm`` between ``observable_selection`` and
    #     ``observable_selection_b`` (interface / ligand-cavity contacts).
    observable_metric: Optional[Literal["ca_rmsf", "contact_count"]] = None
    observable_selection: Optional[str] = None  # mdtraj selection (group A)
    observable_selection_b: Optional[str] = None  # mdtraj selection (group B)
    contact_cutoff_nm: float = 0.45
    # sign_to_direction maps the sign of (mutant - reference) for the observable
    # onto an effect.direction value, e.g.
    # {"increase": "destabilizing", "decrease": "stabilizing"}.
    sign_to_direction: Optional[dict[str, str]] = None
    # direction_grounding: separation (|delta| / sigma) below this is treated as
    # inconclusive and scored at the neutral partial credit below, so an honest
    # "the data are inconclusive" report is not penalized.
    inconclusive_sigma: float = 1.0
    inconclusive_score: float = 0.5
    # Which submission field holds the agent's claimed direction.
    direction_field: str = "effect.direction"
    # observable_recompute_consistency: name of the entry in
    # ``evidence_report.observables`` whose wt_value/mutant_value are compared to
    # the recomputed values; agreement within tolerance_fraction scores 1.0 and
    # degrades linearly with relative error.
    report_observable_name: Optional[str] = None
    tolerance_fraction: float = 0.5
    # paired_mutation_topology: optional biological residue number for named
    # mutations, plus optional heavy-atom counts for a name-agnostic molecular
    # swap (for example, two ligands whose residue codes are agent-selected).
    mutation_resseq: Optional[int] = None
    reference_changed_residue_heavy_atom_count: Optional[int] = Field(
        default=None,
        ge=1,
    )
    variant_changed_residue_heavy_atom_count: Optional[int] = Field(
        default=None,
        ge=1,
    )
    reference_changed_residue_element_counts: Optional[dict[str, int]] = None
    variant_changed_residue_element_counts: Optional[dict[str, int]] = None

    # v2_entity_condition_comparison: resolve a generalized StudyIndexV2
    # comparison and require matched conditions except for the named
    # perturbation dimensions.
    comparison_id: Optional[str] = None
    matched_except: Optional[list[str]] = None


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

    # submission_artifact_hashes: require provenance.raw_outputs md5 entries
    # for manifest.json and every manifest-declared artifact except
    # provenance.json, which cannot self-hash.
    require_manifest_output_hashes: bool = True

    # openmm_minimized_state_consistency: require minimized_structure.pdb to be
    # a coordinate view of outputs.topology's state.xml.
    coordinate_tolerance_nm: Optional[float] = None

    # provenance_execution_evidence: require structured command/action records
    # proving that a completed submission attempted the relevant workflow stages.
    required_stages: Optional[list[str]] = None
    min_command_count: Optional[int] = None
    # When true, provenance.command_log remains useful agent-side trace text,
    # but the integrity check also requires a scorer-side harness execution
    # record outside submission/ so solvers cannot pass by hand-editing
    # provenance.json after the fact.
    require_harness_record: bool = False
    harness_record_path: Optional[str] = None


class TaskScoring(BaseModel):
    deterministic_checks: list[DeterministicCheck] = Field(default_factory=list)
    ground_truth_checks: list[GroundTruthCheck] = Field(default_factory=list)
    llm_judge_rubrics: list[str] = Field(default_factory=list)
    integrity_checks: list[IntegrityCheck] = Field(default_factory=list)
    # "warn" only records warnings; "reject" clamps weighted_total to 0 on any
    # artifact/provenance integrity failure.
    integrity_policy: IntegrityPolicy = "warn"


class TaskReference(BaseModel):
    note: Optional[str] = None
    source: Optional[str] = None
    doi: Optional[str] = None
    url: Optional[str] = None


class _GroundedV2Model(BaseModel):
    """Base for v2 models with Pydantic v1/v2 semantic checks.

    The repository still carries a small Pydantic-v1 compatibility shim.  A
    post-init hook keeps the new cross-field invariants active under both
    Pydantic generations without changing any of the frozen v1 models.
    """

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self._validate_grounded_v2_semantics()

    def _validate_grounded_v2_semantics(self) -> None:
        """Validate invariants that cannot be expressed by individual fields."""


def _require_nonempty_unique(values: list[str], label: str) -> None:
    normalized = [value.strip() for value in values]
    if any(not value for value in normalized):
        raise ValueError(f"{label} must contain non-empty strings")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} must not contain duplicates")


DecisionRuleKindV2 = Literal["directional_ci", "equivalence_ci", "custom"]


class AnalysisDecisionRule(_GroundedV2Model):
    """A preregistered rule for turning one observable into a verdict."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    kind: DecisionRuleKindV2
    confidence_level: float = Field(default=0.95, gt=0.0, lt=1.0)
    equivalence_margin: Optional[float | dict[str, float]] = None
    unit: Optional[str] = None
    parameters: dict[str, Any] = Field(default_factory=dict)

    def _validate_grounded_v2_semantics(self) -> None:
        margin = self.equivalence_margin
        if self.kind == "equivalence_ci" and margin is None:
            raise ValueError(
                "equivalence_ci decision rules require equivalence_margin"
            )
        if margin is None:
            return
        values = list(margin.values()) if isinstance(margin, dict) else [margin]
        if not values or any(
            isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) <= 0.0
            for value in values
        ):
            raise ValueError("equivalence_margin values must be finite and positive")


class PrimaryEvidenceContractV2(_GroundedV2Model):
    """Task-owned rule for converting one recomputed estimand into an outcome.

    The agent still chooses structures and how to allocate sampling above any
    published adequacy floor. The benchmark owns the observable semantics,
    direction mapping, and statistical boundary so a solver cannot obtain a
    preferred answer by redefining the measurement after seeing data.
    """

    model_config = ConfigDict(extra="forbid")

    verifier_id: str = Field(min_length=1)
    outcome_mapping: dict[str, str]
    decision_rule: AnalysisDecisionRule
    fixed_observable_parameters: dict[str, Any] = Field(default_factory=dict)

    def _validate_grounded_v2_semantics(self) -> None:
        if not self.verifier_id.strip():
            raise ValueError("primary_evidence_contract.verifier_id must be non-empty")
        required_keys = {"increase", "decrease", "equivalent", "unresolved"}
        if set(self.outcome_mapping) != required_keys:
            raise ValueError(
                "primary_evidence_contract.outcome_mapping must contain exactly "
                f"{sorted(required_keys)}"
            )
        if any(
            not isinstance(value, str) or not value.strip()
            for value in self.outcome_mapping.values()
        ):
            raise ValueError(
                "primary_evidence_contract.outcome_mapping values must be "
                "non-empty strings"
            )


class ControlEvidenceContractV2(_GroundedV2Model):
    """Task-owned semantics for one required validity control."""

    model_config = ConfigDict(extra="forbid")

    verifier_id: str = Field(min_length=1)
    outcome_mapping: dict[str, str]
    decision_rule: AnalysisDecisionRule
    fixed_observable_parameters: dict[str, Any] = Field(default_factory=dict)

    def _validate_grounded_v2_semantics(self) -> None:
        if not self.verifier_id.strip():
            raise ValueError("control evidence verifier_id must be non-empty")
        if set(self.outcome_mapping) != {"pass", "fail"}:
            raise ValueError(
                "control evidence outcome_mapping must contain exactly "
                "['fail', 'pass']"
            )
        if any(
            not isinstance(value, str) or not value.strip()
            for value in self.outcome_mapping.values()
        ):
            raise ValueError(
                "control evidence outcome_mapping values must be non-empty"
            )


class ScientificTarget(_GroundedV2Model):
    """Public scientific meaning for a grounded-correct-v2 task.

    This specifies the estimand, conditions, and minimal common evidence
    semantics, not a canonical structure or full simulation protocol.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    estimand: str = Field(min_length=1)
    claim_type: str = Field(min_length=1)
    allowed_outcomes: list[str]
    unresolved_outcome: str = Field(min_length=1)
    neutral_requires_equivalence: bool = True
    neutral_outcome: Optional[str] = None
    required_control_verifiers: list[str] = Field(default_factory=list)
    primary_evidence_contract: Optional[PrimaryEvidenceContractV2] = None
    control_evidence_contracts: list[ControlEvidenceContractV2] = Field(
        default_factory=list
    )
    execution_adapter: Optional[str] = None
    required_conditions: dict[str, Any] = Field(default_factory=dict)
    entity: dict[str, Any] = Field(default_factory=dict)

    def _validate_grounded_v2_semantics(self) -> None:
        _require_nonempty_unique(
            self.allowed_outcomes,
            "scientific_target.allowed_outcomes",
        )
        if len(self.allowed_outcomes) < 2:
            raise ValueError("scientific_target requires at least two outcomes")
        if not self.claim_type.strip():
            raise ValueError("scientific_target.claim_type must be non-empty")
        unresolved = self.unresolved_outcome.strip()
        if not unresolved:
            raise ValueError("scientific_target.unresolved_outcome must be non-empty")
        if unresolved in self.allowed_outcomes:
            raise ValueError(
                "unresolved_outcome must be separate from allowed_outcomes"
            )
        if self.neutral_requires_equivalence:
            if (
                not self.neutral_outcome
                or self.neutral_outcome not in self.allowed_outcomes
            ):
                raise ValueError(
                    "neutral_requires_equivalence requires neutral_outcome in "
                    "allowed_outcomes"
                )
        elif self.neutral_outcome is not None and (
            self.neutral_outcome not in self.allowed_outcomes
        ):
            raise ValueError("neutral_outcome must be in allowed_outcomes")
        _require_nonempty_unique(
            self.required_control_verifiers,
            "scientific_target.required_control_verifiers",
        )
        control_verifiers = [
            item.verifier_id for item in self.control_evidence_contracts
        ]
        _require_nonempty_unique(
            control_verifiers,
            "scientific_target.control_evidence_contracts.verifier_id",
        )
        unknown_control_contracts = (
            set(control_verifiers) - set(self.required_control_verifiers)
        )
        if unknown_control_contracts:
            raise ValueError(
                "control_evidence_contracts must refer only to "
                "required_control_verifiers"
            )
        missing_control_contracts = (
            set(self.required_control_verifiers) - set(control_verifiers)
        )
        if missing_control_contracts:
            raise ValueError(
                "every required_control_verifier requires a task-owned "
                "control_evidence_contract"
            )
        contract = self.primary_evidence_contract
        if contract is not None:
            resolved_mapping = {
                value
                for key, value in contract.outcome_mapping.items()
                if key != "unresolved"
            }
            if resolved_mapping != set(self.allowed_outcomes):
                raise ValueError(
                    "primary_evidence_contract must map exactly the public "
                    "allowed_outcomes"
                )
            if (
                contract.outcome_mapping["unresolved"]
                != self.unresolved_outcome
            ):
                raise ValueError(
                    "primary_evidence_contract unresolved mapping must match "
                    "scientific_target.unresolved_outcome"
                )
            if self.neutral_requires_equivalence and (
                contract.outcome_mapping["equivalent"] != self.neutral_outcome
                or contract.decision_rule.kind != "equivalence_ci"
            ):
                raise ValueError(
                    "the neutral outcome requires the task-owned equivalent "
                    "mapping and an equivalence_ci decision rule"
                )
        if self.execution_adapter is not None and not self.execution_adapter.strip():
            raise ValueError("scientific_target.execution_adapter must be non-empty")


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
    # Optional so v0.2 task files keep their existing flat-output and weighted
    # scoring contracts.  ``grounded_correct_v1`` opts a Study task into the
    # role-based paired-study submission contract.
    evaluation_protocol: Optional[EvaluationProtocol] = None
    # v2 makes the public estimand explicit without prescribing a canonical
    # structure or analysis plan. It remains optional so all v1 task files keep
    # validating unchanged.
    scientific_target: Optional[ScientificTarget] = None
    public_source: Optional[str] = None
    scoring: TaskScoring = Field(default_factory=TaskScoring)
    task_intent: str
    references: list[TaskReference] = Field(default_factory=list)

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        if self.evaluation_protocol != "grounded_correct_v2":
            return
        if self.scientific_target is None:
            raise ValueError(
                "grounded_correct_v2 requires scientific_target"
            )
        if self.scientific_target.primary_evidence_contract is None:
            raise ValueError(
                "grounded_correct_v2 requires a task-owned "
                "primary_evidence_contract"
            )
        if not self.scientific_target.execution_adapter:
            raise ValueError(
                "grounded_correct_v2 requires a certified execution_adapter"
            )


# ---------------------------------------------------------------------------
# Submission contract (manifest.json shape)


class PairedStudySource(BaseModel):
    """Agent-selected structural source for one side of a paired study."""

    model_config = ConfigDict(extra="allow")

    type: str
    id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PairedStudyReplica(BaseModel):
    """One independently declared MD run.

    A run may use one trajectory file or a sequence of trajectory segments.
    Cross-field validation (at least one of the two) is performed by the
    submission validator so this model remains usable with Pydantic v1 and v2.
    """

    model_config = ConfigDict(extra="allow")

    replica_id: str
    topology: str
    trajectory: Optional[str] = None
    trajectory_segments: list[str] = Field(default_factory=list)


class PairedStudySystem(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["reference", "variant"]
    source: PairedStudySource
    replicas: list[PairedStudyReplica]


class PairedStudyIndex(BaseModel):
    """Role-based index of every raw run submitted for a two-system study."""

    model_config = ConfigDict(extra="allow")

    schema_version: SchemaVersion = "1.0"
    task_id: str
    conditions: dict[str, Any] = Field(default_factory=dict)
    systems: list[PairedStudySystem]


class StudyEvidenceConclusion(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    direction: str = Field(min_length=1)
    evidence_status: Literal["supported", "inconclusive", "contradicted"]
    confidence: float = Field(ge=0.0, le=1.0)


class StudyEvidenceItem(BaseModel):
    """One agent-selected observable connecting raw runs to its conclusion."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    id: Optional[str] = None
    metric: str = Field(min_length=1)
    selection: str = Field(min_length=1)
    reference: float
    variant: float
    uncertainty: float | dict[str, float]
    unit: Optional[str] = None
    selection_b: Optional[str] = None
    contact_cutoff_nm: Optional[float] = Field(default=None, gt=0.0)


class StudyEvidenceReport(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: SchemaVersion = "1.0"
    task_id: str
    conclusion: StudyEvidenceConclusion
    evidence: list[StudyEvidenceItem]
    reasoning: str = Field(min_length=1)
    limitations: list[str]


AnalysisIntentSchemaVersion = Literal["1.0"]
GroundedV2SchemaVersion = Literal["2.0"]
StudyRunPhaseV2 = Literal["exploratory", "confirmatory"]
EvidenceClaimRoleV2 = Literal[
    "direct_estimator",
    "validated_proxy",
    "mechanistic_only",
    "validity_control",
]
AnalysisRoleV2 = Literal["estimand", "validity_control"]
MDVerdictStatusV2 = Literal["resolved", "unresolved"]
MDVerdictBasisV2 = Literal[
    "direct_estimator",
    "validated_proxy",
    "mechanistic_only",
    "insufficient",
]


class PrimaryAnalysis(_GroundedV2Model):
    """One preregistered, agent-selected analysis and estimand mapping."""

    model_config = ConfigDict(extra="allow")

    analysis_id: str = Field(min_length=1)
    analysis_role: AnalysisRoleV2 = "estimand"
    comparison_id: str = Field(min_length=1)
    observable: dict[str, Any]
    outcome_mapping: dict[str, str]
    decision_rule: AnalysisDecisionRule
    estimand_link: str = Field(min_length=1)
    alternative_explanations: list[str] = Field(default_factory=list)
    verifier_id: Optional[str] = None

    def _validate_grounded_v2_semantics(self) -> None:
        if not self.analysis_id.strip():
            raise ValueError("analysis_id must be non-empty")
        if not self.comparison_id.strip():
            raise ValueError("comparison_id must be non-empty")
        if not self.observable:
            raise ValueError("observable must not be empty")
        if not self.outcome_mapping or any(
            not str(key).strip() or not str(value).strip()
            for key, value in self.outcome_mapping.items()
        ):
            raise ValueError(
                "outcome_mapping must contain non-empty keys and outcomes"
            )
        if not self.estimand_link.strip():
            raise ValueError("estimand_link must be non-empty")
        if self.verifier_id is not None and not self.verifier_id.strip():
            raise ValueError("verifier_id must be non-empty when provided")
        _require_nonempty_unique(
            self.alternative_explanations,
            "primary_analysis.alternative_explanations",
        )


class AnalysisIntent(_GroundedV2Model):
    """Agent-authored analysis intent, hashed by the harness before confirmation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: AnalysisIntentSchemaVersion = "1.0"
    task_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    target_estimand: str = Field(min_length=1)
    primary_analyses: list[PrimaryAnalysis]
    supersedes_intent_id: Optional[str] = None

    def _validate_grounded_v2_semantics(self) -> None:
        for name, value in (
            ("task_id", self.task_id),
            ("intent_id", self.intent_id),
            ("target_estimand", self.target_estimand),
        ):
            if not value.strip():
                raise ValueError(f"analysis_intent.{name} must be non-empty")
        if not self.primary_analyses:
            raise ValueError("analysis_intent requires at least one primary analysis")
        _require_nonempty_unique(
            [analysis.analysis_id for analysis in self.primary_analyses],
            "analysis_intent.primary_analyses analysis_id values",
        )
        if (
            self.supersedes_intent_id is not None
            and not self.supersedes_intent_id.strip()
        ):
            raise ValueError("supersedes_intent_id must be non-empty when provided")
        if self.supersedes_intent_id == self.intent_id:
            raise ValueError("an analysis intent cannot supersede itself")


class StudySourceV2(BaseModel):
    """Agent-selected source declaration for one v2 study system."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(min_length=1)
    id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StudyRunV2(_GroundedV2Model):
    """One exploratory or confirmatory run with harness-owned lineage."""

    model_config = ConfigDict(extra="allow")

    run_id: str = Field(min_length=1)
    phase: StudyRunPhaseV2
    topology: str = Field(min_length=1)
    trajectory: Optional[str] = None
    trajectory_segments: list[str] = Field(default_factory=list)
    production_event_id: str = Field(min_length=1)
    intent_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def _validate_grounded_v2_semantics(self) -> None:
        for name, value in (
            ("run_id", self.run_id),
            ("topology", self.topology),
            ("production_event_id", self.production_event_id),
        ):
            if not value.strip():
                raise ValueError(f"study_run.{name} must be non-empty")
        has_trajectory = bool(self.trajectory and self.trajectory.strip())
        has_segments = bool(self.trajectory_segments)
        if has_trajectory == has_segments:
            raise ValueError(
                "study runs require exactly one of trajectory or "
                "trajectory_segments"
            )
        _require_nonempty_unique(
            self.trajectory_segments,
            "study_run.trajectory_segments",
        )
        if self.phase == "confirmatory" and not (
            self.intent_id and self.intent_id.strip()
        ):
            raise ValueError("confirmatory runs require intent_id")
        if self.intent_id is not None and not self.intent_id.strip():
            raise ValueError("intent_id must be non-empty when provided")


class StudySystemV2(_GroundedV2Model):
    """A structural/physical system participating in one or more comparisons."""

    model_config = ConfigDict(extra="allow")

    system_id: str = Field(min_length=1)
    source: StudySourceV2
    runs: list[StudyRunV2]
    conditions: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def _validate_grounded_v2_semantics(self) -> None:
        if not self.system_id.strip():
            raise ValueError("system_id must be non-empty")
        if not self.source.type.strip():
            raise ValueError("study system source.type must be non-empty")
        if not self.runs:
            raise ValueError("study systems require at least one run")
        _require_nonempty_unique(
            [run.run_id for run in self.runs],
            f"study system {self.system_id!r} run_id values",
        )


class StudyComparisonV2(_GroundedV2Model):
    """A declared contrast over arbitrary sets of study systems."""

    model_config = ConfigDict(extra="allow")

    comparison_id: str = Field(min_length=1)
    reference_system_ids: list[str]
    variant_system_ids: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)

    def _validate_grounded_v2_semantics(self) -> None:
        if not self.comparison_id.strip():
            raise ValueError("comparison_id must be non-empty")
        _require_nonempty_unique(
            self.reference_system_ids,
            "study_comparison.reference_system_ids",
        )
        if not self.reference_system_ids or not self.variant_system_ids:
            raise ValueError("study comparisons require both system-ID sides")
        _require_nonempty_unique(
            self.variant_system_ids,
            "study_comparison.variant_system_ids",
        )
        overlap = set(self.reference_system_ids) & set(self.variant_system_ids)
        if overlap:
            raise ValueError(
                "reference_system_ids and variant_system_ids must be disjoint: "
                f"{sorted(overlap)}"
            )


class StudyIndexV2(_GroundedV2Model):
    """Generalized systems-and-comparisons index for open-planning studies."""

    model_config = ConfigDict(extra="allow")

    schema_version: GroundedV2SchemaVersion = "2.0"
    task_id: str = Field(min_length=1)
    conditions: dict[str, Any]
    systems: list[StudySystemV2]
    comparisons: list[StudyComparisonV2]

    def _validate_grounded_v2_semantics(self) -> None:
        if not self.task_id.strip():
            raise ValueError("study_index.task_id must be non-empty")
        if len(self.systems) < 2:
            raise ValueError("study_index requires at least two systems")
        if not self.comparisons:
            raise ValueError("study_index requires at least one comparison")
        system_ids = [system.system_id for system in self.systems]
        _require_nonempty_unique(system_ids, "study_index.system_id values")
        comparison_ids = [
            comparison.comparison_id for comparison in self.comparisons
        ]
        _require_nonempty_unique(
            comparison_ids,
            "study_index.comparison_id values",
        )
        run_ids = [run.run_id for system in self.systems for run in system.runs]
        _require_nonempty_unique(run_ids, "study_index global run_id values")
        known_systems = set(system_ids)
        for comparison in self.comparisons:
            referenced = set(comparison.reference_system_ids) | set(
                comparison.variant_system_ids
            )
            unknown = referenced - known_systems
            if unknown:
                raise ValueError(
                    f"comparison {comparison.comparison_id!r} references unknown "
                    f"system IDs: {sorted(unknown)}"
                )


class PriorExpectationV2(_GroundedV2Model):
    """Structured open-book expectation, excluded from the grounding judge."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    outcome: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    sources: list[str | dict[str, Any]] = Field(default_factory=list)
    rationale: str = ""

    def _validate_grounded_v2_semantics(self) -> None:
        if self.outcome is None:
            if self.confidence is not None:
                raise ValueError(
                    "prior confidence requires a declared prior outcome"
                )
            return
        if not self.outcome.strip():
            raise ValueError("prior outcome must be non-empty when provided")


class MDVerdictV2(_GroundedV2Model):
    """The MD-only verdict; unresolved is distinct from a neutral direction."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    status: MDVerdictStatusV2
    outcome: Optional[str] = None
    basis: MDVerdictBasisV2
    confidence: float = Field(ge=0.0, le=1.0)
    cited_evidence_ids: list[str] = Field(default_factory=list)
    unresolved_reasons: list[str] = Field(default_factory=list)

    def _validate_grounded_v2_semantics(self) -> None:
        _require_nonempty_unique(
            self.cited_evidence_ids,
            "md_verdict.cited_evidence_ids",
        )
        _require_nonempty_unique(
            self.unresolved_reasons,
            "md_verdict.unresolved_reasons",
        )
        resolved_bases = {"direct_estimator", "validated_proxy"}
        unresolved_bases = {"mechanistic_only", "insufficient"}
        if self.status == "resolved":
            if not self.outcome or not self.outcome.strip():
                raise ValueError("resolved MD verdicts require outcome")
            if self.basis not in resolved_bases:
                raise ValueError(
                    "resolved MD verdicts require direct_estimator or "
                    "validated_proxy basis"
                )
            if not self.cited_evidence_ids:
                raise ValueError(
                    "resolved MD verdicts require at least one cited evidence ID"
                )
            if self.unresolved_reasons:
                raise ValueError(
                    "resolved MD verdicts cannot declare unresolved_reasons"
                )
            return
        if self.outcome is not None:
            raise ValueError("unresolved MD verdicts require outcome=null")
        if self.basis not in unresolved_bases:
            raise ValueError(
                "unresolved MD verdicts require mechanistic_only or "
                "insufficient basis"
            )
        if not self.unresolved_reasons:
            raise ValueError(
                "unresolved MD verdicts require at least one unresolved reason"
            )


class StudyEvidenceItemV2(BaseModel):
    """One evidence claim linked to a preregistered analysis and comparison."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    analysis_id: str = Field(min_length=1)
    comparison_id: str = Field(min_length=1)
    verifier_id: str = Field(min_length=1)
    claim_role: EvidenceClaimRoleV2
    estimand_link: str = Field(min_length=1)
    reported: dict[str, Any] = Field(default_factory=dict)
    uncertainty: Optional[float | dict[str, float]] = None
    artifacts: list[str] = Field(default_factory=list)


class EvidenceReportV2(_GroundedV2Model):
    """Open-book prior plus an independently judged MD-only verdict."""

    model_config = ConfigDict(extra="allow")

    schema_version: GroundedV2SchemaVersion = "2.0"
    task_id: str = Field(min_length=1)
    prior_expectation: PriorExpectationV2
    md_verdict: MDVerdictV2
    evidence: list[StudyEvidenceItemV2]
    reasoning: str = Field(min_length=1)
    limitations: list[str]

    def _validate_grounded_v2_semantics(self) -> None:
        if not self.task_id.strip():
            raise ValueError("evidence_report.task_id must be non-empty")
        if not self.evidence:
            raise ValueError("evidence_report requires at least one evidence item")
        evidence_ids = [item.id for item in self.evidence]
        _require_nonempty_unique(evidence_ids, "evidence_report evidence IDs")
        cited = set(self.md_verdict.cited_evidence_ids)
        unknown = cited - set(evidence_ids)
        if unknown:
            raise ValueError(
                "md_verdict cites unknown evidence IDs: " f"{sorted(unknown)}"
            )
        if not self.reasoning.strip():
            raise ValueError("evidence_report.reasoning must be non-empty")
        _require_nonempty_unique(
            self.limitations,
            "evidence_report.limitations",
        )
        if not self.limitations:
            raise ValueError("evidence_report requires at least one limitation")


class SubmissionOutputs(BaseModel):
    metrics: Optional[str] = "metrics.json"
    provenance: Optional[str] = "provenance.json"
    evidence_report: Optional[str] = "evidence_report.json"
    analysis_intent: Optional[str] = None
    decision_log: Optional[str] = None
    methods: Optional[str] = None
    figures: list[str] = Field(default_factory=list)
    topology: list[str] = Field(default_factory=list)
    trajectories: list[str] = Field(default_factory=list)
    checkpoints: list[str] = Field(default_factory=list)
    prepared_structure: Optional[str] = None
    minimized_structure: Optional[str] = None
    minimization_report: Optional[str] = None
    source_selection: Optional[str] = None
    # v0.3 paired Study tasks point to a role-based index rather than relying
    # on positional alignment between flat topology/trajectory lists.
    study_index: Optional[str] = None


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
    support_verdict: Optional[
        Literal["supported", "inconclusive", "contradicted"]
    ] = None
    logical_grounding_supported: Optional[bool] = None
    abstention_justified: Optional[bool] = None
    abstention_reason_codes: list[str] = Field(default_factory=list)
    cited_evidence_ids: list[str] = Field(default_factory=list)
    evidence_packet_hash: Optional[str] = None
    rationale: dict[str, str] = Field(default_factory=dict)


class RuntimeRecord(BaseModel):
    walltime_minutes: float = 0.0
    tokens: int = 0
    gpu_hours: float = 0.0


class StudyVerdict(BaseModel):
    """Conjunctive outcome for the grounded-correct Study protocol."""

    enabled: bool = False
    evaluation_complete: bool = False
    valid_md: bool = False
    evidence_verified: bool = False
    evidence_status: Optional[
        Literal["supported", "inconclusive", "contradicted"]
    ] = None
    reasoning_grounded: bool = False
    truth_correct: bool = False
    grounded_correct: bool = False
    decision_reasons: list[str] = Field(default_factory=list)
    evidence_packet_hash: Optional[str] = None


StudyResultClassV2 = Literal[
    "not_evaluated",
    "grounded_correct",
    "grounded_wrong",
    "unsupported_claim",
    "unresolved",
    "invalid_execution",
]


class StudyVerdictV2(_GroundedV2Model):
    """The three non-compensating MDStudyBench v2 evaluation gates."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    evaluation_complete: bool = False
    valid_execution: bool = False
    claim_supported: bool = False
    truth_available: bool = False
    truth_agreement: Optional[bool] = None
    grounded_correct: bool = False
    result_class: StudyResultClassV2 = "not_evaluated"
    decision_reason_codes: list[str] = Field(default_factory=list)
    evidence_packet_hash: Optional[str] = None
    analysis_intent_hash: Optional[str] = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    def _validate_grounded_v2_semantics(self) -> None:
        if not self.truth_available and self.truth_agreement is not None:
            raise ValueError(
                "truth_agreement must be null when held-out truth is unavailable"
            )
        if self.truth_agreement is not None and (
            not self.valid_execution or not self.claim_supported
        ):
            raise ValueError(
                "truth_agreement is evaluated only after valid_execution and "
                "claim_supported pass"
            )
        if self.grounded_correct and (
            not self.valid_execution
            or not self.claim_supported
            or self.truth_agreement is not True
        ):
            raise ValueError(
                "grounded_correct requires all three evaluation gates"
            )
        if self.result_class == "grounded_correct" and not self.grounded_correct:
            raise ValueError(
                "result_class='grounded_correct' requires grounded_correct=true"
            )
        if self.grounded_correct and self.result_class != "grounded_correct":
            raise ValueError(
                "grounded_correct=true requires result_class='grounded_correct'"
            )
        if self.result_class == "grounded_wrong" and (
            self.truth_agreement is not False
        ):
            raise ValueError(
                "result_class='grounded_wrong' requires truth_agreement=false"
            )
        if self.result_class == "invalid_execution" and self.valid_execution:
            raise ValueError(
                "result_class='invalid_execution' requires valid_execution=false"
            )
        if self.result_class == "unsupported_claim" and (
            not self.valid_execution or self.claim_supported
        ):
            raise ValueError(
                "result_class='unsupported_claim' requires valid execution and "
                "claim_supported=false"
            )


class Score(BaseModel):
    schema_version: SchemaVersion = "1.0"
    run_id: str = ""
    task_id: str
    primary_score: ScoreAxis
    status: ScoreStatus
    weighted_total: float = Field(ge=0.0, le=1.0)
    scores: dict[str, Optional[float]] = Field(default_factory=dict)
    # Per-capability profile (identity / physical_validity / fidelity /
    # provenance) derived from the weighted mean of checks tagged with each
    # capability. None means the capability was not exercised by this task.
    capability_scores: dict[str, Optional[float]] = Field(default_factory=dict)
    deterministic_checks: list[CheckResult] = Field(default_factory=list)
    ground_truth_checks: list[CheckResult] = Field(default_factory=list)
    llm_judge: LLMJudgeResult = Field(default_factory=LLMJudgeResult)
    study_verdict: StudyVerdictV2 | StudyVerdict = Field(
        default_factory=StudyVerdict
    )
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


class SolverContextInfo(BaseModel):
    """Harness-owned record of solver-visible skill or prompt context."""

    skill_usage: str = "unknown"
    source: str = "unknown"
    skill_names: list[str] = Field(default_factory=list)
    skill_files: list[str] = Field(default_factory=list)
    prompt_includes_skill_text: bool = False
    notes: str = ""


class BudgetSpec(BaseModel):
    max_walltime_minutes_per_task: int = 180
    max_gpu_hours: float = 0.0
    max_tokens_per_task: int = 0
    max_simulation_ns: float = 0.0


class RunConfig(BaseModel):
    schema_version: SchemaVersion = "1.0"
    benchmark_version: str = DEFAULT_BENCHMARK_VERSION
    run_id: str
    created_at: str
    execution_mode: ExecutionMode = "lite"
    judge_mode: JudgeMode = "deterministic"
    backend: BackendInfo = Field(default_factory=BackendInfo)
    harness: HarnessInfo = Field(default_factory=HarnessInfo)
    model: ModelInfo = Field(default_factory=ModelInfo)
    solver_context: SolverContextInfo = Field(default_factory=SolverContextInfo)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    tooling_condition: ToolingCondition = "unknown"
    task_ids: list[str] = Field(default_factory=list)
    dataset_dir: Optional[str] = None


class Attestation(BaseModel):
    """Machine-readable declaration of the conditions a run executed under.

    Written next to the run config so any third party can audit that two runs
    used the same public package + scorer and that no task-specific hints were
    injected into the solver. The scorer records its presence/consistency and
    marks runs without it ``verified=false``.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: SchemaVersion = "1.0"
    run_id: str = ""
    benchmark_version: str = DEFAULT_BENCHMARK_VERSION
    scorer: str = "mdclaw"
    scorer_version: str = ""
    public_package_sha256: str = ""
    tooling_condition: ToolingCondition = "unknown"
    solver_context: SolverContextInfo = Field(default_factory=SolverContextInfo)
    no_task_specific_hints_injected: bool = True
    created_at: str = ""


class RunSummary(BaseModel):
    schema_version: SchemaVersion = "1.0"
    benchmark_version: str = DEFAULT_BENCHMARK_VERSION
    run_id: str
    created_at: str
    execution_mode: ExecutionMode = "lite"
    judge_mode: JudgeMode = "deterministic"
    backend: BackendInfo = Field(default_factory=BackendInfo)
    harness: HarnessInfo = Field(default_factory=HarnessInfo)
    model: ModelInfo = Field(default_factory=ModelInfo)
    solver_context: SolverContextInfo = Field(default_factory=SolverContextInfo)
    tooling_condition: ToolingCondition = "unknown"
    verified: bool = False
    attestation: Optional[Attestation] = None
    n_tasks: int = 0
    n_failed_tasks: int = 0
    overall_score: float = 0.0
    grounded_correct_rate: float = Field(ge=0.0, le=1.0, default=0.0)
    scores: dict[str, Optional[float]] = Field(default_factory=dict)
    capability_scores: dict[str, Optional[float]] = Field(default_factory=dict)
    task_scores: list[dict] = Field(default_factory=list)
    runtime: dict = Field(default_factory=dict)
    contract_diagnostics: dict = Field(default_factory=dict)
    harness_diagnostics: dict = Field(default_factory=dict)
