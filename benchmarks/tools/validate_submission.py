#!/usr/bin/env python3
"""Tool-neutral public preflight for MD benchmark submissions.

This script intentionally uses only the public ``submission_contract.json`` and
the solver's ``submission/`` directory. It does not read private task metadata
or hidden truth files, so it is safe to ship in the public benchmark package.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path, PurePosixPath
from typing import Any


OPENMM_TRIPLE = (
    "topology/system.xml",
    "topology/topology.pdb",
    "topology/state.xml",
)

_V2_MANIFEST_OUTPUTS = {
    "confirmatory_plan": "confirmatory_plan.json",
    "claim": "claim.json",
    "episode": "episode/episode.json",
}
_V2_PLAN_FIELDS = {"schema_version", "task_id", "runs"}
_V2_PLAN_REQUIRED_FIELDS = {"task_id", "runs"}
_V2_RUN_FIELDS = {
    "run_id",
    "condition_role",
    "job_dir",
    "node_id",
    "simulation_time_ns",
}
_V2_CLAIM_FIELDS = {
    "schema_version",
    "task_id",
    "status",
    "outcome",
}
_V2_CLAIM_REQUIRED_FIELDS = {"task_id", "status", "outcome"}
_V2_INPUT_ARTIFACTS = {"base_system", "topology", "start_state"}
_V2_OUTPUT_ARTIFACTS = {
    "trajectory",
    "state",
    "energy",
    "runtime_system",
    "integrator",
}
_V2_ARTIFACT_RECORD_FIELDS = {"path", "sha256", "bytes"}

_MAX_ABS_ENERGY_PER_PARTICLE_KJ_MOL = 1.0e6
_CLASH_OVERLAP_FRACTION = 0.6
_MAX_CLASHES = 0
_TWO_TO_ONE_SIXTH = 2.0 ** (1.0 / 6.0)
_METAL_ELEMENTS = {
    "Li",
    "Be",
    "Na",
    "Mg",
    "Al",
    "K",
    "Ca",
    "Sc",
    "Ti",
    "V",
    "Cr",
    "Mn",
    "Fe",
    "Co",
    "Ni",
    "Cu",
    "Zn",
    "Ga",
    "Rb",
    "Sr",
    "Y",
    "Zr",
    "Nb",
    "Mo",
    "Tc",
    "Ru",
    "Rh",
    "Pd",
    "Ag",
    "Cd",
    "In",
    "Sn",
    "Cs",
    "Ba",
    "La",
    "Hf",
    "Ta",
    "W",
    "Re",
    "Os",
    "Ir",
    "Pt",
    "Au",
    "Hg",
    "Tl",
    "Pb",
    "Bi",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate public MD benchmark submission contract basics."
    )
    parser.add_argument("--submission-dir", required=True)
    parser.add_argument("--submission-contract", required=True)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--output-file", default="")
    parser.add_argument(
        "--skip-openmm",
        action="store_true",
        help="Skip OpenMM load, energy, and geometry checks.",
    )
    args = parser.parse_args(argv)

    result = validate_submission(
        submission_dir=Path(args.submission_dir),
        contract_file=Path(args.submission_contract),
        task_id=args.task_id,
        check_openmm=not args.skip_openmm,
    )
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output_file:
        out = Path(args.output_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0 if result["success"] else 1


def validate_submission(
    *,
    submission_dir: Path,
    contract_file: Path,
    task_id: str = "",
    check_openmm: bool = True,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    try:
        contract = json.loads(contract_file.read_text())
    except FileNotFoundError:
        return _result(
            success=False,
            task_id=task_id,
            submission_dir=submission_dir,
            contract_file=contract_file,
            failure_class="missing_contract",
            errors=[f"submission_contract.json not found: {contract_file}"],
            warnings=[],
            checks=[],
        )
    except (json.JSONDecodeError, ValueError) as exc:
        return _result(
            success=False,
            task_id=task_id,
            submission_dir=submission_dir,
            contract_file=contract_file,
            failure_class="invalid_contract",
            errors=[f"submission_contract.json invalid: {exc}"],
            warnings=[],
            checks=[],
        )

    if not task_id:
        task_id = str(contract.get("task_id") or "")

    if submission_dir.is_symlink():
        return _result(
            success=False,
            task_id=task_id,
            submission_dir=submission_dir,
            contract_file=contract_file,
            failure_class="unsafe_submission_path",
            errors=[f"submission_dir must not be a symlink: {submission_dir}"],
            warnings=warnings,
            checks=checks,
        )
    if not submission_dir.is_dir():
        return _result(
            success=False,
            task_id=task_id,
            submission_dir=submission_dir,
            contract_file=contract_file,
            failure_class="missing_submission_dir",
            errors=[f"submission_dir not found: {submission_dir}"],
            warnings=warnings,
            checks=checks,
        )

    required_outputs = [
        str(rel)
        for rel in contract.get("required_outputs", [])
        if isinstance(rel, str)
    ]
    strict_raw_allowlist = contract.get("primary_score") == "preparation"
    traversal = _scan_submission_paths(
        submission_dir,
        set(required_outputs) if strict_raw_allowlist else None,
    )
    checks.append({
        "name": "submission_paths_stay_inside_submission",
        "passed": not traversal,
    })
    if traversal:
        return _result(
            success=False,
            task_id=task_id,
            submission_dir=submission_dir,
            contract_file=contract_file,
            failure_class="unsafe_submission_path",
            errors=traversal,
            warnings=warnings,
            checks=checks,
        )

    invalid_paths = [
        rel for rel in required_outputs if _invalid_relative_path_reason(rel)
    ]
    if invalid_paths:
        errors.extend(f"invalid required output path in contract: {rel}" for rel in invalid_paths)
    checks.append({
        "name": "required_output_paths_are_relative",
        "passed": not invalid_paths,
        "count": len(required_outputs),
    })

    missing: list[str] = []
    empty: list[str] = []
    for rel in required_outputs:
        if _invalid_relative_path_reason(rel):
            continue
        path = submission_dir / rel
        if not path.is_file():
            missing.append(rel)
        elif path.stat().st_size <= 0:
            empty.append(rel)
    if missing:
        errors.append(f"missing required output(s): {missing}")
    if empty:
        errors.append(f"empty required output file(s): {empty}")
    checks.append({
        "name": "required_outputs_exist",
        "passed": not missing and not empty,
        "missing": missing,
        "empty": empty,
    })

    manifest_result = _validate_manifest_contract(
        submission_dir=submission_dir,
        contract=contract,
        task_id=task_id,
    )
    checks.append(manifest_result)
    errors.extend(manifest_result.get("errors") or [])

    has_openmm_contract = all(rel in required_outputs for rel in OPENMM_TRIPLE)
    if has_openmm_contract:
        openmm_result = _validate_openmm_bundle(
            submission_dir=submission_dir,
            check_openmm=check_openmm,
        )
        checks.append(openmm_result)
        warnings.extend(openmm_result.get("warnings") or [])
        if not openmm_result.get("passed"):
            errors.extend(openmm_result.get("errors") or [])

    failure_class = _failure_class(errors)
    return _result(
        success=not errors,
        task_id=task_id,
        submission_dir=submission_dir,
        contract_file=contract_file,
        failure_class=failure_class,
        errors=errors,
        warnings=warnings,
        checks=checks,
    )


def _validate_manifest_contract(
    *,
    submission_dir: Path,
    contract: dict[str, Any],
    task_id: str,
) -> dict[str, Any]:
    """Validate completed manifest fields declared by the public contract."""
    rules = contract.get("manifest_contract")
    if not isinstance(rules, dict):
        return {
            "name": "completed_manifest_contract",
            "passed": True,
            "skipped": True,
            "errors": [],
        }

    errors: list[str] = []
    manifest_path = submission_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except FileNotFoundError:
        return {
            "name": "completed_manifest_contract",
            "passed": False,
            "skipped": False,
            "errors": ["missing required output manifest.json"],
        }
    except (json.JSONDecodeError, ValueError) as exc:
        return {
            "name": "completed_manifest_contract",
            "passed": False,
            "skipped": False,
            "errors": [f"manifest.json invalid: {exc}"],
        }
    if not isinstance(manifest, dict):
        return {
            "name": "completed_manifest_contract",
            "passed": False,
            "skipped": False,
            "errors": ["manifest.json must contain a JSON object"],
        }

    manifest_task_id = manifest.get("task_id")
    if manifest_task_id != task_id:
        errors.append(
            f"manifest.task_id={manifest_task_id!r} differs from {task_id!r}"
        )
    status = manifest.get("status", "completed")
    allowed_statuses = rules.get("allowed_statuses") or []
    if allowed_statuses and status not in allowed_statuses:
        errors.append(
            f"manifest.status={status!r} is not one of {allowed_statuses!r}"
        )

    completed_status = rules.get("completed_status", "completed")
    required_fields = rules.get("required_manifest_output_fields") or []
    list_fields = rules.get("required_manifest_list_fields") or {}
    v2_result: dict[str, Any] | None = None
    if status == completed_status:
        for json_path in required_fields:
            if not isinstance(json_path, str):
                errors.append(
                    f"invalid required manifest output path in contract: {json_path!r}"
                )
                continue
            value, found = _json_path_value(manifest, json_path)
            if not found or value in (None, "", []):
                errors.append(
                    f"missing required output field in manifest: {json_path}"
                )
                continue
            errors.extend(
                _manifest_artifact_path_errors(
                    submission_dir=submission_dir,
                    json_path=json_path,
                    value=value,
                )
            )

        if not isinstance(list_fields, dict):
            errors.append("required_manifest_list_fields must be a JSON object")
        else:
            for json_path, raw_min_count in list_fields.items():
                if not isinstance(json_path, str):
                    errors.append(
                        "invalid required manifest list path in contract: "
                        f"{json_path!r}"
                    )
                    continue
                try:
                    min_count = int(raw_min_count)
                except (TypeError, ValueError):
                    errors.append(
                        f"invalid minimum list size for {json_path}: {raw_min_count!r}"
                    )
                    continue
                value, found = _json_path_value(manifest, json_path)
                if not found or not isinstance(value, list) or len(value) < min_count:
                    errors.append(
                        f"missing required output list in manifest: {json_path} "
                        f"needs at least {min_count} item(s)"
                    )

        if contract.get("evaluation_protocol") == "grounded_correct_v2":
            v2_result = _grounded_v2_public_checks(
                submission_dir=submission_dir,
                manifest=manifest,
                contract=contract,
                task_id=task_id,
            )
            errors.extend(v2_result.get("errors") or [])
        else:
            v2_result = None

    result = {
        "name": "completed_manifest_contract",
        "passed": not errors,
        "skipped": False,
        "status": status,
        "errors": errors,
    }
    if status == completed_status and v2_result is not None:
        result["v2_truth_blind_checks"] = v2_result
    return result


def _grounded_v2_public_checks(
    *,
    submission_dir: Path,
    manifest: dict[str, Any],
    contract: dict[str, Any],
    task_id: str,
) -> dict[str, Any]:
    """Validate the runner-finalized plan/claim/episode envelope.

    The public preflight deliberately does not duplicate the OpenMM replay
    scorer. It verifies portable paths, hashes, and the two small agent
    objects; official scoring performs runtime and scientific checks.
    """

    errors: list[str] = []
    generated_by = manifest.get("generated_by")
    if not isinstance(generated_by, dict) or (
        generated_by.get("tool") != "mdclaw_benchmark_runner"
    ):
        errors.append("v2 manifest must be generated by mdclaw_benchmark_runner")

    outputs = manifest.get("outputs")
    if outputs != _V2_MANIFEST_OUTPUTS:
        errors.append(
            "v2 manifest.outputs must exactly declare the released "
            "confirmatory_plan, claim, and episode paths"
        )

    payloads: dict[str, dict[str, Any]] = {}
    paths: dict[str, str] = {}
    for field in ("confirmatory_plan", "claim", "episode"):
        relative, found = _json_path_value(manifest, f"outputs.{field}")
        if not found or not isinstance(relative, str) or not relative.strip():
            errors.append(f"missing required output field: outputs.{field}")
            continue
        reason = _invalid_relative_path_reason(relative)
        if reason:
            errors.append(f"invalid outputs.{field} path {relative!r}: {reason}")
            continue
        try:
            payload = json.loads((submission_dir / relative).read_text())
        except FileNotFoundError:
            errors.append(f"outputs.{field} file not found: {relative}")
            continue
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"outputs.{field} is not valid JSON: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"outputs.{field} must contain a JSON object")
            continue
        payloads[field] = payload
        paths[field] = relative
    if len(payloads) != 3:
        return {"passed": False, "errors": errors}

    plan = payloads["confirmatory_plan"]
    claim = payloads["claim"]
    episode = payloads["episode"]
    for label, payload in payloads.items():
        if payload.get("task_id") != task_id:
            errors.append(
                f"{label}.task_id={payload.get('task_id')!r} differs from "
                f"{task_id!r}"
            )

    errors.extend(
        _v2_field_errors(
            "confirmatory_plan",
            plan,
            allowed=_V2_PLAN_FIELDS,
            required=_V2_PLAN_REQUIRED_FIELDS,
        )
    )
    if plan.get("schema_version", "1.0") != "1.0":
        errors.append("confirmatory_plan.schema_version must be '1.0'")
    if not _nonempty_string(plan.get("task_id")):
        errors.append("confirmatory_plan.task_id must be a non-empty string")

    runs = plan.get("runs")
    if not isinstance(runs, list) or not runs:
        errors.append("confirmatory_plan.runs must be a non-empty list")
        runs = []
    roles: set[str] = set()
    run_ids: set[str] = set()
    nodes: set[tuple[str, str]] = set()
    plan_runs: dict[str, dict[str, Any]] = {}
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            errors.append(f"confirmatory_plan.runs[{index}] must be an object")
            continue
        prefix = f"confirmatory_plan.runs[{index}]"
        errors.extend(
            _v2_field_errors(
                prefix,
                run,
                allowed=_V2_RUN_FIELDS,
                required=_V2_RUN_FIELDS,
            )
        )
        role = run.get("condition_role")
        if role not in {"reference", "variant"}:
            errors.append(f"{prefix}.condition_role is invalid")
        else:
            roles.add(role)
        run_id = run.get("run_id")
        if not _nonempty_string(run_id):
            errors.append(f"{prefix}.run_id must be a non-empty string")
        elif run_id in run_ids:
            errors.append(f"duplicate confirmatory run_id: {run_id!r}")
        else:
            run_ids.add(run_id)
            plan_runs[run_id] = run
        job_dir = run.get("job_dir")
        node_id = run.get("node_id")
        if not _nonempty_string(job_dir):
            errors.append(f"{prefix}.job_dir must be a non-empty string")
        else:
            reason = _invalid_relative_path_reason(job_dir)
            if reason:
                errors.append(f"{prefix}.job_dir is invalid: {reason}")
        if not _nonempty_string(node_id):
            errors.append(f"{prefix}.node_id must be a non-empty string")
        elif (
            PurePosixPath(node_id).name != node_id
            or "/" in node_id
            or "\\" in node_id
        ):
            errors.append(f"{prefix}.node_id is unsafe")
        if _nonempty_string(job_dir) and _nonempty_string(node_id):
            node = (job_dir, node_id)
            if node in nodes:
                errors.append(
                    f"duplicate confirmatory job/node request: {node!r}"
                )
            else:
                nodes.add(node)
        duration = run.get("simulation_time_ns")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) <= 0.0
        ):
            errors.append(
                f"confirmatory_plan.runs[{index}].simulation_time_ns "
                "must be finite and positive"
            )
    if roles != {"reference", "variant"}:
        errors.append(
            "confirmatory_plan requires reference and variant runs"
        )

    errors.extend(
        _v2_field_errors(
            "claim",
            claim,
            allowed=_V2_CLAIM_FIELDS,
            required=_V2_CLAIM_REQUIRED_FIELDS,
        )
    )
    if claim.get("schema_version", "1.0") != "1.0":
        errors.append("claim.schema_version must be '1.0'")
    if not _nonempty_string(claim.get("task_id")):
        errors.append("claim.task_id must be a non-empty string")
    status = claim.get("status")
    outcome = claim.get("outcome")
    allowed_outcomes = (
        contract.get("scientific_target", {}).get("allowed_outcomes", [])
        if isinstance(contract.get("scientific_target"), dict)
        else []
    )
    if status == "resolved":
        if not _nonempty_string(outcome):
            errors.append("resolved claim requires a non-empty string outcome")
        elif outcome not in allowed_outcomes:
            errors.append(
                f"claim.outcome must be one of {sorted(allowed_outcomes)}"
            )
    elif status == "unresolved":
        if outcome is not None:
            errors.append("unresolved claim requires outcome=null")
    else:
        errors.append("claim.status must be resolved or unresolved")
    if "outcome" in claim and outcome is not None and not isinstance(outcome, str):
        errors.append("claim.outcome must be a string or null")

    if episode.get("schema_version") != "1.0":
        errors.append("episode.schema_version must be '1.0'")
    if episode.get("kind") != "mdstudybench_runner_episode_v2":
        errors.append("episode.kind is not the released runner episode")
    if episode.get("recorded_by") != "mdclaw_benchmark_runner":
        errors.append("episode is not runner-authored")
    if episode.get("success") is not True:
        errors.append("episode.success must be true")
    if episode.get("within_task_budget") is not True:
        errors.append("episode.within_task_budget must be true")
    if episode.get("errors") != []:
        errors.append("episode.errors must be an empty list")
    plan_hash = episode.get("plan_sha256")
    if not _sha256_value(plan_hash):
        errors.append("episode.plan_sha256 must be a SHA-256 digest")
    else:
        observed = _sha256_file(submission_dir / paths["confirmatory_plan"])
        if observed != plan_hash:
            errors.append("confirmatory plan hash differs from episode")

    episode_root = (submission_dir / paths["episode"]).parent
    events = episode.get("events")
    if not isinstance(events, list) or not events:
        errors.append("episode.events must be a non-empty list")
        events = []
    event_run_ids: set[str] = set()
    for event_index, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append(f"episode.events[{event_index}] must be an object")
            continue
        prefix = f"episode.events[{event_index}]"
        run_id = event.get("run_id")
        if not _nonempty_string(run_id):
            errors.append(f"{prefix}.run_id must be a non-empty string")
        elif run_id not in plan_runs:
            errors.append(f"{prefix}.run_id is not declared by the plan")
        elif run_id in event_run_ids:
            errors.append(f"{prefix}.run_id is duplicated")
        else:
            event_run_ids.add(run_id)
            plan_run = plan_runs[run_id]
            for field in ("condition_role", "node_id"):
                if event.get(field) != plan_run.get(field):
                    errors.append(f"{prefix}.{field} differs from the plan")
        if event.get("plan_sha256") != plan_hash:
            errors.append(f"{prefix} plan hash mismatch")
        for group, required in (
            ("input_artifacts", _V2_INPUT_ARTIFACTS),
            ("output_artifacts", _V2_OUTPUT_ARTIFACTS),
        ):
            records = event.get(group)
            if not isinstance(records, dict):
                errors.append(f"{prefix}.{group} must be an object")
                continue
            if set(records) != required:
                errors.append(
                    f"{prefix}.{group} must contain exactly "
                    f"{sorted(required)!r}"
                )
            for name in sorted(required):
                record = records.get(name)
                if not isinstance(record, dict):
                    errors.append(
                        f"{prefix}.{group}.{name} "
                        "must be an object"
                    )
                    continue
                if set(record) != _V2_ARTIFACT_RECORD_FIELDS:
                    errors.append(
                        f"{prefix}.{group}.{name} must contain exactly "
                        "path, sha256, and bytes"
                    )
                relative = record.get("path")
                if not isinstance(relative, str):
                    errors.append(
                        f"episode artifact path {relative!r} must be a string"
                    )
                    continue
                reason = _invalid_relative_path_reason(relative)
                if reason:
                    errors.append(
                        f"episode artifact path {relative!r} is invalid: {reason}"
                    )
                    continue
                artifact = episode_root / str(relative)
                if not artifact.is_file():
                    errors.append(f"episode artifact not found: {relative}")
                    continue
                digest = record.get("sha256")
                if not _sha256_value(digest):
                    errors.append(
                        f"episode artifact SHA-256 is invalid: {relative}"
                    )
                elif _sha256_file(artifact) != digest:
                    errors.append(f"episode artifact hash mismatch: {relative}")
                size = record.get("bytes")
                if (
                    isinstance(size, bool)
                    or not isinstance(size, int)
                    or size < 0
                ):
                    errors.append(
                        f"episode artifact bytes is invalid: {relative}"
                    )
                elif artifact.stat().st_size != size:
                    errors.append(f"episode artifact size mismatch: {relative}")

    if set(plan_runs) != event_run_ids:
        errors.append("episode events must cover every confirmatory plan run once")

    return {"passed": not errors, "errors": errors}


def _v2_field_errors(
    label: str,
    payload: dict[str, Any],
    *,
    allowed: set[str],
    required: set[str],
) -> list[str]:
    errors: list[str] = []
    missing = sorted(required - set(payload))
    extra = sorted(set(payload) - allowed)
    if missing:
        errors.append(f"{label} is missing required field(s): {missing!r}")
    if extra:
        errors.append(f"{label} has unexpected field(s): {extra!r}")
    return errors


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _sha256_value(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_path_value(payload: Any, json_path: str) -> tuple[Any, bool]:
    current = payload
    for part in json_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None, False
        current = current[part]
    return current, True


def _manifest_artifact_path_errors(
    *,
    submission_dir: Path,
    json_path: str,
    value: Any,
) -> list[str]:
    paths = value if isinstance(value, list) else [value]
    errors: list[str] = []
    for item in paths:
        if not isinstance(item, str):
            errors.append(
                f"manifest output {json_path} contains a non-path item: {item!r}"
            )
            continue
        reason = _invalid_relative_path_reason(item)
        if reason:
            errors.append(f"invalid manifest output path {json_path}: {item!r} ({reason})")
            continue
        path = submission_dir / item
        if not path.is_file():
            errors.append(f"manifest output file not found for {json_path}: {item}")
        elif path.stat().st_size <= 0:
            errors.append(f"manifest output file is empty for {json_path}: {item}")
    return errors


def _result(
    *,
    success: bool,
    task_id: str,
    submission_dir: Path,
    contract_file: Path,
    failure_class: str | None,
    errors: list[str],
    warnings: list[str],
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "task_id": task_id,
        "submission_dir": str(submission_dir),
        "submission_contract": str(contract_file),
        "success": success,
        "contract_status": "complete" if success else "failed",
        "failure_class": failure_class,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
    }


def _invalid_relative_path_reason(rel: str) -> str:
    rel = rel.strip()
    if not rel:
        return "empty path"
    path = PurePosixPath(rel)
    if path.is_absolute():
        return "absolute path"
    if any(part in {"", ".", ".."} for part in path.parts):
        return "path traversal or empty component"
    return ""


def _scan_submission_paths(
    submission_dir: Path,
    allowed_outputs: set[str] | None,
) -> list[str]:
    errors: list[str] = []
    root = submission_dir.resolve()
    for path in submission_dir.rglob("*"):
        if path.is_symlink():
            errors.append(f"submission path must not be a symlink: {path}")
            continue
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            errors.append(f"cannot resolve submission path {path}: {exc}")
            continue
        if root != resolved and root not in resolved.parents:
            errors.append(f"submission path escapes submission_dir: {path}")
            continue
        if path.is_file() and allowed_outputs is not None:
            relative = path.relative_to(submission_dir).as_posix()
            if relative not in allowed_outputs:
                errors.append(
                    f"unexpected file outside public raw contract: {relative}"
                )
    return errors


def _nonbonded_force(system: Any) -> Any:
    try:
        from openmm import NonbondedForce
    except Exception:  # noqa: BLE001
        return None
    for index in range(system.getNumForces()):
        force = system.getForce(index)
        if isinstance(force, NonbondedForce):
            return force
    return None


def _particle_parameter_rows(
    system: Any,
) -> list[dict[str, float | bool]] | None:
    nonbonded = _nonbonded_force(system)
    if nonbonded is None:
        return None
    try:
        from openmm import unit
    except Exception:  # noqa: BLE001
        return None

    rows: list[dict[str, float | bool]] = []
    for index in range(system.getNumParticles()):
        if index >= nonbonded.getNumParticles():
            return None
        _charge, sigma, epsilon = nonbonded.getParticleParameters(index)
        try:
            is_virtual = bool(system.isVirtualSite(index))
        except Exception:  # noqa: BLE001
            is_virtual = False
        rows.append(
            {
                "sigma": float(sigma.value_in_unit(unit.nanometer)),
                "epsilon": float(epsilon.value_in_unit(unit.kilojoule_per_mole)),
                "is_virtual": is_virtual,
            }
        )
    return rows


def _nonbonded_exception_pairs(system: Any) -> set[tuple[int, int]]:
    nonbonded = _nonbonded_force(system)
    pairs: set[tuple[int, int]] = set()
    if nonbonded is None:
        return pairs
    for index in range(nonbonded.getNumExceptions()):
        p1, p2, *_rest = nonbonded.getExceptionParameters(index)
        a, b = int(p1), int(p2)
        pairs.add((a, b) if a < b else (b, a))
    return pairs


def _monoatomic_metal_ion_indices(topology: Any) -> set[int]:
    indices: set[int] = set()
    try:
        for residue in topology.residues():
            atoms = list(residue.atoms())
            if len(atoms) != 1:
                continue
            atom = atoms[0]
            symbol = getattr(getattr(atom, "element", None), "symbol", None)
            if symbol in _METAL_ELEMENTS:
                indices.add(int(atom.index))
    except Exception:  # noqa: BLE001
        return set()
    return indices


def _count_nonbonded_clashes(
    system: Any,
    coords: list[tuple[float, float, float]],
    overlap_fraction: float,
    limit: int,
    *,
    exclude_indices: set[int] | None = None,
) -> tuple[int, list[str], bool]:
    """Scan scorer-equivalent nonbonded overlaps with bounded examples."""

    rows = _particle_parameter_rows(system)
    if rows is None:
        return -1, ["NonbondedForce particle parameters unavailable"], False
    if len(rows) != len(coords):
        return (
            -1,
            [f"particle/coord count mismatch: {len(rows)} vs {len(coords)}"],
            False,
        )

    excluded = exclude_indices or set()
    sigmas = [float(row["sigma"]) for row in rows]
    epsilons = [float(row["epsilon"]) for row in rows]
    virtual = [bool(row["is_virtual"]) for row in rows]
    interacting = [
        not virtual[index]
        and sigmas[index] > 0.0
        and epsilons[index] > 0.0
        and index not in excluded
        for index in range(len(rows))
    ]
    max_sigma = max(
        (sigmas[index] for index in range(len(rows)) if interacting[index]),
        default=0.0,
    )
    cell = overlap_fraction * max_sigma * _TWO_TO_ONE_SIXTH
    if cell <= 0.0:
        return 0, [], False

    exceptions = _nonbonded_exception_pairs(system)
    grid: dict[tuple[int, int, int], list[int]] = {}
    inverse_cell = 1.0 / cell
    for index, (x, y, z) in enumerate(coords):
        if not interacting[index]:
            continue
        key = (
            int(math.floor(x * inverse_cell)),
            int(math.floor(y * inverse_cell)),
            int(math.floor(z * inverse_cell)),
        )
        grid.setdefault(key, []).append(index)

    clashes = 0
    examples: list[str] = []
    neighbor_offsets = [(dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)]
    for (cell_x, cell_y, cell_z), members in grid.items():
        for dx, dy, dz in neighbor_offsets:
            neighbors = grid.get((cell_x + dx, cell_y + dy, cell_z + dz))
            if not neighbors:
                continue
            for first in members:
                for second in neighbors:
                    if second <= first or (first, second) in exceptions:
                        continue
                    x1, y1, z1 = coords[first]
                    x2, y2, z2 = coords[second]
                    distance_squared = (x1 - x2) ** 2 + (y1 - y2) ** 2 + (z1 - z2) ** 2
                    r_min = (sigmas[first] + sigmas[second]) * 0.5 * _TWO_TO_ONE_SIXTH
                    threshold = overlap_fraction * r_min
                    if distance_squared >= threshold * threshold:
                        continue
                    clashes += 1
                    if len(examples) < 5:
                        examples.append(
                            f"{first}-{second} at "
                            f"{math.sqrt(distance_squared) * 10:.2f} A "
                            f"(< {threshold * 10:.2f} A)"
                        )
                    if clashes > limit + 1:
                        return clashes, examples, True
    return clashes, examples, False


def _single_point_energy_kj_mol(
    system: Any,
    state: Any,
    positions: Any,
) -> tuple[float | None, str | None, str | None]:
    try:
        from openmm import Context, Platform, VerletIntegrator, unit
    except Exception as exc:  # noqa: BLE001
        return None, None, f"OpenMM import failed: {type(exc).__name__}: {exc}"

    try:
        box_vectors = state.getPeriodicBoxVectors()
    except Exception:  # noqa: BLE001
        box_vectors = None

    failures: list[str] = []
    for platform_name in ("CPU", "Reference"):
        context = None
        integrator = None
        try:
            platform = Platform.getPlatformByName(platform_name)
            integrator = VerletIntegrator(0.001 * unit.picoseconds)
            context = Context(system, integrator, platform)
            if box_vectors is not None:
                try:
                    context.setPeriodicBoxVectors(*box_vectors)
                except Exception:  # noqa: BLE001
                    pass
            context.setPositions(positions)
            energy = context.getState(getEnergy=True).getPotentialEnergy()
            value = energy.value_in_unit(unit.kilojoule_per_mole)
            return float(value), platform_name, None
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{platform_name}: {type(exc).__name__}: {exc}")
        finally:
            if context is not None:
                del context
            if integrator is not None:
                del integrator
    return None, None, "; ".join(failures)


def _validate_openmm_bundle(
    *,
    submission_dir: Path,
    check_openmm: bool,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not check_openmm:
        return {
            "name": "openmm_bundle_loads",
            "passed": True,
            "skipped": True,
            "warnings": ["OpenMM validation skipped by --skip-openmm"],
            "errors": [],
        }

    system_xml = submission_dir / "topology" / "system.xml"
    topology_pdb = submission_dir / "topology" / "topology.pdb"
    state_xml = submission_dir / "topology" / "state.xml"
    try:
        from openmm import System, State, XmlSerializer, unit
        from openmm.app import PDBFile
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "openmm_bundle_loads",
            "passed": False,
            "skipped": False,
            "warnings": [],
            "errors": [f"OpenMM import failed: {type(exc).__name__}: {exc}"],
        }

    system = None
    state = None
    pdb = None
    positions = None
    positions_are_finite: bool | None = None
    state_position_count = None
    energy_kj_mol = None
    energy_platform = None
    energy_is_finite: bool | None = None
    abs_energy_per_particle_kj_mol = None
    clash_count = None
    clash_examples: list[str] = []
    clash_scan_stopped_early = False

    try:
        system = XmlSerializer.deserialize(system_xml.read_text())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"topology/system.xml is not a valid OpenMM System XML: {exc}")
    try:
        state = XmlSerializer.deserialize(state_xml.read_text())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"topology/state.xml is not a valid OpenMM State XML: {exc}")
    try:
        pdb = PDBFile(str(topology_pdb))
        pdb_atom_count = sum(1 for _ in pdb.topology.atoms())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"topology/topology.pdb is not readable by OpenMM: {exc}")
        pdb_atom_count = None

    particle_count = None
    if isinstance(system, System):
        particle_count = int(system.getNumParticles())
        if particle_count <= 0:
            errors.append("OpenMM System has no particles")
    elif system is not None:
        errors.append("topology/system.xml did not contain an OpenMM System")

    if state is not None and not isinstance(state, State):
        errors.append("topology/state.xml did not contain an OpenMM State")

    if particle_count is not None and pdb_atom_count is not None:
        if particle_count != pdb_atom_count:
            errors.append(
                "OpenMM particle count differs from topology PDB atom count: "
                f"{particle_count} vs {pdb_atom_count}"
            )

    if isinstance(state, State):
        try:
            positions = state.getPositions(asNumpy=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"topology/state.xml has no readable positions: {exc}")
        else:
            if positions is None:
                errors.append("topology/state.xml does not contain positions")
            else:
                values = positions.value_in_unit(unit.nanometer)
                state_position_count = len(values)
                positions_are_finite = all(
                    math.isfinite(float(component)) for row in values for component in row
                )
                if not positions_are_finite:
                    errors.append("topology/state.xml contains non-finite positions")

    if particle_count is not None and state_position_count is not None:
        if particle_count != state_position_count:
            errors.append(
                "OpenMM particle count differs from state position count: "
                f"{particle_count} vs {state_position_count}"
            )

    bundle_loaded_cleanly = not errors
    if bundle_loaded_cleanly:
        assert system is not None
        assert state is not None
        assert pdb is not None
        assert positions is not None
        assert particle_count is not None

        energy_kj_mol, energy_platform, energy_error = _single_point_energy_kj_mol(
            system,
            state,
            positions,
        )
        if energy_error:
            errors.append(f"OpenMM single-point energy evaluation failed: {energy_error}")
        elif energy_kj_mol is not None:
            energy_is_finite = math.isfinite(energy_kj_mol)
            if not energy_is_finite:
                errors.append("OpenMM single-point potential energy is not finite")
            else:
                abs_energy_per_particle_kj_mol = abs(energy_kj_mol) / particle_count
                if abs_energy_per_particle_kj_mol > _MAX_ABS_ENERGY_PER_PARTICLE_KJ_MOL:
                    errors.append(
                        "OpenMM single-point potential energy is physically "
                        f"implausible: {energy_kj_mol:.6g} kJ/mol "
                        f"({abs_energy_per_particle_kj_mol:.6g} "
                        "kJ/mol/particle)"
                    )

        coords = [
            (float(row[0]), float(row[1]), float(row[2]))
            for row in positions.value_in_unit(unit.nanometer)
        ]
        metal_indices = _monoatomic_metal_ion_indices(pdb.topology)
        clash_count, clash_examples, clash_scan_stopped_early = _count_nonbonded_clashes(
            system,
            coords,
            _CLASH_OVERLAP_FRACTION,
            _MAX_CLASHES,
            exclude_indices=metal_indices,
        )
        if clash_count < 0:
            errors.append(f"OpenMM steric clash scan failed: {clash_examples}")
        elif clash_count > _MAX_CLASHES:
            qualifier = "at least " if clash_scan_stopped_early else ""
            errors.append(
                f"OpenMM state contains {qualifier}{clash_count} steric clash(es) "
                f"> {_MAX_CLASHES} (e.g. {clash_examples})"
            )

    return {
        "name": "openmm_bundle_loads",
        "passed": not errors,
        "skipped": False,
        "particle_count": particle_count,
        "pdb_atom_count": pdb_atom_count,
        "state_position_count": state_position_count,
        "positions_are_finite": positions_are_finite,
        "energy_kj_mol": energy_kj_mol,
        "energy_platform": energy_platform,
        "energy_is_finite": energy_is_finite,
        "abs_energy_per_particle_kj_mol": abs_energy_per_particle_kj_mol,
        "max_abs_energy_per_particle_kj_mol": (_MAX_ABS_ENERGY_PER_PARTICLE_KJ_MOL),
        "clash_count": clash_count,
        "clash_examples": clash_examples,
        "clash_scan_stopped_early": clash_scan_stopped_early,
        "clash_overlap_fraction": _CLASH_OVERLAP_FRACTION,
        "max_clashes": _MAX_CLASHES,
        "warnings": warnings,
        "errors": errors,
    }


def _failure_class(errors: list[str]) -> str | None:
    if not errors:
        return None
    joined = "\n".join(errors).lower()
    if "missing required output" in joined or "not found" in joined:
        return "missing_raw_artifacts"
    if "openmm" in joined or "topology/" in joined:
        return "invalid_openmm_bundle"
    if "escapes submission_dir" in joined or "invalid required output path" in joined:
        return "invalid_submission_path"
    return "contract_violation"


if __name__ == "__main__":
    raise SystemExit(main())
