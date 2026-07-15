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
from mdclaw.benchmark import (
    judge,
    normalization,
    public_contract,
    scoring,
    validation,
)
from mdclaw.benchmark.datasets import (
    DEFAULT_BENCHMARK_VERSION,
    DEFAULT_DATASET_DIR,
    resolve_dataset_dir,
)
from mdclaw.benchmark.models import (
    SubmissionManifest,
    Task,
)

_PUBLIC_EXPORT_MARKER = ".md-benchmark-public-export.json"
_PUBLIC_EXPORT_KIND = "md_benchmark_public_export"
_PRIVATE_EXPORT_MARKER = ".md-benchmark-private-export.json"
_PRIVATE_EXPORT_KIND = "md_benchmark_private_evaluator_export"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_STANDALONE_PACKAGER_RELATIVE = Path("tools") / "package_submission.py"
_STANDALONE_PACKAGER_SOURCE = (
    _REPO_ROOT / "benchmarks" / "tools" / "package_submission.py"
)
_STANDALONE_PREFLIGHT_RELATIVE = Path("tools") / "validate_submission.py"
_STANDALONE_PREFLIGHT_SOURCE = (
    _REPO_ROOT / "benchmarks" / "tools" / "validate_submission.py"
)
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
    dataset_path = resolve_dataset_dir(dataset_dir) / "dataset.json"
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


