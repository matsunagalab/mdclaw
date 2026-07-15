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
