"""Top-level CLI tool functions for the MD benchmark suites.

Each function is a thin orchestration layer over ``models``, ``validation``,
``scoring``, ``judge``, and ``run``. Every function returns a JSON-serializable
dict so the dispatcher in ``mdclaw._cli`` can emit it as stdout.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from mdclaw._common import ensure_directory
from mdclaw.benchmark import judge, public_contract, scoring, validation
from mdclaw.benchmark.datasets import (
    DEFAULT_BENCHMARK_VERSION,
    DEFAULT_DATASET_DIR,
)
from mdclaw.benchmark.models import (
    SubmissionManifest,
    Task,
)

_PUBLIC_EXPORT_MARKER = ".md-benchmark-public-export.json"
_PUBLIC_EXPORT_KIND = "md_benchmark_public_export"
_PRIVATE_EXPORT_MARKER = ".md-benchmark-private-export.json"
_PRIVATE_EXPORT_KIND = "md_benchmark_private_evaluator_export"


def _has_valid_public_export_marker(path: Path) -> bool:
    return _has_valid_export_marker(path, _PUBLIC_EXPORT_MARKER, _PUBLIC_EXPORT_KIND)


def _has_valid_private_export_marker(path: Path) -> bool:
    return _has_valid_export_marker(path, _PRIVATE_EXPORT_MARKER, _PRIVATE_EXPORT_KIND)


def _has_valid_export_marker(path: Path, marker_name: str, kind: str) -> bool:
    marker = path / marker_name
    if not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text())
    except json.JSONDecodeError:
        return False
    return payload.get("kind") == kind


def _public_export_destination_error(source: Path, dest: Path) -> Optional[str]:
    if dest.resolve() == source.resolve():
        return "output_dir must be different from dataset_dir"
    if dest.exists() and not dest.is_dir():
        return f"output_dir exists and is not a directory: {dest}"
    if dest.exists() and any(dest.iterdir()) and not _has_valid_public_export_marker(dest):
        return (
            "output_dir exists and was not created by "
            "export_benchmark_public_package; refusing to overwrite: "
            f"{dest}"
        )
    return None


def _private_export_destination_error(source: Path, dest: Path) -> Optional[str]:
    if dest.resolve() == source.resolve():
        return "output_dir must be different from dataset_dir"
    if dest.exists() and not dest.is_dir():
        return f"output_dir exists and is not a directory: {dest}"
    if dest.exists() and any(dest.iterdir()) and not _has_valid_private_export_marker(dest):
        return (
            "output_dir exists and was not created by "
            "export_benchmark_private_package; refusing to overwrite: "
            f"{dest}"
        )
    return None


def _build_family_lookup(dataset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for family_key, family in (dataset.get("families") or {}).items():
        if not isinstance(family, dict):
            continue
        for task_id in family.get("task_ids") or []:
            lookup[str(task_id)] = {
                "family": family_key,
                "family_display_name": family.get("display_name", family_key),
                "family_intent": family.get("intent", ""),
            }
    return lookup


def _intent_summary(task_intent: str) -> str:
    """Return a compact one-sentence summary for task discovery output."""
    first_sentence = task_intent.split(". ", 1)[0].strip()
    if first_sentence and not first_sentence.endswith("."):
        first_sentence += "."
    return first_sentence


# ---------------------------------------------------------------------------
# Discovery


def list_benchmark_tasks(dataset_dir: str = DEFAULT_DATASET_DIR) -> dict[str, Any]:
    """List tasks defined under ``dataset_dir``. v1.0 reads dataset.json
    rather than embedding the task list in code.
    """
    dataset_path = Path(dataset_dir) / "dataset.json"
    if not dataset_path.is_file():
        return {"success": False, "errors": [f"dataset.json not found at {dataset_path}"]}
    try:
        dataset = json.loads(dataset_path.read_text())
    except json.JSONDecodeError as exc:
        return {"success": False, "errors": [f"dataset.json invalid: {exc}"]}

    tasks_meta: list[dict[str, Any]] = []
    family_lookup = _build_family_lookup(dataset)
    for task_id in dataset.get("task_ids", []):
        task_path = Path(dataset_dir) / "tasks" / task_id / "task.json"
        if not task_path.is_file():
            tasks_meta.append({"task_id": task_id, "missing": True})
            continue
        try:
            task = validation.load_task(task_path)
        except (ValidationError, json.JSONDecodeError) as exc:
            tasks_meta.append({"task_id": task_id, "errors": str(exc)})
            continue
        tasks_meta.append({
            "task_id": task.task_id,
            "category": task.category,
            "family": family_lookup.get(task.task_id, {}).get("family"),
            "family_display_name": family_lookup.get(task.task_id, {}).get(
                "family_display_name"
            ),
            "primary_score": task.primary_score,
            "secondary_scores": list(task.secondary_scores),
            "execution_mode": task.execution_mode,
            "time_limit_minutes": task.time_limit_minutes,
            "intent_summary": _intent_summary(task.task_intent),
        })

    return {
        "success": True,
        "benchmark_version": dataset.get(
            "benchmark_version", DEFAULT_BENCHMARK_VERSION
        ),
        "schema_version": dataset.get("schema_version", "1.0"),
        "task_count": len(tasks_meta),
        "families": dataset.get("families", {}),
        "tasks": tasks_meta,
    }


# ---------------------------------------------------------------------------
# Validation


def validate_benchmark_task(task_file: str) -> dict[str, Any]:
    """Validate a single task.json. Wraps :func:`validation.validate_task`."""
    return validation.validate_task(task_file)


def validate_benchmark_submission(task_file: str,
                                  submission_dir: str) -> dict[str, Any]:
    """Validate a submission directory against its task contract."""
    return validation.validate_submission(task_file, submission_dir)


# ---------------------------------------------------------------------------
# Scoring


def score_benchmark_submission(
    task_file: str,
    submission_dir: str,
    run_id: str = "",
    output_file: Optional[str] = None,
    llm_judge_file: Optional[str] = None,
    harness_record_file: Optional[str] = None,
) -> dict[str, Any]:
    """Score a submission directory and write ``score.json``.

    Returns a dict with the score payload and the path to score.json.
    """
    task_path = Path(task_file)
    sub_dir = Path(submission_dir)

    try:
        task = validation.load_task(task_path)
    except (ValidationError, json.JSONDecodeError, FileNotFoundError) as exc:
        return {"success": False, "errors": [f"task file invalid: {exc}"]}

    try:
        judge_payload = judge.load_judge_payload(llm_judge_file)
    except ValueError as exc:
        return {"success": False, "errors": [str(exc)]}

    score = scoring.score_submission(
        task=task,
        submission_dir=sub_dir,
        run_id=run_id,
        llm_judge_payload=judge_payload,
        task_dir=task_path.parent,
        harness_record_file=harness_record_file,
    )
    score_payload = score.model_dump()

    if output_file is None:
        output_file = str(sub_dir / "score.json")
    out_path = Path(output_file)
    ensure_directory(out_path.parent)
    out_path.write_text(json.dumps(score_payload, indent=2, sort_keys=True,
                                   default=str) + "\n")

    return {
        "success": True,
        "task_id": score.task_id,
        "score_file": str(out_path),
        "score": score_payload,
    }


def validate_and_score_benchmark_submission(
    task_file: str,
    submission_dir: str,
    run_id: str = "",
    output_file: Optional[str] = None,
    validation_output_file: Optional[str] = None,
    llm_judge_file: Optional[str] = None,
    require_validation_success: bool = True,
    harness_record_file: Optional[str] = None,
) -> dict[str, Any]:
    """Validate, score, and return normalized status fields.

    This is the evaluator-side convenience wrapper for benchmark harnesses.
    It deliberately does not run an MD agent. It only consumes a completed
    ``submission/`` directory, writes optional validation/score artifacts, and
    exposes the canonical pass/fail fields so callers do not need to know the
    internal shape returned by :func:`score_benchmark_submission`.
    """
    sub_dir = Path(submission_dir)
    validation_result = validate_benchmark_submission(task_file, submission_dir)
    validation_file = None
    if validation_output_file:
        validation_path = Path(validation_output_file)
        ensure_directory(validation_path.parent)
        validation_path.write_text(
            json.dumps(validation_result, indent=2, sort_keys=True, default=str) + "\n"
        )
        validation_file = str(validation_path)

    if require_validation_success and not validation_result.get("success"):
        return {
            "success": False,
            "task_id": validation_result.get("task_id"),
            "submission_dir": str(sub_dir),
            "validation_success": False,
            "validation_file": validation_file,
            "score_success": False,
            "score_file": None,
            "score_status": None,
            "weighted_total": None,
            "scores": None,
            "benchmark_passed": False,
            "validation": validation_result,
            "score": None,
            "errors": validation_result.get("errors", []),
        }

    score_result = score_benchmark_submission(
        task_file=task_file,
        submission_dir=submission_dir,
        run_id=run_id,
        output_file=output_file,
        llm_judge_file=llm_judge_file,
        harness_record_file=harness_record_file,
    )
    if not score_result.get("success"):
        return {
            "success": False,
            "task_id": validation_result.get("task_id"),
            "submission_dir": str(sub_dir),
            "validation_success": bool(validation_result.get("success")),
            "validation_file": validation_file,
            "score_success": False,
            "score_file": score_result.get("score_file"),
            "score_status": None,
            "weighted_total": None,
            "scores": None,
            "benchmark_passed": False,
            "validation": validation_result,
            "score": None,
            "errors": score_result.get("errors", []),
        }

    score_payload = score_result.get("score") or {}
    score_status = score_payload.get("status")
    weighted_total = score_payload.get("weighted_total")
    return {
        "success": True,
        "task_id": score_payload.get("task_id") or validation_result.get("task_id"),
        "submission_dir": str(sub_dir),
        "validation_success": bool(validation_result.get("success")),
        "validation_file": validation_file,
        "score_success": True,
        "score_file": score_result.get("score_file"),
        "score_status": score_status,
        "weighted_total": weighted_total,
        "scores": score_payload.get("scores"),
        "benchmark_passed": score_status == "passed",
        "validation": validation_result,
        "score": score_payload,
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Schema / dataset maintenance


def write_benchmark_schemas(
    output_dir: str = f"{DEFAULT_DATASET_DIR}/schemas",
) -> dict[str, Any]:
    """Generate JSON Schema files from the pydantic models."""
    out_dir = Path(output_dir)
    ensure_directory(out_dir)

    files = []
    schemas = {
        "task.schema.json": Task,
        "submission_manifest.schema.json": SubmissionManifest,
    }
    # Score schema is generated separately because the scoring layer is the
    # authority for its shape.
    from mdclaw.benchmark.models import Score
    schemas["score.schema.json"] = Score

    for filename, model in schemas.items():
        schema = model.model_json_schema()
        target = out_dir / filename
        target.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        files.append(str(target))

    return {"success": True, "schemas_written": files}


def export_benchmark_public_package(
    dataset_dir: str = DEFAULT_DATASET_DIR,
    output_dir: Optional[str] = None,
) -> dict[str, Any]:
    """Export the agent-visible benchmark package.

    The canonical dataset layout keeps ``prompt.md``, ``task.json``, and
    scorer-only ``truth/`` files next to each other for repository maintenance.
    External agents should not receive that canonical tree directly. This
    helper creates a public package containing only:

    - ``dataset.json``
    - submission-facing schemas
    - one ``prompt.md`` plus ``submission_contract.json`` and
      ``submission_checklist.md`` per task

    It deliberately omits ``task.json``, ``truth/``, and ``scorer/``.
    """
    source = Path(dataset_dir)
    if output_dir is None:
        dest = source / "public"
    else:
        dest = Path(output_dir)

    dataset_path = source / "dataset.json"
    if not dataset_path.is_file():
        return {
            "success": False,
            "errors": [f"dataset.json not found at {dataset_path}"],
        }
    try:
        dataset = json.loads(dataset_path.read_text())
    except json.JSONDecodeError as exc:
        return {
            "success": False,
            "errors": [f"dataset.json invalid: {exc}"],
        }

    destination_error = _public_export_destination_error(source, dest)
    if destination_error is not None:
        return {"success": False, "errors": [destination_error]}

    dest.parent.mkdir(parents=True, exist_ok=True)
    staging: Optional[Path] = Path(
        tempfile.mkdtemp(prefix=f".{dest.name}.", dir=str(dest.parent))
    )
    try:
        shutil.copy2(dataset_path, staging / "dataset.json")

        schemas_dir = staging / "schemas"
        schemas_dir.mkdir()
        schema_files = []
        for name in ("submission_manifest.schema.json", "score.schema.json"):
            src = source / "schemas" / name
            if src.is_file():
                shutil.copy2(src, schemas_dir / name)
                schema_files.append(str(dest / "schemas" / name))

        task_files: list[str] = []
        task_ids = [str(tid) for tid in dataset.get("task_ids", [])]
        benchmark_version = str(
            dataset.get("benchmark_version", DEFAULT_BENCHMARK_VERSION)
        )
        for task_id in task_ids:
            task_dir = source / "tasks" / task_id
            prompt_src = task_dir / "prompt.md"
            task_src = task_dir / "task.json"
            if not prompt_src.is_file():
                return {
                    "success": False,
                    "errors": [f"missing prompt.md for {task_id}: {prompt_src}"],
                }
            try:
                task = validation.load_task(task_src)
            except (ValidationError, json.JSONDecodeError, FileNotFoundError) as exc:
                return {
                    "success": False,
                    "errors": [f"task file invalid for {task_id}: {exc}"],
                }

            public_task_dir = staging / "tasks" / task_id
            public_task_dir.mkdir(parents=True)
            shutil.copy2(prompt_src, public_task_dir / "prompt.md")

            contract = public_contract.public_submission_contract(
                task,
                benchmark_version=benchmark_version,
            )
            contract_path = public_task_dir / "submission_contract.json"
            contract_path.write_text(
                json.dumps(contract, indent=2, sort_keys=True) + "\n"
            )
            checklist_path = public_task_dir / "submission_checklist.md"
            checklist_path.write_text(
                public_contract.submission_checklist_markdown(task, contract)
            )
            task_files.extend([
                str(dest / "tasks" / task_id / "prompt.md"),
                str(dest / "tasks" / task_id / "submission_contract.json"),
                str(dest / "tasks" / task_id / "submission_checklist.md"),
            ])

        readme = staging / "README.md"
        readme.write_text(
            "# MD Benchmark Public Package\n\n"
            "This directory is safe to give to benchmark agents. It contains task "
            "prompts and submission-facing contracts only.\n\n"
            "Agents should read `tasks/<task_id>/prompt.md`, then use "
            "`tasks/<task_id>/submission_contract.json` and "
            "`tasks/<task_id>/submission_checklist.md` to build a `submission/` "
            "directory. The contract includes a `submission_blueprint` for the "
            "minimum manifest, metrics, provenance, and minimization-report "
            "shape expected by the scorer. For MDClaw prep submissions, use "
            "a `min` node with `mdclaw run_minimization` when running a normal "
            "MDClaw DAG. When packaging a topology bundle directly, use "
            "`mdclaw export_state_pdb` to create `minimized_structure.pdb` "
            "from the `topology.pdb` + `state.xml` bundle.\n\n"
            "Agents "
            "must not be given evaluator-side `task.json`, `truth/`, or `scorer/` "
            "files from the canonical repository tree. The contract lists required "
            "outputs, metric requirements, and manifest rules such as "
            "`status=\"completed\"`.\n\n"
            "Score submissions with the MDClaw benchmark scorer from a held-out "
            "private evaluator package exported by "
            "`mdclaw export_benchmark_private_package`, or from a canonical "
            "dataset checkout that was never mounted into the solver workspace.\n"
        )

        marker_path = staging / _PUBLIC_EXPORT_MARKER
        marker_path.write_text(
            json.dumps(
                {
                    "kind": _PUBLIC_EXPORT_KIND,
                    "dataset_dir": str(source),
                    "benchmark_version": benchmark_version,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

        if dest.exists():
            shutil.rmtree(dest)
        staging.rename(dest)
        staging = None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    return {
        "success": True,
        "dataset_dir": str(source),
        "output_dir": str(dest),
        "task_count": len(task_ids),
        "files_written": [
            str(dest / "dataset.json"),
            str(dest / "README.md"),
            str(dest / _PUBLIC_EXPORT_MARKER),
        ]
        + schema_files
        + task_files,
        "omitted_private_material": ["task.json", "truth/", "scorer/"],
    }


def export_benchmark_private_package(
    dataset_dir: str = DEFAULT_DATASET_DIR,
    output_dir: Optional[str] = None,
) -> dict[str, Any]:
    """Export evaluator-only benchmark material for a held-out scorer package.

    This is the complement of :func:`export_benchmark_public_package`. The
    public package is safe for solvers; this private package is only for the
    scorer/harness repo or container. It contains canonical ``task.json`` files,
    scorer-side ``truth/`` and ``scorer/`` directories when present, schemas,
    and top-level private references. It deliberately omits task prompts and
    public submission checklists so it is not mistaken for an agent package.
    """
    source = Path(dataset_dir)
    if output_dir is None:
        dest = source / "private_evaluator"
    else:
        dest = Path(output_dir)

    dataset_path = source / "dataset.json"
    if not dataset_path.is_file():
        return {
            "success": False,
            "errors": [f"dataset.json not found at {dataset_path}"],
        }
    try:
        dataset = json.loads(dataset_path.read_text())
    except json.JSONDecodeError as exc:
        return {
            "success": False,
            "errors": [f"dataset.json invalid: {exc}"],
        }

    destination_error = _private_export_destination_error(source, dest)
    if destination_error is not None:
        return {"success": False, "errors": [destination_error]}

    dest.parent.mkdir(parents=True, exist_ok=True)
    staging: Optional[Path] = Path(
        tempfile.mkdtemp(prefix=f".{dest.name}.", dir=str(dest.parent))
    )
    try:
        shutil.copy2(dataset_path, staging / "dataset.json")

        files_written = [str(dest / "dataset.json")]

        schemas_src = source / "schemas"
        if schemas_src.is_dir():
            schemas_dest = staging / "schemas"
            shutil.copytree(schemas_src, schemas_dest)
            files_written.extend(
                str(dest / path.relative_to(staging))
                for path in sorted(schemas_dest.rglob("*"))
                if path.is_file()
            )

        private_refs_src = source / "private_references"
        if private_refs_src.is_dir():
            private_refs_dest = staging / "private_references"
            shutil.copytree(private_refs_src, private_refs_dest)
            files_written.extend(
                str(dest / path.relative_to(staging))
                for path in sorted(private_refs_dest.rglob("*"))
                if path.is_file()
            )

        task_ids = [str(tid) for tid in dataset.get("task_ids", [])]
        private_material: list[str] = []
        for task_id in task_ids:
            task_dir = source / "tasks" / task_id
            task_src = task_dir / "task.json"
            try:
                validation.load_task(task_src)
            except (ValidationError, json.JSONDecodeError, FileNotFoundError) as exc:
                return {
                    "success": False,
                    "errors": [f"task file invalid for {task_id}: {exc}"],
                }

            private_task_dir = staging / "tasks" / task_id
            private_task_dir.mkdir(parents=True)
            shutil.copy2(task_src, private_task_dir / "task.json")
            files_written.append(str(dest / "tasks" / task_id / "task.json"))
            private_material.append(f"tasks/{task_id}/task.json")

            for private_name in ("truth", "scorer"):
                private_src = task_dir / private_name
                if not private_src.is_dir():
                    continue
                private_dest = private_task_dir / private_name
                shutil.copytree(private_src, private_dest)
                for path in sorted(private_dest.rglob("*")):
                    if path.is_file():
                        rel = path.relative_to(staging).as_posix()
                        files_written.append(str(dest / rel))
                        private_material.append(rel)

        readme = staging / "README.md"
        readme.write_text(
            "# MD Benchmark Private Evaluator Package\n\n"
            "This directory is for the scorer/harness only. Do not mount it "
            "into the solver workspace. It contains canonical task contracts, "
            "held-out truth files, and scorer-side references needed to run "
            "`mdclaw validate_and_score_benchmark_submission` or "
            "`mdclaw score_benchmark_run`.\n\n"
            "Solvers should receive a separate public package exported with "
            "`mdclaw export_benchmark_public_package`.\n\n"
            "For strict provenance checks, keep harness execution records "
            "outside each solver-writable `submission/` directory and pass "
            "`--harness-record-file` when scoring single submissions. Prepared "
            "run directories use `tasks/<task_id>/harness_execution.json` by "
            "default.\n"
        )
        files_written.append(str(dest / "README.md"))

        marker_path = staging / _PRIVATE_EXPORT_MARKER
        marker_path.write_text(
            json.dumps(
                {
                    "kind": _PRIVATE_EXPORT_KIND,
                    "dataset_dir": str(source),
                    "benchmark_version": dataset.get(
                        "benchmark_version", DEFAULT_BENCHMARK_VERSION
                    ),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        files_written.append(str(dest / _PRIVATE_EXPORT_MARKER))

        if dest.exists():
            shutil.rmtree(dest)
        staging.rename(dest)
        staging = None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    return {
        "success": True,
        "dataset_dir": str(source),
        "output_dir": str(dest),
        "task_count": len(task_ids),
        "files_written": files_written,
        "included_private_material": sorted(set(private_material)),
        "omitted_agent_material": [
            "prompt.md",
            "submission_contract.json",
            "submission_checklist.md",
        ],
    }


def _copy_if_different(src: Path, dst: Path) -> None:
    """Copy an artifact unless it is already at the requested destination."""
    if src.resolve() == dst.resolve():
        return
    shutil.copy2(src, dst)


def _single_point_energy_kj_mol(system_xml: Path, state_xml: Path) -> dict[str, Any]:
    """Compute one potential energy for an OpenMM system + serialized state.

    Artifact-as-truth: this measures the agent's own submitted system; it does
    not change any chemistry. Returns finiteness flags and the energy so the
    packaged minimization report reflects the real artifact rather than a
    fabricated value.
    """
    out: dict[str, Any] = {
        "success": False,
        "energy_kj_mol": None,
        "energy_is_finite": False,
        "positions_are_finite": False,
        "particle_count": 0,
        "errors": [],
    }
    try:
        import math

        from openmm import (
            Context,
            LangevinIntegrator,
            Platform,
            XmlSerializer,
            unit,
        )
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"OpenMM import failed: {type(exc).__name__}: {exc}")
        return out

    try:
        system = XmlSerializer.deserialize(system_xml.read_text())
        state = XmlSerializer.deserialize(state_xml.read_text())
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"deserialize failed: {type(exc).__name__}: {exc}")
        return out

    out["particle_count"] = system.getNumParticles()
    try:
        integrator = LangevinIntegrator(
            300 * unit.kelvin, 1.0 / unit.picosecond, 0.001 * unit.picoseconds
        )
        context_platform = Platform.getPlatformByName("Reference")
        context = Context(system, integrator, context_platform)
        context.setState(state)
        snapshot = context.getState(getEnergy=True, getPositions=True)
        energy = snapshot.getPotentialEnergy().value_in_unit(
            unit.kilojoule_per_mole
        )
        out["energy_kj_mol"] = float(energy)
        out["energy_is_finite"] = bool(math.isfinite(energy))
        positions = snapshot.getPositions(asNumpy=True).value_in_unit(
            unit.nanometer
        )
        out["positions_are_finite"] = bool(
            math.isfinite(float(positions.sum()))
        )
        out["success"] = True
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"energy evaluation failed: {type(exc).__name__}: {exc}")
    return out


def package_openmm_submission(
    submission_dir: str,
    task_id: str,
    system_xml_file: str,
    topology_pdb_file: str,
    state_xml_file: str,
    run_id: str = "",
    status: str = "completed",
    prepared_structure_file: Optional[str] = None,
    evidence_report_file: Optional[str] = None,
    command_log_file: Optional[str] = None,
    force_field: str = "unspecified",
    water_model: str = "unspecified",
    agent: str = "unknown",
    backend: str = "openmm-script",
    harness: str = "unknown",
    model: str = "unknown",
    minimized: bool = True,
) -> dict[str, Any]:
    """Package an agent's own OpenMM System into a scorer-valid ``submission/``.

    This is a backend-neutral convenience for agents (including MDClaw-free
    entrants such as MDCrow or a plain OpenMM script) that already produced an
    OpenMM ``system.xml`` + ``topology.pdb`` + ``state.xml`` triple. It writes
    the topology bundle, exports ``minimized_structure.pdb`` from the state,
    measures a single real potential energy to fill ``minimization_report.json``
    honestly, and scaffolds ``manifest.json`` / ``metrics.json`` /
    ``provenance.json``.

    If an optional evidence report is desired, pass it via
    ``evidence_report_file`` so the packager can update ``manifest.json`` in
    one pass. Do not hand-edit ``manifest.json`` or ``provenance.json`` after
    this command; provenance hashes are checked by the scorer.

    Hard rule (fairness): it never *chooses* force field, water model, chains,
    ions, or mutations. Those come from the agent's own output and are
    recomputed from the artifact at scoring time anyway. Anything the agent did
    not declare is recorded as ``"unspecified"``. Provenance ``command_log``
    must come from the agent (via ``command_log_file``); when absent it is left
    empty and the scorer's execution-evidence check will flag it, rather than
    the packager inventing steps.
    """
    from mdclaw.simulation.platform import export_state_pdb

    sub = Path(submission_dir)
    system_src = Path(system_xml_file)
    topo_src = Path(topology_pdb_file)
    state_src = Path(state_xml_file)

    errors: list[str] = []
    for label, path in (
        ("system_xml_file", system_src),
        ("topology_pdb_file", topo_src),
        ("state_xml_file", state_src),
    ):
        if not path.is_file():
            errors.append(f"{label} not found: {path}")
    if evidence_report_file and not Path(evidence_report_file).is_file():
        errors.append(f"evidence_report_file not found: {evidence_report_file}")
    if errors:
        return {"success": False, "submission_dir": str(sub), "errors": errors}

    ensure_directory(sub)
    topo_dir = sub / "topology"
    ensure_directory(topo_dir)
    _copy_if_different(system_src, topo_dir / "system.xml")
    _copy_if_different(topo_src, topo_dir / "topology.pdb")
    _copy_if_different(state_src, topo_dir / "state.xml")

    minimized_pdb = sub / "minimized_structure.pdb"
    export = export_state_pdb(
        topology_pdb_file=str(topo_dir / "topology.pdb"),
        state_xml_file=str(topo_dir / "state.xml"),
        output_pdb_file=str(minimized_pdb),
    )
    if not export.get("success"):
        return {
            "success": False,
            "submission_dir": str(sub),
            "errors": ["export_state_pdb failed"] + (export.get("errors") or []),
        }

    prepared_pdb = sub / "prepared_structure.pdb"
    if prepared_structure_file and Path(prepared_structure_file).is_file():
        _copy_if_different(Path(prepared_structure_file), prepared_pdb)
    else:
        # No separate pre-minimization structure supplied: use the topology
        # reference as the prepared structure. This does not invent chemistry;
        # it reuses the agent's own topology atoms/residues.
        _copy_if_different(topo_src, prepared_pdb)

    evidence_report_path: Optional[Path] = None
    if evidence_report_file:
        evidence_report_path = sub / "evidence_report.json"
        _copy_if_different(Path(evidence_report_file), evidence_report_path)

    energy = _single_point_energy_kj_mol(
        topo_dir / "system.xml", topo_dir / "state.xml"
    )
    minimization_report = {
        "schema_version": "1.0",
        "minimization": {
            "attempted": bool(minimized),
            "completed": bool(minimized),
            "energy_is_finite": energy["energy_is_finite"],
            "positions_are_finite": energy["positions_are_finite"],
            "atom_count_preserved": True,
            "energy_initial_kj_mol": energy["energy_kj_mol"],
            "energy_final_kj_mol": energy["energy_kj_mol"],
            "particle_count": energy["particle_count"],
        },
        "notes": (
            "Packaged from an externally produced OpenMM system+state. Energy "
            "is a single-point measurement of the submitted artifact."
        ),
    }
    (sub / "minimization_report.json").write_text(
        json.dumps(minimization_report, indent=2, sort_keys=True) + "\n"
    )

    manifest = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "status": status,
        "outputs": {
            "metrics": "metrics.json",
            "provenance": "provenance.json",
            "prepared_structure": "prepared_structure.pdb",
            "minimized_structure": "minimized_structure.pdb",
            "minimization_report": "minimization_report.json",
            "topology": [
                "topology/system.xml",
                "topology/topology.pdb",
                "topology/state.xml",
            ],
        },
    }
    if evidence_report_path is not None:
        manifest["outputs"]["evidence_report"] = "evidence_report.json"
    (sub / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )

    metrics = {
        "schema_version": "1.0",
        "topology": {"backend": "openmm"},
        "preparation": {
            "force_field": force_field,
            "water_model": water_model,
        },
        "minimization": {
            "completed": bool(minimized),
            "energy_is_finite": energy["energy_is_finite"],
            "positions_are_finite": energy["positions_are_finite"],
        },
    }
    (sub / "metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n"
    )

    command_log: list[Any] = []
    if command_log_file and Path(command_log_file).is_file():
        try:
            loaded = json.loads(Path(command_log_file).read_text())
            if isinstance(loaded, list):
                command_log = loaded
            elif isinstance(loaded, dict) and isinstance(
                loaded.get("command_log"), list
            ):
                command_log = loaded["command_log"]
        except json.JSONDecodeError:
            errors.append(f"command_log_file is not valid JSON: {command_log_file}")

    provenance = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "agent": agent,
        "backend": backend,
        "harness": harness,
        "model": model,
        "command_log": command_log,
    }
    (sub / "provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n"
    )

    warnings: list[str] = []
    if not command_log:
        warnings.append(
            "no command_log provided; the scorer's execution-evidence check "
            "will flag this submission. Pass --command-log-file with the "
            "agent's own source/prep/topo/min steps."
        )
    if force_field == "unspecified" or water_model == "unspecified":
        warnings.append(
            "force_field/water_model recorded as 'unspecified'; the scorer "
            "recomputes physical properties from the artifact, but declared "
            "fidelity fields stay unspecified unless the agent provides them."
        )
    if not energy["success"]:
        warnings.append(
            "single-point energy could not be measured: "
            + "; ".join(energy.get("errors") or [])
        )

    return {
        "success": True,
        "submission_dir": str(sub),
        "task_id": task_id,
        "files_written": [
            str(sub / "manifest.json"),
            str(sub / "metrics.json"),
            str(sub / "provenance.json"),
            str(sub / "prepared_structure.pdb"),
            str(minimized_pdb),
            str(sub / "minimization_report.json"),
            str(topo_dir / "system.xml"),
            str(topo_dir / "topology.pdb"),
            str(topo_dir / "state.xml"),
        ]
        + ([str(evidence_report_path)] if evidence_report_path is not None else []),
        "energy_kj_mol": energy["energy_kj_mol"],
        "warnings": warnings,
        "errors": errors,
    }


__all__ = [
    "list_benchmark_tasks",
    "validate_benchmark_task",
    "validate_benchmark_submission",
    "validate_and_score_benchmark_submission",
    "score_benchmark_submission",
    "write_benchmark_schemas",
    "export_benchmark_public_package",
    "export_benchmark_private_package",
    "package_openmm_submission",
]