def validate_benchmark_submission(
    task_file: str,
    submission_dir: str,
) -> dict[str, Any]:
    """Validate a raw prep submission or a direct study submission."""
    task_path = Path(task_file)
    sub_dir = Path(submission_dir)
    try:
        task = validation.load_task(task_path)
    except (ValidationError, json.JSONDecodeError, FileNotFoundError):
        return validation.validate_submission(task_path, sub_dir)

    if task.primary_score != "preparation":
        return validation.validate_submission(task_path, sub_dir)

    with tempfile.TemporaryDirectory(prefix="mdprepbench_validate_") as temp_dir:
        normalized_dir = Path(temp_dir) / "normalized_submission"
        normalization_result = normalization.normalize_preparation_submission(
            task=task,
            raw_submission_dir=sub_dir,
            normalized_submission_dir=normalized_dir,
        )
        if not normalization_result.get("success"):
            normalization_result["normalized_submission_dir"] = None
            return {
                "success": False,
                "task_id": task.task_id,
                "submission_dir": str(sub_dir),
                "errors": list(normalization_result.get("errors") or []),
                "warnings": list(normalization_result.get("warnings") or []),
                "missing_outputs": [],
                "hints": [],
                "normalization": normalization_result,
            }
        result = validation.validate_submission(task_path, normalized_dir)
    normalization_result["normalized_submission_dir"] = None
    result["raw_submission_dir"] = str(sub_dir)
    result["normalization"] = normalization_result
    return result


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

    Preparation submissions may be raw artifact bundles.  In that case the
    evaluator normalizes them into ``normalized_submission/`` before scoring so
    agents are not required to self-report metrics, hashes, or minimized PDBs.
    Returns a dict with the score payload and the path to score.json.
    """
    task_path = Path(task_file)
    sub_dir = Path(submission_dir)

    try:
        task = validation.load_task(task_path)
    except (ValidationError, json.JSONDecodeError, FileNotFoundError) as exc:
        return {"success": False, "errors": [f"task file invalid: {exc}"]}

    normalization_result: Optional[dict[str, Any]] = None
    scoring_submission_dir = sub_dir
    if task.primary_score == "preparation":
        normalized_dir = sub_dir.parent / "normalized_submission"
        normalization_result = normalization.normalize_preparation_submission(
            task=task,
            raw_submission_dir=sub_dir,
            normalized_submission_dir=normalized_dir,
            run_id=run_id,
        )
        if not normalization_result.get("success"):
            return {
                "success": False,
                "task_id": task.task_id,
                "submission_dir": str(sub_dir),
                "normalized_submission_dir": str(normalized_dir),
                "score_file": output_file,
                "score": None,
                "normalization": normalization_result,
                "errors": list(normalization_result.get("errors") or []),
                "warnings": list(normalization_result.get("warnings") or []),
            }
        scoring_submission_dir = normalized_dir

    return _score_loaded_benchmark_submission(
        task=task,
        task_path=task_path,
        submission_dir=sub_dir,
        scoring_submission_dir=scoring_submission_dir,
        run_id=run_id,
        output_file=output_file,
        llm_judge_file=llm_judge_file,
        harness_record_file=harness_record_file,
        normalization_result=normalization_result,
    )


def _score_loaded_benchmark_submission(
    *,
    task: Task,
    task_path: Path,
    submission_dir: Path,
    scoring_submission_dir: Path,
    run_id: str,
    output_file: Optional[str],
    llm_judge_file: Optional[str],
    harness_record_file: Optional[str],
    normalization_result: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Score an already-loaded task against its scorer-facing directory."""
    try:
        judge_payload = judge.load_judge_payload(llm_judge_file)
    except ValueError as exc:
        return {"success": False, "errors": [str(exc)]}

    score = scoring.score_submission(
        task=task,
        submission_dir=scoring_submission_dir,
        run_id=run_id,
        llm_judge_payload=judge_payload,
        task_dir=task_path.parent,
        harness_record_file=harness_record_file,
    )
    score_payload = score.model_dump()

    if output_file is None:
        output_file = str(
            submission_dir.parent / "score.json"
            if task.primary_score == "preparation"
            else submission_dir / "score.json"
        )
    out_path = Path(output_file)
    ensure_directory(out_path.parent)
    out_path.write_text(json.dumps(score_payload, indent=2, sort_keys=True,
                                   default=str) + "\n")

    return {
        "success": True,
        "task_id": score.task_id,
        "submission_dir": str(submission_dir),
        "normalized_submission_dir": (
            str(scoring_submission_dir)
            if scoring_submission_dir != submission_dir
            else None
        ),
        "score_file": str(out_path),
        "score": score_payload,
        "normalization": normalization_result,
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
    try:
        task = validation.load_task(task_file)
    except (ValidationError, json.JSONDecodeError, FileNotFoundError) as exc:
        validation_result = {
            "success": False,
            "task_id": None,
            "submission_dir": str(sub_dir),
            "errors": [f"task file invalid: {exc}"],
            "warnings": [],
            "missing_outputs": [],
        }
        task = None

    normalization_result: Optional[dict[str, Any]] = None
    scoring_submission_dir = sub_dir
    if task is not None and task.primary_score == "preparation":
        normalized_dir = sub_dir.parent / "normalized_submission"
        normalization_result = normalization.normalize_preparation_submission(
            task=task,
            raw_submission_dir=sub_dir,
            normalized_submission_dir=normalized_dir,
            run_id=run_id,
        )
        if normalization_result.get("success"):
            scoring_submission_dir = normalized_dir
        else:
            validation_result = {
                "success": False,
                "task_id": task.task_id,
                "submission_dir": str(sub_dir),
                "normalized_submission_dir": str(normalized_dir),
                "errors": list(normalization_result.get("errors") or []),
                "warnings": list(normalization_result.get("warnings") or []),
                "missing_outputs": [],
                "normalization": normalization_result,
            }
    if task is not None and (
        normalization_result is None or normalization_result.get("success")
    ):
        validation_result = validation.validate_submission(
            task_file,
            str(scoring_submission_dir),
        )
        if normalization_result is not None:
            validation_result["raw_submission_dir"] = str(sub_dir)
            validation_result["normalized_submission_dir"] = str(scoring_submission_dir)
            validation_result["normalization"] = normalization_result

    validation_file = None
    if validation_output_file:
        validation_path = Path(validation_output_file)
        ensure_directory(validation_path.parent)
        validation_path.write_text(
            json.dumps(validation_result, indent=2, sort_keys=True, default=str) + "\n"
        )
        validation_file = str(validation_path)

    normalization_failed = (
        normalization_result is not None
        and not normalization_result.get("success")
    )
    if (
        task is None
        or normalization_failed
        or (require_validation_success and not validation_result.get("success"))
    ):
        return {
            "success": False,
            "task_id": validation_result.get("task_id"),
            "submission_dir": str(sub_dir),
            "normalized_submission_dir": (
                str(scoring_submission_dir)
                if scoring_submission_dir != sub_dir
                else None
            ),
            "validation_success": bool(validation_result.get("success")),
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

    assert task is not None
    score_result = _score_loaded_benchmark_submission(
        task=task,
        task_path=Path(task_file),
        submission_dir=sub_dir,
        scoring_submission_dir=scoring_submission_dir,
        run_id=run_id,
        output_file=output_file,
        llm_judge_file=llm_judge_file,
        harness_record_file=harness_record_file,
        normalization_result=normalization_result,
    )
    if not score_result.get("success"):
        return {
            "success": False,
            "task_id": validation_result.get("task_id"),
            "submission_dir": str(sub_dir),
            "normalized_submission_dir": (
                str(scoring_submission_dir)
                if scoring_submission_dir != sub_dir
                else None
            ),
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
        "normalized_submission_dir": (
            str(scoring_submission_dir)
            if scoring_submission_dir != sub_dir
            else None
        ),
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
        "normalization": normalization_result,
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


def _public_package_readme(primary_scores: set[str]) -> str:
    """Build a suite-aware public-package README.

    Preparation tasks submit raw OpenMM artifacts that the evaluator normalizes;
    scientific-answer / evidence-bundle (study) tasks submit author-written
    manifest/metrics/provenance/evidence files that are scored as written. A
    mixed dataset gets both paragraphs.
    """
    has_prep = "preparation" in primary_scores
    has_study = bool(primary_scores & {"scientific_answer", "evidence_communication"})

    parts = [
        "# MD Benchmark Public Package\n",
        "This directory is safe to give to benchmark agents. It contains task "
        "prompts and submission-facing contracts only.\n",
        "Agents should read `tasks/<task_id>/prompt.md`, then use "
        "`tasks/<task_id>/submission_contract.json` and "
        "`tasks/<task_id>/submission_checklist.md` to build a `submission/` "
        "directory.\n",
    ]
    if has_prep:
        parts.append(
            "For preparation tasks, submit raw OpenMM artifacts: "
            "`topology/system.xml`, `topology/topology.pdb`, "
            "`topology/state.xml`, `prepared_structure.pdb`, and any "
            "task-specific raw files. The evaluator normalizes those artifacts "
            "into `manifest.json`, `metrics.json`, `provenance.json`, md5 "
            "hashes, `minimized_structure.pdb`, and `minimization_report.json`; "
            "do not hand-write those generated files. MDClaw helpers and "
            "`tools/package_submission.py` are optional convenience tools only. "
            "`tools/validate_submission.py` is a tool-neutral public preflight "
            "for the raw artifact contract.\n"
        )
    if has_study:
        parts.append(
            "For scientific-answer and evidence-bundle (study) tasks, you author "
            "the submission files yourself: `manifest.json`, `metrics.json`, "
            "`provenance.json`, and `evidence_report.json`, plus the artifacts "
            "your manifest references (comparative WT/mutant trajectories under "
            "`outputs.trajectories` with matching `outputs.topology`, and "
            "`methods.md` / `decision_log.jsonl` for evidence-bundle tasks). "
            "Unlike preparation tasks, these files are scored as written and are "
            "not regenerated by the evaluator. A scorer-side harness execution "
            "record (not solver-written provenance) supplies the trusted "
            "workflow-stage evidence.\n"
        )
    parts.append(
        "Agents must not be given evaluator-side `task.json`, `truth/`, or "
        "`scorer/` files from the canonical repository tree. The contract lists "
        "required outputs and task-specific requirements.\n"
    )
    parts.append(
        "Score submissions with the MDClaw benchmark scorer from a held-out "
        "private evaluator package exported by "
        "`mdclaw export_benchmark_private_package`, or from a canonical dataset "
        "checkout that was never mounted into the solver workspace.\n"
    )
    return "\n".join(parts)


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

        tool_files: list[str] = []
        if _STANDALONE_PACKAGER_SOURCE.is_file():
            packager_dest = staging / _STANDALONE_PACKAGER_RELATIVE
            packager_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_STANDALONE_PACKAGER_SOURCE, packager_dest)
            tool_files.append(str(dest / _STANDALONE_PACKAGER_RELATIVE))
        if _STANDALONE_PREFLIGHT_SOURCE.is_file():
            preflight_dest = staging / _STANDALONE_PREFLIGHT_RELATIVE
            preflight_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_STANDALONE_PREFLIGHT_SOURCE, preflight_dest)
            tool_files.append(str(dest / _STANDALONE_PREFLIGHT_RELATIVE))

        task_files: list[str] = []
        primary_scores: set[str] = set()
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
            primary_scores.add(task.primary_score)

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

        schemas_dir = staging / "schemas"
        schemas_dir.mkdir()
        schema_names = ["score.schema.json"]
        if primary_scores - {"preparation"}:
            schema_names.insert(0, "submission_manifest.schema.json")
        schema_files = []
        for name in schema_names:
            src = source / "schemas" / name
            if src.is_file():
                shutil.copy2(src, schemas_dir / name)
                schema_files.append(str(dest / "schemas" / name))

        readme = staging / "README.md"
        readme.write_text(_public_package_readme(primary_scores))

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
        + tool_files
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
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _parse_raw_extra_output_files(
    specs: Optional[list[str]],
) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Parse ``relative_path=source_path`` task-specific raw artifacts."""
    parsed: list[tuple[Path, Path]] = []
    errors: list[str] = []
    seen: set[str] = set()
    reserved = {
        "topology/system.xml",
        "topology/topology.pdb",
        "topology/state.xml",
        "prepared_structure.pdb",
        "manifest.json",
        "metrics.json",
        "provenance.json",
        "minimized_structure.pdb",
        "minimization_report.json",
        "evidence_report.json",
        "command_log.json",
        "harness_execution.json",
    }
    for spec in specs or []:
        if "=" not in spec:
            errors.append(
                "extra_output_files entries must be relative_path=source_path: "
                f"{spec}"
            )
            continue
        raw_relative, raw_source = spec.split("=", 1)
        relative = Path(raw_relative.strip())
        relative_text = relative.as_posix()
        source = Path(raw_source.strip()).expanduser()
        if (
            relative_text in {"", "."}
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            errors.append(f"invalid extra output relative path: {raw_relative!r}")
            continue
        if relative_text in reserved or relative_text in seen:
            errors.append(f"duplicate or reserved extra output path: {relative_text}")
            continue
        if not source.is_file():
            errors.append(f"extra output file not found for {relative_text}: {source}")
            continue
        seen.add(relative_text)
        parsed.append((relative, source))
    return parsed, errors


def _write_raw_preparation_submission(
    *,
    submission_dir: str,
    task_id: str,
    system_xml_file: Path,
    topology_pdb_file: Path,
    state_xml_file: Path,
    prepared_structure_file: Path,
    extra_output_files: Optional[list[str]],
) -> dict[str, Any]:
    """Atomically copy the MDPrepBench v0.3 raw artifact contract."""
    sub = Path(submission_dir).resolve()
    core_sources = {
        Path("topology/system.xml"): system_xml_file,
        Path("topology/topology.pdb"): topology_pdb_file,
        Path("topology/state.xml"): state_xml_file,
    }
    errors = [
        f"input file not found: {source}"
        for source in [*core_sources.values(), prepared_structure_file]
        if not source.is_file()
    ]
    extras, extra_errors = _parse_raw_extra_output_files(extra_output_files)
    errors.extend(extra_errors)
    if errors:
        return {
            "success": False,
            "task_id": task_id,
            "submission_dir": str(sub),
            "errors": errors,
        }

    if sub.exists() and not sub.is_dir():
        return {
            "success": False,
            "task_id": task_id,
            "submission_dir": str(sub),
            "errors": [f"submission_dir exists and is not a directory: {sub}"],
        }

    ensure_directory(sub.parent)
    staging = Path(tempfile.mkdtemp(prefix=f".{sub.name}.", dir=str(sub.parent)))
    try:
        for relative, source in core_sources.items():
            _copy_if_different(source, staging / relative)
        _copy_if_different(
            prepared_structure_file,
            staging / "prepared_structure.pdb",
        )
        for relative, source in extras:
            _copy_if_different(source, staging / relative)
        if sub.exists():
            shutil.rmtree(sub)
        staging.rename(sub)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    files_written = [
        str(sub / "topology/system.xml"),
        str(sub / "topology/topology.pdb"),
        str(sub / "topology/state.xml"),
        str(sub / "prepared_structure.pdb"),
        *(str(sub / relative) for relative, _ in extras),
    ]
    return {
        "success": True,
        "task_id": task_id,
        "submission_dir": str(sub),
        "files_written": files_written,
        "warnings": [],
        "errors": [],
    }


def package_openmm_submission(
    submission_dir: str,
    task_id: str,
    system_xml_file: str,
    topology_pdb_file: str,
    state_xml_file: str,
    prepared_structure_file: str,
    extra_output_files: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Copy an existing OpenMM bundle into a raw MDPrepBench v0.3 submission.

    This helper does not create manifest, metrics, provenance, evidence, hashes,
    timing, or minimization summaries. The benchmark evaluator owns all derived
    metadata. ``state_xml_file`` must already contain the minimized state.

    ``extra_output_files`` entries use ``relative_path=source_path`` for raw
    task-specific artifacts such as ``wt_prepared_structure.pdb``.
    """
    return _write_raw_preparation_submission(
        submission_dir=submission_dir,
        task_id=task_id,
        system_xml_file=Path(system_xml_file),
        topology_pdb_file=Path(topology_pdb_file),
        state_xml_file=Path(state_xml_file),
        prepared_structure_file=Path(prepared_structure_file),
        extra_output_files=extra_output_files,
    )


def package_mdprep_submission(
    submission_dir: str,
    task_id: str,
    job_dir: str,
    node_id: str,
    prepared_structure_file: Optional[str] = None,
    extra_output_files: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Export raw MDPrepBench v0.3 artifacts from a completed MDClaw min node."""
    from mdclaw.node.graph import find_ancestor_artifact
    from mdclaw.node.io import _read_artifact_from_node
    from mdclaw.node.lifecycle import read_node

    job = Path(job_dir).resolve()
    try:
        node = read_node(str(job), node_id)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "success": False,
            "task_id": task_id,
            "submission_dir": str(Path(submission_dir).resolve()),
            "errors": [f"could not read DAG node {node_id}: {exc}"],
        }
    if node.get("node_type") != "min" or node.get("status") != "completed":
        return {
            "success": False,
            "task_id": task_id,
            "submission_dir": str(Path(submission_dir).resolve()),
            "errors": [
                "package_mdprep_submission requires a completed min node; "
                f"found node_type={node.get('node_type')!r}, "
                f"status={node.get('status')!r}"
            ],
        }
    system = find_ancestor_artifact(str(job), node_id, "topo", "system_xml")
    topology = find_ancestor_artifact(str(job), node_id, "topo", "topology_pdb")
    state = _read_artifact_from_node(str(job), node_id, "state")
    prepared = prepared_structure_file or (
        find_ancestor_artifact(str(job), node_id, "prep", "merged_pdb")
        or find_ancestor_artifact(str(job), node_id, "prep", "prepared_pdb")
    )
    missing = [
        label
        for label, value in (
            ("topo.system_xml", system),
            ("topo.topology_pdb", topology),
            ("min.state", state),
            ("prepared_structure", prepared),
        )
        if not isinstance(value, str) or not value
    ]
    if missing:
        return {
            "success": False,
            "task_id": task_id,
            "submission_dir": str(Path(submission_dir).resolve()),
            "errors": [f"DAG artifact not found: {label}" for label in missing],
        }

    result = _write_raw_preparation_submission(
        submission_dir=submission_dir,
        task_id=task_id,
        system_xml_file=Path(str(system)),
        topology_pdb_file=Path(str(topology)),
        state_xml_file=Path(str(state)),
        prepared_structure_file=Path(str(prepared)),
        extra_output_files=extra_output_files,
    )
    if result.get("success"):
        result["mdclaw_dag"] = {"job_dir": str(job), "min_node_id": node_id}
    return result
