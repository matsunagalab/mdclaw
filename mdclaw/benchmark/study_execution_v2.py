"""Runner-owned OpenMM execution certificates for MDStudyBench v2.

The generic stage wrapper is useful provenance, but it cannot prove that a
trajectory came from MD: the evaluated agent can invoke any command and can
write its own JSON.  This module defines the narrower S01 pilot boundary.
Only the benchmark runner may execute a frozen confirmatory MDClaw production
request and attach the resulting ledger to ``harness_execution.json``.

No held-out scientific answer is accepted anywhere in this module.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any


RUNNER_EXECUTION_KIND = "mdstudybench_runner_execution_v2"
MDCLAW_OPENMM_ADAPTER = "mdclaw_openmm@1"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_directory(path: str | Path) -> str:
    root = Path(path)
    if not root.is_dir():
        return ""
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def verify_runner_execution_v2(
    *,
    submission_dir: str | Path,
    scientific_target: dict[str, Any],
    study_index: dict[str, Any],
    analysis_intent_file: str | Path,
    harness_record: Any,
) -> dict[str, Any]:
    """Verify a scorer-side ledger against the submitted study artifacts."""

    root = Path(submission_dir).resolve()
    errors: list[dict[str, str]] = []
    ledger = (
        harness_record.get("study_execution")
        if isinstance(harness_record, dict)
        else None
    )
    if not isinstance(ledger, dict):
        return _execution_certificate(
            ledger=None,
            errors=[
                {
                    "code": "runner_execution_ledger_missing",
                    "message": (
                        "official v2 scoring requires a runner-owned "
                        "confirmatory execution ledger"
                    ),
                }
            ],
        )

    if ledger.get("kind") != RUNNER_EXECUTION_KIND:
        _issue(
            errors,
            "runner_execution_kind_invalid",
            "study_execution.kind is not the released runner ledger kind",
        )
    if ledger.get("task_id") != study_index.get("task_id"):
        _issue(
            errors,
            "runner_execution_task_mismatch",
            "runner ledger task_id does not match study_index.task_id",
        )
    if isinstance(harness_record, dict) and (
        ledger.get("run_id") != harness_record.get("run_id")
    ):
        _issue(
            errors,
            "runner_execution_run_mismatch",
            "runner ledger run_id does not match its harness record",
        )
    expected_adapter = scientific_target.get("execution_adapter")
    if (
        not isinstance(expected_adapter, str)
        or ledger.get("adapter_id") != expected_adapter
    ):
        _issue(
            errors,
            "execution_adapter_mismatch",
            "runner ledger adapter does not match the public task contract",
        )
    if ledger.get("recorded_by") != "mdclaw_benchmark_runner":
        _issue(
            errors,
            "runner_custody_missing",
            "execution ledger is not marked as benchmark-runner owned",
        )
    launcher = ledger.get("adapter_launcher")
    if not isinstance(launcher, dict):
        _issue(
            errors,
            "runner_adapter_launcher_missing",
            "runner ledger lacks the private adapter launcher digest",
        )
    else:
        launcher_path = launcher.get("path")
        launcher_sha256 = launcher.get("sha256")
        if (
            not isinstance(launcher_path, str)
            or not Path(launcher_path).is_absolute()
            or not Path(launcher_path).is_file()
            or not isinstance(launcher_sha256, str)
            or sha256_file(launcher_path) != launcher_sha256
        ):
            _issue(
                errors,
                "runner_adapter_launcher_hash_mismatch",
                "private runner adapter launcher is missing or changed",
            )
    adapter_source = ledger.get("adapter_source")
    if not isinstance(adapter_source, dict):
        _issue(
            errors,
            "runner_adapter_source_missing",
            "runner ledger lacks the frozen adapter-source digest",
        )
    else:
        source_path = adapter_source.get("path")
        source_sha256 = adapter_source.get("sha256")
        expected_source_sha256 = adapter_source.get("expected_sha256")
        if (
            not isinstance(source_path, str)
            or not Path(source_path).is_absolute()
            or not Path(source_path).is_dir()
            or not isinstance(source_sha256, str)
            or not source_sha256
            or source_sha256 != expected_source_sha256
            or sha256_directory(source_path) != source_sha256
        ):
            _issue(
                errors,
                "runner_adapter_source_hash_mismatch",
                "runner adapter source snapshot is missing or changed",
            )
    if ledger.get("within_task_budget") is not True:
        _issue(
            errors,
            "task_budget_not_attested",
            "runner did not attest that confirmatory execution stayed in budget",
        )
    if ledger.get("success") is not True:
        _issue(
            errors,
            "runner_execution_unsuccessful",
            "runner ledger does not attest successful confirmatory execution",
        )
    ledger_failures = ledger.get("errors")
    if not isinstance(ledger_failures, list):
        _issue(
            errors,
            "runner_execution_errors_invalid",
            "runner ledger errors must be a list",
        )
    elif ledger_failures:
        _issue(
            errors,
            "runner_execution_errors_present",
            "runner ledger records confirmatory execution errors",
        )

    intent_path = _safe_file(root, analysis_intent_file)
    submitted_intent_hash = (
        sha256_file(intent_path) if intent_path is not None else None
    )
    frozen = ledger.get("frozen_intent")
    if not isinstance(frozen, dict):
        _issue(errors, "frozen_intent_missing", "runner ledger lacks frozen_intent")
        frozen = {}
    if (
        submitted_intent_hash is None
        or frozen.get("sha256") != submitted_intent_hash
    ):
        _issue(
            errors,
            "frozen_intent_hash_mismatch",
            "submitted analysis_intent.json differs from the runner-frozen bytes",
        )
    if not isinstance(frozen.get("frozen_at"), str):
        _issue(
            errors,
            "frozen_intent_time_missing",
            "runner ledger lacks a freeze timestamp",
        )

    events = ledger.get("events")
    if not isinstance(events, list):
        events = []
        _issue(
            errors,
            "runner_execution_events_missing",
            "runner ledger requires confirmatory execution events",
        )
    event_by_run: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if not isinstance(event, dict):
            _issue(
                errors,
                "runner_execution_event_invalid",
                "runner execution events must be objects",
            )
            continue
        run_id = event.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            _issue(
                errors,
                "runner_execution_run_id_missing",
                "every runner execution event requires run_id",
            )
            continue
        event_by_run.setdefault(run_id, []).append(event)

    roles = _confirmatory_runs_with_roles(study_index, errors)
    attested_runs: list[dict[str, Any]] = []
    base_system_hashes: set[str] = set()
    topology_hashes: set[str] = set()
    for run_id, run in roles.items():
        matches = event_by_run.get(run_id, [])
        if len(matches) != 1:
            _issue(
                errors,
                "runner_execution_event_not_unique",
                f"confirmatory run {run_id!r} requires exactly one runner event",
            )
            continue
        event = matches[0]
        run_errors: list[str] = []
        if event.get("valid") is not True:
            run_errors.append("runner_node_inspection_failed")
        if event.get("adapter_id") != expected_adapter:
            run_errors.append("runner_event_adapter_mismatch")
        if event.get("adapter_exit_code") != 0:
            run_errors.append("confirmatory_adapter_nonzero_exit")
        if event.get("adapter_timed_out") is not False:
            run_errors.append("confirmatory_adapter_timeout")
        if event.get("production_event_id") != run.get("production_event_id"):
            run_errors.append("production_event_id_mismatch")
        if event.get("condition_role") != run.get("condition_role"):
            run_errors.append("condition_role_mismatch")
        if event.get("intent_sha256") != submitted_intent_hash:
            run_errors.append("execution_intent_hash_mismatch")
        if not _event_started_after_freeze(event, frozen):
            run_errors.append("confirmatory_started_before_freeze")

        submitted_topology = _safe_file(root, run.get("topology"))
        submitted_trajectories = [
            path
            for relative in _run_trajectory_paths(run)
            if (path := _safe_file(root, relative)) is not None
        ]
        inputs = event.get("input_artifacts")
        outputs = event.get("output_artifacts")
        if not isinstance(inputs, dict):
            inputs = {}
        if not isinstance(outputs, dict):
            outputs = {}
        if (
            submitted_topology is None
            or inputs.get("topology", {}).get("sha256")
            != sha256_file(submitted_topology)
        ):
            run_errors.append("submitted_topology_hash_mismatch")
        topology_hash = inputs.get("topology", {}).get("sha256")
        if isinstance(topology_hash, str) and topology_hash:
            topology_hashes.add(topology_hash)
        ledger_trajectory = outputs.get("trajectory")
        if (
            len(submitted_trajectories) != 1
            or not isinstance(ledger_trajectory, dict)
            or ledger_trajectory.get("sha256")
            != sha256_file(submitted_trajectories[0])
        ):
            run_errors.append("submitted_trajectory_hash_mismatch")
        base_hash = inputs.get("base_system", {}).get("sha256")
        if isinstance(base_hash, str) and base_hash:
            base_system_hashes.add(base_hash)
        else:
            run_errors.append("base_system_hash_missing")

        if run_errors:
            for code in run_errors:
                _issue(
                    errors,
                    code,
                    f"confirmatory run {run_id!r} failed: {code}",
                )
        attested_runs.append(
            {
                "run_id": run_id,
                "production_event_id": run.get("production_event_id"),
                "condition_role": run.get("condition_role"),
                "attested": not run_errors,
                "reason_codes": run_errors,
                "runtime": (
                    event.get("runtime")
                    if isinstance(event.get("runtime"), dict)
                    else {}
                ),
            }
        )

    extra_run_ids = sorted(set(event_by_run) - set(roles))
    if extra_run_ids:
        _issue(
            errors,
            "undeclared_runner_execution_event",
            f"runner ledger contains undeclared run IDs {extra_run_ids}",
        )
    if len(base_system_hashes) > 1:
        _issue(
            errors,
            "paired_chemistry_mismatch",
            "paired pressure conditions must use the same base system.xml bytes",
        )
    if len(topology_hashes) > 1:
        _issue(
            errors,
            "paired_topology_mismatch",
            "paired pressure conditions must use the same topology.pdb bytes",
        )
    observed_roles = {
        run.get("condition_role") for run in roles.values()
    }
    if observed_roles != {"reference", "variant"}:
        _issue(
            errors,
            "paired_condition_roles_missing",
            "confirmatory study requires both reference and variant pressure runs",
        )

    return _execution_certificate(
        ledger=ledger,
        errors=errors,
        attested_runs=attested_runs,
        submitted_intent_hash=submitted_intent_hash,
    )


def inspect_mdclaw_production_node_v2(
    *,
    job_dir: str | Path,
    node_id: str,
    run_id: str,
    production_event_id: str,
    condition_role: str,
    scientific_target: dict[str, Any],
    intent_sha256: str,
    started_at: str,
    completed_at: str,
    walltime_seconds: float,
) -> dict[str, Any]:
    """Inspect one completed MDClaw prod node and return a ledger event."""

    job = Path(job_dir).resolve()
    node_dir = job / "nodes" / node_id
    errors: list[str] = []
    node = _read_json(node_dir / "node.json")
    if not node:
        errors.append("node_json_missing_or_invalid")
        return _node_event(
            run_id=run_id,
            production_event_id=production_event_id,
            condition_role=condition_role,
            intent_sha256=intent_sha256,
            started_at=started_at,
            completed_at=completed_at,
            walltime_seconds=walltime_seconds,
            job_dir=job,
            node_id=node_id,
            errors=errors,
        )
    if node.get("status") != "completed":
        errors.append("production_node_not_completed")
    if (node.get("node_type") or node.get("type")) != "prod":
        errors.append("production_node_type_mismatch")
    if not _has_completed_ancestor(job, node_id, "eq"):
        errors.append("completed_equilibration_ancestor_missing")

    from mdclaw.node.inputs import resolve_node_inputs

    resolved = resolve_node_inputs(str(job), node_id, "prod")
    if resolved.get("input_resolution_error"):
        errors.append("production_inputs_unresolved")
    input_paths = {
        "base_system": _existing_path(resolved.get("system_xml_file")),
        "topology": _existing_path(resolved.get("topology_pdb_file")),
        "start_state": _existing_path(
            resolved.get("restart_from") or resolved.get("state_xml_file")
        ),
    }
    artifacts = node.get("artifacts")
    if not isinstance(artifacts, dict):
        artifacts = {}
        errors.append("production_node_artifacts_missing")
    output_paths = {
        key: _node_artifact(node_dir, artifacts.get(key))
        for key in (
            "trajectory",
            "state",
            "energy",
            "runtime_system",
            "integrator",
        )
    }
    for key, path in input_paths.items():
        if path is None:
            errors.append(f"{key}_missing")
    for key, path in output_paths.items():
        if path is None:
            errors.append(f"{key}_missing")

    metadata = node.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        errors.append("production_metadata_missing")
    if metadata.get("custom_force"):
        errors.append("biased_confirmatory_production")

    runtime_facts: dict[str, Any] = {}
    if all(input_paths.values()) and all(output_paths.values()):
        try:
            runtime_facts, runtime_errors = _inspect_openmm_artifacts(
                input_paths=input_paths,
                output_paths=output_paths,
                metadata=metadata,
                condition_role=condition_role,
                scientific_target=scientific_target,
            )
        except Exception:  # noqa: BLE001 -- artifact trust boundary is fail-closed
            runtime_errors = ["openmm_artifact_inspection_failed"]
        errors.extend(runtime_errors)

    artifact_hashes = metadata.get("artifact_sha256")
    if not isinstance(artifact_hashes, dict):
        artifact_hashes = {}
        errors.append("node_artifact_hashes_missing")
    for key, path in output_paths.items():
        if path is not None and artifact_hashes.get(key) != sha256_file(path):
            errors.append(f"{key}_node_hash_mismatch")

    return _node_event(
        run_id=run_id,
        production_event_id=production_event_id,
        condition_role=condition_role,
        intent_sha256=intent_sha256,
        started_at=started_at,
        completed_at=completed_at,
        walltime_seconds=walltime_seconds,
        job_dir=job,
        node_id=node_id,
        errors=errors,
        input_paths=input_paths,
        output_paths=output_paths,
        runtime_facts=runtime_facts,
    )


def _inspect_openmm_artifacts(
    *,
    input_paths: dict[str, Path | None],
    output_paths: dict[str, Path | None],
    metadata: dict[str, Any],
    condition_role: str,
    scientific_target: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    import mdtraj as md
    import numpy as np
    from openmm import (
        HarmonicAngleForce,
        HarmonicBondForce,
        LangevinMiddleIntegrator,
        MonteCarloBarostat,
        NonbondedForce,
        PeriodicTorsionForce,
        XmlSerializer,
    )
    from openmm.app import PDBFile
    from openmm.unit import (
        bar,
        femtoseconds,
        kelvin,
        nanometer,
        picosecond,
    )

    errors: list[str] = []
    base_system_path = input_paths["base_system"]
    topology_path = input_paths["topology"]
    runtime_system_path = output_paths["runtime_system"]
    integrator_path = output_paths["integrator"]
    trajectory_path = output_paths["trajectory"]
    state_path = output_paths["state"]
    assert base_system_path is not None
    assert topology_path is not None
    assert runtime_system_path is not None
    assert integrator_path is not None
    assert trajectory_path is not None
    assert state_path is not None

    base_system = XmlSerializer.deserialize(base_system_path.read_text())
    runtime_system = XmlSerializer.deserialize(runtime_system_path.read_text())
    integrator = XmlSerializer.deserialize(integrator_path.read_text())
    final_state = XmlSerializer.deserialize(state_path.read_text())
    topology = PDBFile(str(topology_path)).topology
    trajectory = md.load(str(trajectory_path), top=str(topology_path))

    base_force_names = [
        type(base_system.getForce(index)).__name__
        for index in range(base_system.getNumForces())
    ]
    runtime_forces = [
        runtime_system.getForce(index)
        for index in range(runtime_system.getNumForces())
    ]
    runtime_force_names = [type(force).__name__ for force in runtime_forces]
    barostats = [
        force for force in runtime_forces if isinstance(force, MonteCarloBarostat)
    ]
    if len(barostats) != 1:
        errors.append("standard_barostat_count_mismatch")
    expected_force_names = Counter(base_force_names)
    expected_force_names["MonteCarloBarostat"] += 1
    if Counter(runtime_force_names) != expected_force_names:
        errors.append("runtime_system_differs_beyond_barostat")
    runtime_without_barostat = XmlSerializer.deserialize(
        XmlSerializer.serialize(runtime_system)
    )
    for force_index in reversed(
        range(runtime_without_barostat.getNumForces())
    ):
        if isinstance(
            runtime_without_barostat.getForce(force_index),
            MonteCarloBarostat,
        ):
            runtime_without_barostat.removeForce(force_index)
    base_system_canonical_xml = XmlSerializer.serialize(base_system)
    runtime_base_canonical_xml = XmlSerializer.serialize(
        runtime_without_barostat
    )
    if runtime_base_canonical_xml != base_system_canonical_xml:
        errors.append("runtime_system_parameters_differ_beyond_barostat")
    if any(
        name.startswith("Custom") or name == "TorchForce"
        for name in runtime_force_names
    ):
        errors.append("biased_or_custom_force_present")
    if any("Barostat" in name for name in base_force_names):
        errors.append("base_system_contains_barostat")
    if not runtime_system.usesPeriodicBoundaryConditions():
        errors.append("runtime_system_not_periodic")
    if not isinstance(integrator, LangevinMiddleIntegrator):
        errors.append("integrator_class_mismatch")
    physics_facts, physics_errors = _inspect_base_system_physics(
        base_system=base_system,
        topology=topology,
        force_types={
            "HarmonicAngleForce": HarmonicAngleForce,
            "HarmonicBondForce": HarmonicBondForce,
            "NonbondedForce": NonbondedForce,
            "PeriodicTorsionForce": PeriodicTorsionForce,
        },
    )
    errors.extend(physics_errors)

    conditions = scientific_target.get("required_conditions")
    if not isinstance(conditions, dict):
        conditions = {}
    expected_temperature = float(conditions.get("temperature_k", 300.0))
    expected_pressure_mpa = (
        conditions.get("reference_pressure_mpa")
        if condition_role == "reference"
        else conditions.get("test_pressure_mpa")
    )
    expected_pressure_bar = float(expected_pressure_mpa) * 10.0
    integrator_temperature = float(
        integrator.getTemperature().value_in_unit(kelvin)
    )
    timestep_fs = float(
        integrator.getStepSize().value_in_unit(femtoseconds)
    )
    friction_per_ps = float(
        integrator.getFriction().value_in_unit(picosecond**-1)
    )
    if not math.isfinite(timestep_fs) or not 0.0 < timestep_fs <= 4.0:
        errors.append("integrator_timestep_out_of_range")
    if not math.isfinite(friction_per_ps) or friction_per_ps <= 0.0:
        errors.append("integrator_friction_invalid")
    if not math.isclose(
        integrator_temperature,
        expected_temperature,
        rel_tol=0.0,
        abs_tol=1.0e-6,
    ):
        errors.append("integrator_temperature_mismatch")

    pressure_bar = None
    barostat_temperature = None
    barostat_frequency = None
    if len(barostats) == 1:
        pressure_bar = float(
            barostats[0].getDefaultPressure().value_in_unit(bar)
        )
        barostat_temperature = float(
            barostats[0].getDefaultTemperature().value_in_unit(kelvin)
        )
        barostat_frequency = int(barostats[0].getFrequency())
        if barostat_frequency <= 0:
            errors.append("barostat_frequency_invalid")
        if not math.isclose(
            pressure_bar,
            expected_pressure_bar,
            rel_tol=0.0,
            abs_tol=1.0e-6,
        ):
            errors.append("barostat_pressure_mismatch")
        if not math.isclose(
            barostat_temperature,
            expected_temperature,
            rel_tol=0.0,
            abs_tol=1.0e-6,
        ):
            errors.append("barostat_temperature_mismatch")

    particle_count = int(runtime_system.getNumParticles())
    topology_atom_count = int(topology.getNumAtoms())
    if particle_count != topology_atom_count or particle_count != trajectory.n_atoms:
        errors.append("particle_topology_trajectory_count_mismatch")
    try:
        state_position_count = len(final_state.getPositions())
    except Exception:  # noqa: BLE001
        state_position_count = None
    if state_position_count != particle_count:
        errors.append("final_state_position_count_mismatch")
    state_facts, state_errors = _inspect_final_state(
        final_state,
        nanometer=nanometer,
        picosecond=picosecond,
    )
    errors.extend(state_errors)
    if trajectory.n_frames < 2:
        errors.append("trajectory_has_fewer_than_two_frames")
    elif (
        not bool(np.isfinite(trajectory.xyz).all())
        or float(np.max(np.abs(trajectory.xyz[1:] - trajectory.xyz[:-1])))
        <= 1.0e-7
    ):
        errors.append("trajectory_static_or_nonfinite")
    if trajectory.unitcell_vectors is None:
        errors.append("trajectory_periodic_box_missing")

    start_step = _finite_int(metadata.get("start_step"))
    final_step = _finite_int(metadata.get("final_step"))
    if start_step is None or final_step is None or final_step <= start_step:
        errors.append("production_step_range_invalid")
    state_step = _state_step_count(final_state)
    if state_step is not None and final_step is not None and state_step != final_step:
        errors.append("final_state_step_mismatch")
    energy_summary = _energy_step_summary(output_paths["energy"])
    energy_rows = energy_summary["row_count"]
    energy_final_step = energy_summary["final_step"]
    if energy_rows < 1:
        errors.append("energy_rows_missing")
    if energy_summary["required_columns_present"] is not True:
        errors.append("energy_required_columns_missing")
    if energy_summary["finite"] is not True:
        errors.append("energy_values_nonfinite")
    if (
        energy_final_step is not None
        and final_step is not None
        and energy_final_step != final_step
    ):
        errors.append("energy_final_step_mismatch")
    signature = metadata.get("system_signature")
    if not isinstance(signature, dict):
        signature = {}
        errors.append("system_signature_missing")
    if signature.get("ensemble") != "NPT":
        errors.append("production_ensemble_not_npt")
    if signature.get("solvent_type") != "explicit":
        errors.append("production_not_explicit_solvent")
    if signature.get("system_xml_sha256") != sha256_file(base_system_path):
        errors.append("base_system_signature_mismatch")
    if signature.get("topology_pdb_sha256") != sha256_file(topology_path):
        errors.append("topology_signature_mismatch")
    duration_ns = (
        (final_step - start_step) * timestep_fs / 1_000_000.0
        if start_step is not None
        and final_step is not None
        and final_step > start_step
        else None
    )
    declared_duration_ns = metadata.get("simulation_time_ns")
    if (
        isinstance(declared_duration_ns, (int, float))
        and not isinstance(declared_duration_ns, bool)
        and duration_ns is not None
        and not math.isclose(
            float(declared_duration_ns),
            duration_ns,
            rel_tol=1.0e-6,
            abs_tol=1.0e-9,
        )
    ):
        errors.append("production_duration_metadata_mismatch")

    return {
        "engine": "OpenMM",
        "adapter_id": MDCLAW_OPENMM_ADAPTER,
        "particle_count": particle_count,
        "topology_atom_count": topology_atom_count,
        "trajectory_atom_count": int(trajectory.n_atoms),
        "trajectory_frame_count": int(trajectory.n_frames),
        "integrator_class": type(integrator).__name__,
        "integrator_temperature_k": integrator_temperature,
        "timestep_fs": timestep_fs,
        "friction_per_ps": friction_per_ps,
        "integrator_random_seed": int(integrator.getRandomNumberSeed()),
        "barostat_class": (
            type(barostats[0]).__name__ if len(barostats) == 1 else None
        ),
        "pressure_bar": pressure_bar,
        "barostat_temperature_k": barostat_temperature,
        "barostat_frequency": barostat_frequency,
        "base_force_classes": base_force_names,
        "runtime_force_classes": runtime_force_names,
        "base_system_canonical_sha256": hashlib.sha256(
            base_system_canonical_xml.encode("utf-8")
        ).hexdigest(),
        "runtime_without_barostat_canonical_sha256": hashlib.sha256(
            runtime_base_canonical_xml.encode("utf-8")
        ).hexdigest(),
        "start_step": start_step,
        "final_step": final_step,
        "final_state_step": state_step,
        "final_state_position_count": state_position_count,
        "energy_rows": energy_rows,
        "energy_final_step": energy_final_step,
        "energy_columns": energy_summary["columns"],
        "duration_ns": duration_ns,
        "platform": metadata.get("platform"),
        "random_seed": metadata.get("random_seed"),
        "base_system_physics": physics_facts,
        "final_state": state_facts,
    }, errors


def _inspect_base_system_physics(
    *,
    base_system: Any,
    topology: Any,
    force_types: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Reject empty/zero-force systems while remaining force-field agnostic."""

    from openmm.unit import (
        dalton,
        elementary_charge,
        kilojoule_per_mole,
        nanometer,
        radian,
    )

    errors: list[str] = []
    force_counts = Counter(
        type(base_system.getForce(index)).__name__
        for index in range(base_system.getNumForces())
    )
    particles = int(base_system.getNumParticles())
    topology_atoms = list(topology.atoms())
    topology_bonds = {
        tuple(sorted((int(first.index), int(second.index))))
        for first, second in topology.bonds()
    }
    topology_neighbors: dict[int, set[int]] = {}
    for first, second in topology_bonds:
        topology_neighbors.setdefault(first, set()).add(second)
        topology_neighbors.setdefault(second, set()).add(first)
    masses: list[float] = []
    for index in range(particles):
        mass = float(base_system.getParticleMass(index).value_in_unit(dalton))
        masses.append(mass)
        if (
            not math.isfinite(mass)
            or mass < 0.0
            or (mass == 0.0 and not base_system.isVirtualSite(index))
        ):
            errors.append("particle_mass_invalid")
            break
    if len(topology_atoms) != particles:
        errors.append("base_particle_topology_count_mismatch")
    else:
        standard_masses = [
            (
                float(atom.element.mass.value_in_unit(dalton))
                if atom.element is not None
                else 0.0
            )
            for atom in topology_atoms
        ]
        hydrogen_excess: dict[int, float] = {}
        for index, atom in enumerate(topology_atoms):
            mass = masses[index]
            is_virtual = bool(base_system.isVirtualSite(index))
            element = atom.element
            symbol = (
                str(getattr(element, "symbol", "")).upper()
                if element is not None
                else ""
            )
            if is_virtual and element is not None:
                errors.append("virtual_site_topology_element_mismatch")
                break
            if not is_virtual and element is None:
                errors.append("particle_topology_element_missing")
                break
            if (
                symbol == "H"
                and (
                    mass < standard_masses[index] - 0.05
                    or mass > 4.1
                )
            ):
                errors.append("hydrogen_particle_mass_mismatch")
                break
            if symbol == "H":
                hydrogen_excess[index] = max(
                    0.0,
                    mass - standard_masses[index],
                )
        else:
            for index, atom in enumerate(topology_atoms):
                element = atom.element
                symbol = (
                    str(getattr(element, "symbol", "")).upper()
                    if element is not None
                    else ""
                )
                if not symbol or symbol == "H":
                    continue
                expected_mass = standard_masses[index] - sum(
                    hydrogen_excess.get(neighbor, 0.0)
                    for neighbor in topology_neighbors.get(index, set())
                )
                if (
                    expected_mass <= 0.0
                    or not math.isclose(
                        masses[index],
                        expected_mass,
                        rel_tol=0.0,
                        abs_tol=0.25,
                    )
                ):
                    errors.append("element_particle_mass_mismatch")
                    break
            if any(
                excess > 0.0
                and not any(
                    str(
                        getattr(
                            topology_atoms[neighbor].element,
                            "symbol",
                            "",
                        )
                    ).upper()
                    != "H"
                    for neighbor in topology_neighbors.get(index, set())
                )
                for index, excess in hydrogen_excess.items()
            ):
                errors.append("hydrogen_mass_repartitioning_invalid")

    water_names = {"HOH", "WAT", "SOL", "TIP3", "TIP3P", "SPC", "SPCE"}
    water_residues = [
        residue
        for residue in topology.residues()
        if str(residue.name).upper() in water_names
    ]
    water_atom_count = sum(
        1 for residue in water_residues for _atom in residue.atoms()
    )
    nonwater_residue_count = sum(
        1
        for residue in topology.residues()
        if str(residue.name).upper() not in water_names
    )
    if not water_residues or water_atom_count == 0:
        errors.append("explicit_water_missing")
    for residue in water_residues:
        symbols = [
            str(getattr(atom.element, "symbol", "")).upper()
            if atom.element is not None
            else ""
            for atom in residue.atoms()
        ]
        if symbols.count("O") != 1 or symbols.count("H") != 2:
            errors.append("water_atom_composition_invalid")
            break
    if not base_system.usesPeriodicBoundaryConditions():
        errors.append("base_system_not_periodic")

    required = {
        "HarmonicBondForce",
        "HarmonicAngleForce",
        "NonbondedForce",
    }
    if nonwater_residue_count:
        required.add("PeriodicTorsionForce")
    for force_name in sorted(required):
        if force_counts.get(force_name, 0) < 1:
            errors.append(f"{force_name}_missing")

    nonbonded_forces = [
        base_system.getForce(index)
        for index in range(base_system.getNumForces())
        if isinstance(
            base_system.getForce(index),
            force_types["NonbondedForce"],
        )
    ]
    nonzero_charges = 0
    positive_epsilons = 0
    nonbonded_method = None
    if len(nonbonded_forces) != 1:
        errors.append("nonbonded_force_count_mismatch")
    else:
        nonbonded = nonbonded_forces[0]
        nonbonded_method = int(nonbonded.getNonbondedMethod())
        periodic_methods = {
            int(nonbonded.CutoffPeriodic),
            int(nonbonded.Ewald),
            int(nonbonded.PME),
        }
        if hasattr(nonbonded, "LJPME"):
            periodic_methods.add(int(nonbonded.LJPME))
        if nonbonded_method not in periodic_methods:
            errors.append("nonbonded_method_not_periodic")
        if int(nonbonded.getNumParticles()) != particles:
            errors.append("nonbonded_particle_count_mismatch")
        for index in range(int(nonbonded.getNumParticles())):
            charge, sigma, epsilon = nonbonded.getParticleParameters(index)
            charge_e = float(charge.value_in_unit(elementary_charge))
            sigma_nm = float(sigma.value_in_unit(nanometer))
            epsilon_kj = float(epsilon.value_in_unit(kilojoule_per_mole))
            if not all(
                math.isfinite(value)
                for value in (charge_e, sigma_nm, epsilon_kj)
            ) or sigma_nm < 0.0 or epsilon_kj < 0.0:
                errors.append("nonbonded_particle_parameter_invalid")
                break
            nonzero_charges += int(abs(charge_e) > 1.0e-8)
            positive_epsilons += int(epsilon_kj > 1.0e-8)
        if nonzero_charges == 0:
            errors.append("nonbonded_charges_all_zero")
        if positive_epsilons == 0:
            errors.append("nonbonded_epsilons_all_zero")

    bond_count = 0
    for index in range(base_system.getNumForces()):
        force = base_system.getForce(index)
        if not isinstance(force, force_types["HarmonicBondForce"]):
            continue
        bond_count += int(force.getNumBonds())
        for term in range(int(force.getNumBonds())):
            _a, _b, length, stiffness = force.getBondParameters(term)
            values = (
                float(length.value_in_unit(nanometer)),
                float(
                    stiffness.value_in_unit(
                        kilojoule_per_mole / nanometer**2
                    )
                ),
            )
            if (
                not all(math.isfinite(value) for value in values)
                or values[0] <= 0.0
                or values[1] <= 0.0
            ):
                errors.append("harmonic_bond_parameter_invalid")
                break
    if bond_count == 0:
        errors.append("harmonic_bonds_missing")

    harmonic_bond_pairs: set[tuple[int, int]] = set()
    for index in range(base_system.getNumForces()):
        force = base_system.getForce(index)
        if not isinstance(force, force_types["HarmonicBondForce"]):
            continue
        for term in range(int(force.getNumBonds())):
            first, second, _length, _stiffness = force.getBondParameters(term)
            harmonic_bond_pairs.add(
                tuple(sorted((int(first), int(second))))
            )
    constraint_pairs: set[tuple[int, int]] = set()
    for index in range(int(base_system.getNumConstraints())):
        first, second, _distance = base_system.getConstraintParameters(index)
        constraint_pairs.add(tuple(sorted((int(first), int(second)))))
    represented_bonds = harmonic_bond_pairs | constraint_pairs
    allowed_angle_constraints = {
        pair
        for pair in constraint_pairs - topology_bonds
        if topology_neighbors.get(pair[0], set())
        & topology_neighbors.get(pair[1], set())
        and any(
            str(
                getattr(topology_atoms[particle].element, "symbol", "")
            ).upper()
            == "H"
            for particle in pair
        )
    }
    if (
        not topology_bonds <= represented_bonds
        or bool(harmonic_bond_pairs - topology_bonds)
        or bool(
            constraint_pairs
            - topology_bonds
            - allowed_angle_constraints
        )
    ):
        errors.append("topology_system_bond_graph_mismatch")

    angle_count = 0
    for index in range(base_system.getNumForces()):
        force = base_system.getForce(index)
        if not isinstance(force, force_types["HarmonicAngleForce"]):
            continue
        angle_count += int(force.getNumAngles())
        for term in range(int(force.getNumAngles())):
            _a, _b, _c, angle, stiffness = force.getAngleParameters(term)
            values = (
                float(angle.value_in_unit(radian)),
                float(
                    stiffness.value_in_unit(
                        kilojoule_per_mole / radian**2
                    )
                ),
            )
            if (
                not all(math.isfinite(value) for value in values)
                or values[0] <= 0.0
                or values[1] <= 0.0
            ):
                errors.append("harmonic_angle_parameter_invalid")
                break
    if angle_count == 0:
        errors.append("harmonic_angles_missing")

    torsion_count = sum(
        int(base_system.getForce(index).getNumTorsions())
        for index in range(base_system.getNumForces())
        if isinstance(
            base_system.getForce(index),
            force_types["PeriodicTorsionForce"],
        )
    )
    if nonwater_residue_count and torsion_count == 0:
        errors.append("periodic_torsions_missing")

    return {
        "particle_count": particles,
        "positive_mass_count": sum(mass > 0.0 for mass in masses),
        "force_counts": dict(sorted(force_counts.items())),
        "nonbonded_method": nonbonded_method,
        "nonzero_charge_count": nonzero_charges,
        "positive_epsilon_count": positive_epsilons,
        "bond_count": bond_count,
        "angle_count": angle_count,
        "torsion_count": torsion_count,
        "water_residue_count": len(water_residues),
        "water_atom_count": water_atom_count,
        "nonwater_residue_count": nonwater_residue_count,
        "topology_bond_count": len(topology_bonds),
        "harmonic_bond_pair_count": len(harmonic_bond_pairs),
        "constraint_pair_count": len(constraint_pairs),
        "allowed_angle_constraint_count": len(allowed_angle_constraints),
    }, list(dict.fromkeys(errors))


