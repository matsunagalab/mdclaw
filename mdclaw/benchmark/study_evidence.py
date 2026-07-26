"""Truth-blind verification of MDStudyBench evidence.

The verifier deliberately accepts only submission-owned inputs: a manifest,
an evidence report, and the submission directory containing their artifacts.
It has no task or hidden-truth argument.  Its output is therefore safe to hand
to a reasoning judge before the claimed scientific direction is compared with
the private answer.

The canonical v0.3 manifest points ``outputs.study_index`` at a JSON document
containing the role-based systems.  Two compatibility layouts are also
supported:

* the v0.2 ``outputs.trajectories`` / ``outputs.topology`` pair; and
* inline role-based ``systems`` with ``reference`` and ``variant`` replicas.

The role-based form is the unambiguous way to submit more than one replica per
condition.  Extra legacy trajectories are still inspected, but are rejected as
unassigned unless ``outputs.replica_roles`` labels every entry.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_GENERIC_METRICS = {"ca_rmsf", "contact_count"}
_MIN_FRAMES_PER_REPLICA = 5
_MIN_ALIGNED_COORDINATE_RANGE_NM = 1.0e-5
_ROLE_ALIASES = {
    "reference": "reference",
    "ref": "reference",
    "control": "reference",
    "wild_type": "reference",
    "wild-type": "reference",
    "wt": "reference",
    "variant": "variant",
    "test": "variant",
    "mutant": "variant",
    "mut": "variant",
}


@dataclass(frozen=True)
class _RunSpec:
    key: str
    role: str
    replica_id: str
    topology: str | None
    trajectories: tuple[str, ...]
    source: dict[str, Any]
    system_metadata: dict[str, Any]
    replica_metadata: dict[str, Any]


def build_verified_evidence_packet(
    submission_dir: str | Path,
    manifest: dict[str, Any],
    evidence_report: dict[str, Any],
    *,
    n_blocks: int = 5,
    inconclusive_sigma: float = 1.0,
    mismatch_tolerance_fraction: float = 0.1,
) -> dict[str, Any]:
    """Build a truth-blind packet from raw comparative-MD artifacts.

    ``ca_rmsf`` and ``contact_count`` evidence items are recomputed for every
    declared replica.  Unsupported metrics remain visible as unverified
    supplemental evidence.  The function returns diagnostics instead of
    raising for malformed or missing submission artifacts so the caller can
    persist a complete audit record.
    """
    root = Path(submission_dir).resolve()
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(manifest, dict):
        manifest = {}
        errors.append("manifest must be an object")
    if not isinstance(evidence_report, dict):
        evidence_report = {}
        errors.append("evidence_report must be an object")

    study_index, index_errors = _read_study_index(root, manifest)
    errors.extend(index_errors)
    run_specs, manifest_errors, manifest_warnings = _normalise_runs(
        manifest, study_index
    )
    errors.extend(manifest_errors)
    warnings.extend(manifest_warnings)

    task_ids = {
        str(task_id)
        for task_id in (
            manifest.get("task_id"),
            study_index.get("task_id") if isinstance(study_index, dict) else None,
            evidence_report.get("task_id"),
        )
        if task_id is not None and str(task_id).strip()
    }
    if len(task_ids) > 1:
        errors.append(
            "task_id mismatch across manifest, study_index, and evidence_report: "
            + ", ".join(sorted(task_ids))
        )

    public_runs, loaded_runs, artifacts, load_errors = _load_runs(root, run_specs)
    errors.extend(load_errors)

    duplicates = _find_duplicate_artifacts(artifacts)
    duplicate_trajectories = duplicates["trajectories"]
    if duplicate_trajectories:
        warnings.append(
            "identical trajectory content was declared more than once; "
            "duplicate files do not count as independent replicas"
        )

    evidence_items = _evidence_items(evidence_report)
    verified_items = [
        _verify_evidence_item(
            item,
            loaded_runs,
            evidence_index=index,
            n_blocks=max(1, int(n_blocks)),
            inconclusive_sigma=max(0.0, float(inconclusive_sigma)),
            mismatch_tolerance_fraction=max(
                0.0, float(mismatch_tolerance_fraction)
            ),
        )
        for index, item in enumerate(evidence_items)
    ]
    _invalidate_duplicate_evidence_ids(verified_items)

    role_counts = {
        role: sum(1 for run in public_runs if run["role"] == role)
        for role in ("reference", "variant")
    }
    loadable_role_counts = {
        role: sum(1 for run in loaded_runs.values() if run["spec"].role == role)
        for role in ("reference", "variant")
    }
    artifact_valid = (
        not errors
        and role_counts["reference"] > 0
        and role_counts["variant"] > 0
        and role_counts == loadable_role_counts
    )

    generic = [
        item for item in verified_items if item["metric"] in _GENERIC_METRICS
    ]
    verified_generic = [
        item for item in generic
        if item["verification_status"] == "verified"
    ]
    # Evidence items are independent claims.  One mismatched or differently
    # defined supplemental observable must not erase another native observable
    # that was successfully recomputed from every replica.  The judge may cite
    # only verified, resolved IDs, so failed items still cannot support a pass.
    generic_verified = bool(verified_generic)
    evidence_verified = (
        artifact_valid and generic_verified and not duplicate_trajectories
    )

    return {
        "schema_version": "1.0",
        "kind": "mdstudybench_verified_evidence",
        "truth_blind": True,
        "task_id": next(iter(task_ids)) if len(task_ids) == 1 else None,
        "conclusion": _submitted_conclusion(evidence_report),
        "declared_study_context": {
            "study_index": {
                str(key): _json_safe_value(value)
                for key, value in (study_index or {}).items()
                if key not in {"schema_version", "task_id", "systems"}
            },
            "manifest": {
                str(key): _json_safe_value(value)
                for key, value in manifest.items()
                if key in {
                    "generated_by",
                    "methods",
                    "conditions",
                    "study_design",
                    "limitations",
                }
            },
            "status": "submission_declared_not_independently_verified",
        },
        "systems": _group_public_runs(public_runs),
        "artifacts": artifacts,
        "duplicates": duplicates,
        "evidence": verified_items,
        "summary": {
            "artifact_valid": artifact_valid,
            "evidence_verified": evidence_verified,
            "run_count": len(public_runs),
            "loadable_run_count": len(loaded_runs),
            "replica_count_by_role": role_counts,
            "loadable_replica_count_by_role": loadable_role_counts,
            "verified_evidence_count": sum(
                item["verification_status"] == "verified"
                for item in verified_items
            ),
            "unverified_evidence_count": sum(
                item["verification_status"] != "verified"
                for item in verified_items
            ),
            "duplicate_trajectory_detected": bool(duplicate_trajectories),
        },
        "errors": errors,
        "warnings": warnings,
    }


def verified_evidence_hash(packet: dict[str, Any]) -> str:
    """Return a stable SHA-256 for a packet without modifying the packet.

    Callers can persist this digest beside judge output to bind the reasoning
    decision to the exact verified evidence it consumed.
    """
    canonical = json.dumps(
        packet,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _normalise_runs(
    manifest: dict[str, Any],
    study_index: dict[str, Any] | None,
) -> tuple[list[_RunSpec], list[str], list[str]]:
    systems = study_index.get("systems") if isinstance(study_index, dict) else None
    if not isinstance(systems, list):
        systems = manifest.get("systems")
    if not isinstance(systems, list):
        outputs = manifest.get("outputs")
        if isinstance(outputs, dict) and isinstance(outputs.get("systems"), list):
            systems = outputs["systems"]
    if isinstance(systems, list):
        return _normalise_role_systems(systems)
    return _normalise_legacy_outputs(manifest)


def _read_study_index(
    root: Path, manifest: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[str]]:
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict) or "study_index" not in outputs:
        return None, []
    relative = _path_value(outputs.get("study_index"))
    if relative is None:
        return None, ["outputs.study_index must be a JSON file path"]
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None, [f"study_index path escapes submission directory: {relative}"]
    if not path.is_file():
        return None, [f"study_index file not found: {relative}"]
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"study_index JSON load failed: {exc}"]
    if not isinstance(payload, dict):
        return None, ["study_index must contain a JSON object"]
    return payload, []


def _normalise_role_systems(
    systems: list[Any],
) -> tuple[list[_RunSpec], list[str], list[str]]:
    runs: list[_RunSpec] = []
    errors: list[str] = []
    seen_replica_ids: set[str] = set()
    role_system_counts = {"reference": 0, "variant": 0}

    for system_index, raw_system in enumerate(systems):
        where = f"systems[{system_index}]"
        if not isinstance(raw_system, dict):
            errors.append(f"{where} must be an object")
            continue
        role = _normalise_role(raw_system.get("role"))
        if role not in {"reference", "variant"}:
            errors.append(f"{where}.role must be reference or variant")
            role = "unassigned"
        else:
            role_system_counts[role] += 1

        replicas = raw_system.get("replicas")
        if replicas is None:
            replicas = [raw_system]
        if not isinstance(replicas, list) or not replicas:
            errors.append(f"{where}.replicas must be a non-empty list")
            continue

        inherited_topology = _path_value(raw_system.get("topology"))
        source = (
            _json_safe_value(raw_system.get("source"))
            if isinstance(raw_system.get("source"), dict)
            else {}
        )
        system_metadata = {
            str(key): _json_safe_value(value)
            for key, value in raw_system.items()
            if key not in {"role", "source", "replicas", "topology"}
        }
        for replica_index, raw_replica in enumerate(replicas):
            replica_where = f"{where}.replicas[{replica_index}]"
            if not isinstance(raw_replica, dict):
                errors.append(f"{replica_where} must be an object")
                continue
            replica_id = str(
                raw_replica.get("replica_id")
                or raw_replica.get("id")
                or f"replica_{replica_index + 1}"
            )
            if replica_id in seen_replica_ids:
                errors.append(f"duplicate replica_id {replica_id!r}")
            seen_replica_ids.add(replica_id)

            topology = _path_value(raw_replica.get("topology")) or inherited_topology
            trajectories = _trajectory_values(raw_replica)
            if topology is None:
                errors.append(f"{replica_where}.topology is required")
            if not trajectories:
                errors.append(f"{replica_where} requires a trajectory")
            runs.append(
                _RunSpec(
                    key=f"system_{system_index}_replica_{replica_index}",
                    role=role,
                    replica_id=replica_id,
                    topology=topology,
                    trajectories=tuple(trajectories),
                    source=source,
                    system_metadata=system_metadata,
                    replica_metadata=(
                        _json_safe_value(raw_replica.get("metadata"))
                        if isinstance(raw_replica.get("metadata"), dict)
                        else {}
                    ),
                )
            )

    for required in ("reference", "variant"):
        count = role_system_counts[required]
        if count != 1:
            errors.append(
                f"systems requires exactly one {required} system; found {count}"
            )
    return runs, errors, []


def _normalise_legacy_outputs(
    manifest: dict[str, Any],
) -> tuple[list[_RunSpec], list[str], list[str]]:
    errors: list[str] = []
    warnings = [
        "legacy outputs.trajectories/topology layout accepted; use role-based "
        "systems for unambiguous multi-replica studies"
    ]
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        return [], ["manifest requires systems or outputs"], warnings

    trajectories = _path_list(outputs.get("trajectories"))
    topologies = _path_list(outputs.get("topology"))
    raw_roles = outputs.get("replica_roles", outputs.get("trajectory_roles"))
    roles = raw_roles if isinstance(raw_roles, list) else []
    if not trajectories:
        errors.append("outputs.trajectories must contain comparative trajectories")
    if not topologies:
        errors.append("outputs.topology must contain comparative topologies")
    if roles and len(roles) != len(trajectories):
        errors.append("outputs.replica_roles must label every trajectory")

    runs: list[_RunSpec] = []
    for index, trajectory in enumerate(trajectories):
        if roles and index < len(roles):
            role = _normalise_role(roles[index])
        elif index == 0:
            role = "reference"
        elif index == 1:
            role = "variant"
        else:
            role = "unassigned"
            errors.append(
                f"outputs.trajectories[{index}] has no condition role; add "
                "outputs.replica_roles or use systems[].replicas"
            )
        if role not in {"reference", "variant"}:
            errors.append(f"outputs trajectory role at index {index} is invalid")

        topology: str | None = None
        if len(topologies) == len(trajectories):
            topology = topologies[index]
        elif len(topologies) == 1:
            topology = topologies[0]
        elif len(topologies) >= 2 and role in {"reference", "variant"}:
            topology = topologies[0 if role == "reference" else 1]
        if topology is None:
            errors.append(f"no topology can be assigned to legacy trajectory {index}")

        runs.append(
            _RunSpec(
                key=f"legacy_{index}",
                role=role,
                replica_id=f"replica_{index + 1}",
                topology=topology,
                trajectories=(trajectory,),
                source={},
                system_metadata={},
                replica_metadata={},
            )
        )

    present = {run.role for run in runs}
    for required in ("reference", "variant"):
        if required not in present:
            errors.append(f"legacy outputs requires a {required} trajectory")
    return runs, errors, warnings


def _normalise_role(value: Any) -> str:
    return _ROLE_ALIASES.get(str(value or "").strip().lower(), "unassigned")


def _path_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("path", "file"):
            path = value.get(key)
            if isinstance(path, str) and path.strip():
                return path.strip()
    return None


def _path_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [path for item in value if (path := _path_value(item)) is not None]
    path = _path_value(value)
    return [path] if path is not None else []


def _trajectory_values(replica: dict[str, Any]) -> list[str]:
    for key in ("trajectory_segments", "trajectories", "trajectory"):
        if key in replica:
            return _path_list(replica[key])
    return []


def _load_runs(
    root: Path,
    specs: list[_RunSpec],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]:
    public_runs: list[dict[str, Any]] = []
    loaded_runs: dict[str, dict[str, Any]] = {}
    artifacts: list[dict[str, Any]] = []
    errors: list[str] = []

    try:
        import mdtraj as md
        import numpy as np
    except ImportError as exc:
        errors.append(f"MD evidence runtime dependency unavailable: {exc}")
        md = None
        np = None

    for spec in specs:
        run_errors: list[str] = []
        public = {
            "run_id": spec.key,
            "role": spec.role,
            "replica_id": spec.replica_id,
            "source": spec.source,
            "system_metadata": spec.system_metadata,
            "replica_metadata": spec.replica_metadata,
            "topology": None,
            "trajectories": [],
            "n_frames": 0,
            "n_atoms": None,
            "load_status": "failed",
            "errors": run_errors,
        }
        public_runs.append(public)

        top_path = None
        if spec.topology is None:
            run_errors.append("topology path missing")
        else:
            top_path, artifact, error = _inspect_artifact(
                root, spec.topology, "topology", spec.key
            )
            if artifact is not None:
                public["topology"] = artifact
                artifacts.append(artifact)
            if error:
                run_errors.append(error)

        trajectory_paths: list[Path] = []
        for trajectory in spec.trajectories:
            path, artifact, error = _inspect_artifact(
                root, trajectory, "trajectory", spec.key
            )
            if artifact is not None:
                public["trajectories"].append(artifact)
                artifacts.append(artifact)
            if path is not None:
                trajectory_paths.append(path)
            if error:
                run_errors.append(error)

        trajectory = None
        if md is not None and top_path is not None and trajectory_paths and not run_errors:
            try:
                parts = [md.load(str(path), top=str(top_path)) for path in trajectory_paths]
                trajectory = (
                    parts[0]
                    if len(parts) == 1
                    else md.join(
                        parts,
                        check_topology=True,
                        discard_overlapping_frames=False,
                    )
                )
                if trajectory.n_frames < 1:
                    raise ValueError("trajectory has zero frames")
                if trajectory.n_frames < _MIN_FRAMES_PER_REPLICA:
                    raise ValueError(
                        "trajectory has "
                        f"{trajectory.n_frames} frames; require at least "
                        f"{_MIN_FRAMES_PER_REPLICA}"
                    )
                if not bool(np.isfinite(trajectory.xyz).all()):
                    raise ValueError("trajectory contains non-finite coordinates")
                coordinate_range = _aligned_structural_coordinate_range(
                    trajectory,
                    np,
                )
                if coordinate_range <= _MIN_ALIGNED_COORDINATE_RANGE_NM:
                    raise ValueError(
                        "trajectory has no detectable internal structural motion "
                        "after rigid-body alignment"
                    )
                public["n_frames"] = int(trajectory.n_frames)
                public["n_atoms"] = int(trajectory.n_atoms)
                public["load_status"] = "loaded"
            except Exception as exc:  # noqa: BLE001 -- artifact decoder boundary
                run_errors.append(f"trajectory load failed: {exc}")

        if run_errors:
            errors.extend(f"{spec.key}: {message}" for message in run_errors)
        elif trajectory is not None:
            loaded_runs[spec.key] = {"spec": spec, "trajectory": trajectory}

    return public_runs, loaded_runs, artifacts, errors


def _aligned_structural_coordinate_range(trajectory: Any, np: Any) -> float:
    """Measure internal motion while rejecting pure translation/rotation.

    Protein C-alpha atoms are preferred because solvent or ion motion must not
    make an otherwise static submitted structure look like production MD.  The
    fallback supports non-protein studies with at least three non-water heavy
    atoms.  ``atom_slice`` returns a copy, so alignment does not alter the
    trajectory later used for evidence recomputation.
    """
    indices = trajectory.topology.select("protein and name CA")
    if len(indices) < 3:
        indices = trajectory.topology.select("not water and not element H")
    if len(indices) < 3:
        indices = np.arange(trajectory.n_atoms, dtype=int)
    if len(indices) < 2:
        return 0.0
    structural = trajectory.atom_slice(indices)
    structural.superpose(structural, 0)
    return float(np.max(np.ptp(structural.xyz, axis=0)))


def _inspect_artifact(
    root: Path, relative: str, kind: str, run_key: str
) -> tuple[Path | None, dict[str, Any] | None, str | None]:
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None, None, f"{kind} path escapes submission directory: {relative}"
    if not path.is_file():
        return None, None, f"{kind} file not found: {relative}"
    artifact = {
        "kind": kind,
        "run_id": run_key,
        "path": relative,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }
    return path, artifact, None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_duplicate_artifacts(
    artifacts: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {
        "trajectories": [],
        "topologies": [],
    }
    for kind, output_key in (("trajectory", "trajectories"), ("topology", "topologies")):
        by_hash: dict[str, list[dict[str, str]]] = {}
        for artifact in artifacts:
            if artifact["kind"] != kind:
                continue
            by_hash.setdefault(artifact["sha256"], []).append(
                {"run_id": artifact["run_id"], "path": artifact["path"]}
            )
        result[output_key] = [
            {"sha256": digest, "references": references}
            for digest, references in by_hash.items()
            if len(references) > 1
        ]
    return result


def _evidence_items(evidence_report: dict[str, Any]) -> list[dict[str, Any]]:
    raw = evidence_report.get("evidence")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        return [item for item in raw["items"] if isinstance(item, dict)]
    legacy = evidence_report.get("observables")
    if isinstance(legacy, list):
        return [item for item in legacy if isinstance(item, dict)]
    return []


def _verify_evidence_item(
    submitted: dict[str, Any],
    loaded_runs: dict[str, dict[str, Any]],
    *,
    evidence_index: int,
    n_blocks: int,
    inconclusive_sigma: float,
    mismatch_tolerance_fraction: float,
) -> dict[str, Any]:
    metric = str(submitted.get("metric") or submitted.get("name") or "custom")
    submitted_has_nonfinite = _has_nonfinite_number(submitted)
    result: dict[str, Any] = {
        "id": str(
            submitted.get("id")
            or submitted.get("name")
            or f"evidence_{evidence_index + 1}"
        ),
        "metric": metric,
        "verification_status": "unverified_supplemental",
        "submitted": _json_safe_value(submitted),
        "per_replica": [],
        "recomputed": None,
        "reported": _reported_values(submitted, metric=metric),
        "mismatch": None,
    }
    if submitted_has_nonfinite:
        result["verification_status"] = "failed"
        result["verification_message"] = (
            "evidence item contains a non-finite numeric value"
        )
        return result
    if metric not in _GENERIC_METRICS:
        result["verification_message"] = (
            "custom metric retained as supplemental evidence; raw recomputation "
            "is not implemented"
        )
        return result
    reported = result["reported"]
    if not reported.get("unit_valid", True):
        result["verification_status"] = "failed"
        result["verification_message"] = (
            f"unsupported unit for {metric}: {reported.get('submitted_unit')!r}"
        )
        return result
    selection_a = submitted.get("selection_a", submitted.get("selection"))
    selection_b = submitted.get("selection_b")
    if metric == "ca_rmsf" and not isinstance(selection_a, str):
        selection_a = "protein and name CA"
    if not isinstance(selection_a, str) or not selection_a.strip():
        result["verification_status"] = "failed"
        result["verification_message"] = "metric requires selection"
        return result
    if metric == "contact_count" and (
        not isinstance(selection_b, str) or not selection_b.strip()
    ):
        result["verification_status"] = "failed"
        result["verification_message"] = "contact_count requires selection_b"
        return result

    cutoff = submitted.get("contact_cutoff_nm", submitted.get("cutoff_nm", 0.45))
    try:
        cutoff_nm = float(cutoff)
    except (TypeError, ValueError):
        result["verification_status"] = "failed"
        result["verification_message"] = "contact cutoff must be numeric"
        return result

    metric_errors: list[str] = []
    values_by_role: dict[str, list[dict[str, float]]] = {
        "reference": [],
        "variant": [],
    }
    for run in loaded_runs.values():
        spec: _RunSpec = run["spec"]
        if spec.role not in values_by_role:
            continue
        try:
            blocks = _metric_block_values(
                run["trajectory"],
                metric,
                selection_a,
                selection_b,
                cutoff_nm,
                n_blocks,
            )
            if not blocks:
                raise ValueError("selection matched no atoms")
            value = float(statistics.fmean(blocks))
            uncertainty = _sem(blocks)
            record = {
                "run_id": spec.key,
                "role": spec.role,
                "replica_id": spec.replica_id,
                "value": value,
                "block_uncertainty": uncertainty,
                "block_count": len(blocks),
            }
            result["per_replica"].append(record)
            values_by_role[spec.role].append(record)
        except Exception as exc:  # noqa: BLE001 -- selection/metric boundary
            metric_errors.append(f"{spec.key}: {exc}")

    if metric_errors:
        result["verification_status"] = "failed"
        result["verification_message"] = "; ".join(metric_errors)
        return result
    if not values_by_role["reference"] or not values_by_role["variant"]:
        result["verification_status"] = "failed"
        result["verification_message"] = "both reference and variant replicas are required"
        return result

    reference = _aggregate_replicas(values_by_role["reference"])
    variant = _aggregate_replicas(values_by_role["variant"])
    delta = variant["value"] - reference["value"]
    uncertainty = math.sqrt(reference["uncertainty"] ** 2 + variant["uncertainty"] ** 2)
    scale = max(abs(reference["value"]), abs(variant["value"]), 1.0)
    neutral = math.isclose(delta, 0.0, rel_tol=1e-9, abs_tol=scale * 1e-12)
    estimate_direction = "neutral" if neutral else ("increase" if delta > 0 else "decrease")
    precision_status = (
        "inconclusive"
        if neutral
        or (uncertainty > 0 and abs(delta) < inconclusive_sigma * uncertainty)
        else "resolved"
    )
    result["recomputed"] = {
        "reference": reference["value"],
        "variant": variant["value"],
        "delta": delta,
        "uncertainty": uncertainty,
        "estimate_direction": estimate_direction,
        "precision_status": precision_status,
        "reference_replica_count": len(values_by_role["reference"]),
        "variant_replica_count": len(values_by_role["variant"]),
        "reference_uncertainty": reference["uncertainty"],
        "variant_uncertainty": variant["uncertainty"],
        "unit": "nm" if metric == "ca_rmsf" else "count",
    }

    reported = result["reported"]
    if reported["reference"] is None or reported["variant"] is None:
        result["verification_status"] = "missing_reported_values"
        result["verification_message"] = (
            "raw metric recomputed, but submitted reference/variant values are missing"
        )
        return result

    mismatch = _reported_mismatch(
        reported["reference"],
        reported["variant"],
        reference["value"],
        variant["value"],
        mismatch_tolerance_fraction,
    )
    result["mismatch"] = mismatch
    if mismatch["exceeds_tolerance"]:
        result["verification_status"] = "reported_mismatch"
        result["verification_message"] = "reported values do not match raw recomputation"
    else:
        result["verification_status"] = "verified"
        result["verification_message"] = "reported values match all-replica recomputation"
    return result


def _metric_block_values(
    trajectory: Any,
    metric: str,
    selection_a: str,
    selection_b: str | None,
    cutoff_nm: float,
    n_blocks: int,
) -> list[float]:
    import mdtraj as md
    import numpy as np

    if metric == "ca_rmsf":
        selected = trajectory.topology.select(selection_a)
        if len(selected) == 0:
            return []
        subset = trajectory.atom_slice(selected)
        values: list[float] = []
        for lower, upper in _frame_blocks(subset.n_frames, n_blocks):
            block = subset[lower:upper]
            block.superpose(block, 0)
            rmsf = md.rmsf(block, block, 0)
            # MDTraj and MDClaw's public RMSF analysis both use nanometres.
            values.append(float(np.mean(rmsf)))
        return values

    group_a = trajectory.topology.select(selection_a)
    group_b = trajectory.topology.select(str(selection_b))
    if len(group_a) == 0 or len(group_b) == 0:
        return []
    heavy = set(int(index) for index in trajectory.topology.select("not element H"))
    group_a = [int(index) for index in group_a if int(index) in heavy]
    group_b = [int(index) for index in group_b if int(index) in heavy]
    if not group_a or not group_b:
        return []
    pairs = np.asarray([(a, b) for a in group_a for b in group_b], dtype=int)
    distances = md.compute_distances(trajectory, pairs)
    counts = (distances < cutoff_nm).sum(axis=1)
    return [
        float(np.mean(counts[lower:upper]))
        for lower, upper in _frame_blocks(len(counts), n_blocks)
    ]


def _frame_blocks(n_frames: int, n_blocks: int) -> list[tuple[int, int]]:
    count = max(1, min(n_blocks, n_frames))
    return [
        (lower, upper)
        for block in range(count)
        if (
            upper := int(round((block + 1) * n_frames / count))
        ) > (lower := int(round(block * n_frames / count)))
    ]


def _sem(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return statistics.stdev(values) / math.sqrt(len(values))


def _aggregate_replicas(records: list[dict[str, float]]) -> dict[str, float]:
    values = [record["value"] for record in records]
    within = math.sqrt(
        sum(record["block_uncertainty"] ** 2 for record in records)
    ) / len(records)
    between = _sem(values)
    return {
        "value": float(statistics.fmean(values)),
        "uncertainty": math.sqrt(within**2 + between**2),
        "within_replica_uncertainty": within,
        "between_replica_uncertainty": between,
    }


def _reported_values(
    item: dict[str, Any], *, metric: str
) -> dict[str, float | str | None]:
    def number(*keys: str) -> float | None:
        for key in keys:
            value = item.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                try:
                    value = float(value)
                except (OverflowError, ValueError):
                    return None
                return value if math.isfinite(value) else None
        return None

    reference = number("reference", "reference_value", "wt_value")
    variant = number("variant", "variant_value", "mutant_value")
    uncertainty = number("uncertainty")
    raw_unit = str(item.get("unit") or "").strip().lower()
    canonical_unit = "nm" if metric == "ca_rmsf" else "count"
    angstrom_units = {
        "a",
        "å",
        "angstrom",
        "angstroms",
    }
    nm_units = {"", "nm", "nanometer", "nanometers", "nanometre", "nanometres"}
    count_units = {"", "count", "counts", "dimensionless", "1"}
    unit_valid = (
        raw_unit in (nm_units | angstrom_units)
        if metric == "ca_rmsf"
        else raw_unit in count_units
    )
    if metric == "ca_rmsf" and raw_unit in angstrom_units:
        reference = reference / 10.0 if reference is not None else None
        variant = variant / 10.0 if variant is not None else None
        uncertainty = uncertainty / 10.0 if uncertainty is not None else None
    return {
        "reference": reference,
        "variant": variant,
        "uncertainty": uncertainty,
        "submitted_unit": raw_unit or None,
        "canonical_unit": canonical_unit,
        "unit_valid": unit_valid,
    }


def _invalidate_duplicate_evidence_ids(items: list[dict[str, Any]]) -> None:
    counts: dict[str, int] = {}
    for item in items:
        identifier = str(item.get("id") or "")
        counts[identifier] = counts.get(identifier, 0) + 1
    duplicates = {identifier for identifier, count in counts.items() if count > 1}
    for item in items:
        if str(item.get("id") or "") in duplicates:
            item["verification_status"] = "failed"
            item["verification_message"] = (
                f"evidence id is not unique: {item.get('id')!r}"
            )


def _has_nonfinite_number(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_nonfinite_number(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_nonfinite_number(item) for item in value)
    return False


def _json_safe_value(value: Any) -> Any:
    """Return a JSON-hashable audit copy even for malformed legacy input."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _reported_mismatch(
    reported_reference: float,
    reported_variant: float,
    recomputed_reference: float,
    recomputed_variant: float,
    tolerance: float,
) -> dict[str, Any]:
    def relative(reported: float, recomputed: float) -> float:
        return abs(reported - recomputed) / max(abs(recomputed), 1e-9)

    reference_error = relative(reported_reference, recomputed_reference)
    variant_error = relative(reported_variant, recomputed_variant)
    reported_delta = reported_variant - reported_reference
    recomputed_delta = recomputed_variant - recomputed_reference
    scale = max(
        abs(recomputed_reference),
        abs(recomputed_variant),
        1.0,
    )
    recomputed_neutral = math.isclose(
        recomputed_delta,
        0.0,
        rel_tol=1e-9,
        abs_tol=scale * 1e-12,
    )
    direction_matches = recomputed_neutral or (
        (reported_delta > 0) == (recomputed_delta > 0)
        and not math.isclose(reported_delta, 0.0, abs_tol=scale * 1e-12)
    )
    return {
        "reference_relative_error": reference_error,
        "variant_relative_error": variant_error,
        "tolerance_fraction": tolerance,
        "direction_matches": direction_matches,
        "exceeds_tolerance": (
            max(reference_error, variant_error) > tolerance
            or not direction_matches
        ),
    }


def _submitted_conclusion(evidence_report: dict[str, Any]) -> dict[str, Any]:
    conclusion = evidence_report.get("conclusion")
    if not isinstance(conclusion, dict):
        conclusion = evidence_report.get("effect")
    if not isinstance(conclusion, dict):
        return {}
    return {
        key: _json_safe_value(conclusion[key])
        for key in ("direction", "evidence_status", "confidence")
        if key in conclusion
    }


def _group_public_runs(public_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: list[dict[str, Any]] = []
    for role in ("reference", "variant", "unassigned"):
        runs = [run for run in public_runs if run["role"] == role]
        if not runs:
            continue
        grouped.append(
            {
                "role": role,
                "source": {
                    "declaration": runs[0].get("source") or {},
                    "verification_status": "submission_declared",
                },
                "system_metadata": runs[0].get("system_metadata") or {},
                "replicas": [
                    {
                        key: value
                        for key, value in run.items()
                        if key not in {"source", "system_metadata"}
                    }
                    for run in runs
                ],
            }
        )
    return grouped
