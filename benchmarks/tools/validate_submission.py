#!/usr/bin/env python3
"""Tool-neutral public preflight for MDPrepBench submissions.

This script intentionally uses only the public ``submission_contract.json`` and
the solver's ``submission/`` directory. It does not read private task metadata
or hidden truth files, so it is safe to ship in the public benchmark package.
"""

from __future__ import annotations

import argparse
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
        help="Skip OpenMM deserialize/finite-position checks.",
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
    except json.JSONDecodeError as exc:
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

    traversal = _scan_submission_paths(submission_dir)
    if traversal:
        errors.extend(traversal)
    checks.append({
        "name": "submission_paths_stay_inside_submission",
        "passed": not traversal,
    })

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

    generated_present = [
        rel
        for rel in (
            "manifest.json",
            "metrics.json",
            "provenance.json",
            "minimized_structure.pdb",
            "minimization_report.json",
        )
        if (submission_dir / rel).exists() and rel not in required_outputs
    ]
    if generated_present:
        warnings.append(
            "evaluator-generated files are present and will be ignored or "
            f"regenerated for MDPrepBench preparation tasks: {generated_present}"
        )

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


def _scan_submission_paths(submission_dir: Path) -> list[str]:
    errors: list[str] = []
    root = submission_dir.resolve()
    for path in submission_dir.rglob("*"):
        try:
            resolved = path.resolve()
        except OSError as exc:
            errors.append(f"cannot resolve submission path {path}: {exc}")
            continue
        if root != resolved and root not in resolved.parents:
            errors.append(f"submission path escapes submission_dir: {path}")
    return errors


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

    try:
        system = XmlSerializer.deserialize(system_xml.read_text())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"topology/system.xml is not a valid OpenMM System XML: {exc}")
        system = None
    try:
        state = XmlSerializer.deserialize(state_xml.read_text())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"topology/state.xml is not a valid OpenMM State XML: {exc}")
        state = None
    try:
        pdb = PDBFile(str(topology_pdb))
        pdb_atom_count = sum(1 for _ in pdb.topology.atoms())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"topology/topology.pdb is not readable by OpenMM: {exc}")
        pdb_atom_count = None

    particle_count = None
    if isinstance(system, System):
        particle_count = int(system.getNumParticles())
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
                if not all(
                    math.isfinite(float(component))
                    for row in values
                    for component in row
                ):
                    errors.append("topology/state.xml contains non-finite positions")

    return {
        "name": "openmm_bundle_loads",
        "passed": not errors,
        "skipped": False,
        "particle_count": particle_count,
        "pdb_atom_count": pdb_atom_count,
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