def _inspect_final_state(
    state: Any,
    *,
    nanometer: Any,
    picosecond: Any,
) -> tuple[dict[str, Any], list[str]]:
    import numpy as np

    errors: list[str] = []
    position_count = 0
    velocity_count = 0
    box_volume_nm3 = None
    try:
        positions = state.getPositions(asNumpy=True).value_in_unit(nanometer)
        position_count = int(len(positions))
        if not bool(np.isfinite(positions).all()):
            errors.append("final_state_positions_nonfinite")
    except Exception:  # noqa: BLE001
        errors.append("final_state_positions_missing")
    try:
        velocities = state.getVelocities(asNumpy=True).value_in_unit(
            nanometer / picosecond
        )
        velocity_count = int(len(velocities))
        if (
            velocity_count != position_count
            or not bool(np.isfinite(velocities).all())
        ):
            errors.append("final_state_velocities_invalid")
    except Exception:  # noqa: BLE001
        errors.append("final_state_velocities_missing")
    try:
        vectors = state.getPeriodicBoxVectors(asNumpy=True).value_in_unit(
            nanometer
        )
        if not bool(np.isfinite(vectors).all()):
            errors.append("final_state_box_nonfinite")
        else:
            box_volume_nm3 = float(abs(np.linalg.det(vectors)))
            if not math.isfinite(box_volume_nm3) or box_volume_nm3 <= 0.0:
                errors.append("final_state_box_invalid")
    except Exception:  # noqa: BLE001
        errors.append("final_state_box_missing")
    return {
        "position_count": position_count,
        "velocity_count": velocity_count,
        "box_volume_nm3": box_volume_nm3,
    }, errors


def _confirmatory_runs_with_roles(
    study_index: dict[str, Any],
    errors: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    systems = study_index.get("systems")
    comparisons = study_index.get("comparisons")
    if not isinstance(systems, list) or not isinstance(comparisons, list):
        _issue(
            errors,
            "study_index_structure_invalid",
            "study_index systems/comparisons are required for execution attestation",
        )
        return {}
    system_roles: dict[str, str] = {}
    for comparison in comparisons:
        if not isinstance(comparison, dict):
            continue
        for key, role in (
            ("reference_system_ids", "reference"),
            ("variant_system_ids", "variant"),
        ):
            for system_id in comparison.get(key) or []:
                if isinstance(system_id, str):
                    previous = system_roles.get(system_id)
                    if previous is not None and previous != role:
                        _issue(
                            errors,
                            "system_condition_role_ambiguous",
                            f"system {system_id!r} has conflicting comparison roles",
                        )
                    system_roles[system_id] = role
    output: dict[str, dict[str, Any]] = {}
    for system in systems:
        if not isinstance(system, dict):
            continue
        system_id = system.get("system_id")
        role = system_roles.get(str(system_id))
        for run in system.get("runs") or []:
            if not isinstance(run, dict) or run.get("phase") != "confirmatory":
                continue
            run_id = run.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                continue
            output[run_id] = {
                **run,
                "system_id": system_id,
                "condition_role": role,
            }
    return output


def _execution_certificate(
    *,
    ledger: dict[str, Any] | None,
    errors: list[dict[str, str]],
    attested_runs: list[dict[str, Any]] | None = None,
    submitted_intent_hash: str | None = None,
) -> dict[str, Any]:
    execution_attested = bool(ledger is not None and not errors)
    return {
        "schema_version": "1.0",
        "kind": "mdstudybench_runner_execution_certificate_v2",
        "truth_blind": True,
        "adapter_id": (
            ledger.get("adapter_id") if isinstance(ledger, dict) else None
        ),
        "runner_custody": bool(
            isinstance(ledger, dict)
            and ledger.get("recorded_by") == "mdclaw_benchmark_runner"
        ),
        "execution_attested": execution_attested,
        "attestation_scope": {
            "production_runtime_matches_frozen_base_system": (
                execution_attested
            ),
            "base_system_construction_attested": False,
            "runtime_environment_attested": False,
        },
        "diagnostic_reason_codes": [
            "base_system_construction_unattested",
            "runtime_environment_unattested",
        ],
        "analysis_intent_sha256": submitted_intent_hash,
        "attested_runs": attested_runs or [],
        "reason_codes": list(dict.fromkeys(item["code"] for item in errors)),
        "errors": errors,
    }


def _node_event(
    *,
    run_id: str,
    production_event_id: str,
    condition_role: str,
    intent_sha256: str,
    started_at: str,
    completed_at: str,
    walltime_seconds: float,
    job_dir: Path,
    node_id: str,
    errors: list[str],
    input_paths: dict[str, Path | None] | None = None,
    output_paths: dict[str, Path | None] | None = None,
    runtime_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    execution_valid = not errors
    return {
        "run_id": run_id,
        "production_event_id": production_event_id,
        "condition_role": condition_role,
        "adapter_id": MDCLAW_OPENMM_ADAPTER,
        "intent_sha256": intent_sha256,
        "started_at": started_at,
        "completed_at": completed_at,
        "walltime_seconds": walltime_seconds,
        "job_dir": str(job_dir),
        "node_id": node_id,
        "valid": execution_valid,
        "reason_codes": list(dict.fromkeys(errors)),
        "attestation_scope": {
            "production_runtime_matches_frozen_base_system": (
                execution_valid
            ),
            "base_system_construction_attested": False,
        },
        "diagnostic_reason_codes": [
            "base_system_construction_unattested",
        ],
        "input_artifacts": _artifact_records(input_paths or {}),
        "output_artifacts": _artifact_records(output_paths or {}),
        "runtime": runtime_facts or {},
    }


def _artifact_records(paths: dict[str, Path | None]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, path in paths.items():
        if path is None or not path.is_file():
            continue
        output[key] = {
            "path": str(path),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
    return output


def _safe_file(root: Path, relative: Any) -> Path | None:
    if not isinstance(relative, (str, Path)):
        return None
    candidate = Path(relative)
    if candidate.is_absolute():
        return None
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved if resolved.is_file() else None


def _existing_path(value: Any) -> Path | None:
    if not isinstance(value, (str, Path)):
        return None
    path = Path(value).resolve()
    return path if path.is_file() else None


def _node_artifact(node_dir: Path, relative: Any) -> Path | None:
    if not isinstance(relative, str) or not relative:
        return None
    path = (node_dir / relative).resolve()
    try:
        path.relative_to(node_dir.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def _run_trajectory_paths(run: dict[str, Any]) -> list[str]:
    segments = run.get("trajectory_segments")
    if isinstance(segments, list) and segments:
        return [value for value in segments if isinstance(value, str)]
    trajectory = run.get("trajectory")
    return [trajectory] if isinstance(trajectory, str) else []


def _event_started_after_freeze(
    event: dict[str, Any],
    frozen: dict[str, Any],
) -> bool:
    from datetime import datetime

    try:
        started = datetime.fromisoformat(str(event.get("started_at")))
        frozen_at = datetime.fromisoformat(str(frozen.get("frozen_at")))
    except (TypeError, ValueError):
        return False
    event_sequence = event.get("runner_sequence")
    frozen_sequence = frozen.get("runner_sequence")
    if (
        isinstance(event_sequence, int)
        and not isinstance(event_sequence, bool)
        and isinstance(frozen_sequence, int)
        and not isinstance(frozen_sequence, bool)
    ):
        return event_sequence > frozen_sequence and started >= frozen_at
    return started > frozen_at


def _state_step_count(state: Any) -> int | None:
    getter = getattr(state, "getStepCount", None)
    if getter is None:
        return None
    try:
        return int(getter())
    except Exception:  # noqa: BLE001
        return None


def _energy_step_summary(path: Path | None) -> dict[str, Any]:
    empty = {
        "row_count": 0,
        "final_step": None,
        "columns": [],
        "required_columns_present": False,
        "finite": False,
    }
    if path is None:
        return empty
    try:
        lines = [line for line in path.read_text().splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError):
        return empty
    if len(lines) < 2:
        return empty
    header_line = lines[0].lstrip()
    if header_line.startswith("#"):
        header_line = header_line[1:]
    try:
        columns = [
            value.strip().strip('"')
            for value in next(csv.reader([header_line]))
        ]
        rows = list(csv.reader(lines[1:]))
    except (csv.Error, StopIteration):
        return empty
    normalized_columns = [value.lower() for value in columns]
    required_fragments = (
        "step",
        "potential energy",
        "kinetic energy",
        "total energy",
        "temperature",
        "box volume",
        "density",
    )
    required_columns_present = all(
        any(fragment in column for column in normalized_columns)
        for fragment in required_fragments
    )
    finite = bool(rows)
    for row in rows:
        if len(row) != len(columns):
            finite = False
            continue
        try:
            values = [float(value.strip()) for value in row]
        except ValueError:
            finite = False
            continue
        if not all(math.isfinite(value) for value in values):
            finite = False
    step_index = next(
        (
            index
            for index, column in enumerate(normalized_columns)
            if column == "step"
        ),
        None,
    )
    try:
        final_step = (
            int(float(rows[-1][step_index]))
            if step_index is not None and rows
            else None
        )
    except (IndexError, TypeError, ValueError):
        final_step = None
    return {
        "row_count": len(rows),
        "final_step": final_step,
        "columns": columns,
        "required_columns_present": required_columns_present,
        "finite": finite,
    }


def _has_completed_ancestor(
    job_dir: Path,
    node_id: str,
    node_type: str,
) -> bool:
    start = _read_json(job_dir / "nodes" / node_id / "node.json")
    queue = [
        value
        for value in start.get("parent_node_ids") or []
        if isinstance(value, str)
    ]
    visited: set[str] = set()
    while queue:
        candidate_id = queue.pop(0)
        if candidate_id in visited:
            continue
        visited.add(candidate_id)
        candidate = _read_json(
            job_dir / "nodes" / candidate_id / "node.json"
        )
        if (
            (candidate.get("node_type") or candidate.get("type")) == node_type
            and candidate.get("status") == "completed"
        ):
            return True
        queue.extend(
            value
            for value in candidate.get("parent_node_ids") or []
            if isinstance(value, str)
        )
    return False


def _finite_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return int(value)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _issue(
    errors: list[dict[str, str]],
    code: str,
    message: str,
) -> None:
    errors.append({"code": code, "message": message})
