"""Structural source bundle helpers.

Source nodes normalize flexible inputs into a strict execution contract:

``source_bundle.json`` plus ``artifacts/candidates/candidate_*.{pdb,cif}``.

Raw inputs may remain in the source node for provenance, but prep consumes a
single candidate file selected from the bundle. This keeps PDB/mmCIF files,
NMR-style multi-model structures, and generated ensembles on the same path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

SOURCE_BUNDLE_SCHEMA_VERSION = 1

_SELECTION_ID_KEYS = (
    "structure_id",
    "source_structure_id",
    "candidate_id",
    "source_candidate_id",
)
_MODEL_ANNOTATION_KEYS = ("models", "model_metadata", "per_model")


def _safe_id(value: Any, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip())
    text = text.strip("._-")
    return text or fallback


def _infer_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".cif":
        return "cif"
    if suffix in {".pdb", ".ent"}:
        return "pdb"
    return suffix.lstrip(".") or "unknown"


def _candidate_suffix(path: Path) -> str:
    fmt = _infer_format(path)
    if fmt == "cif":
        return ".cif"
    if fmt == "pdb":
        return ".pdb"
    return path.suffix or ".pdb"


def _node_relative(path: Path, node_dir: Path) -> str:
    try:
        return os.path.relpath(path.resolve(strict=False), node_dir.resolve(strict=False))
    except ValueError:
        return str(path)


def _gemmi_models(path: Path) -> list[tuple[int, str]]:
    try:
        import gemmi
    except ImportError:
        return []

    try:
        if path.suffix.lower() == ".cif":
            doc = gemmi.cif.read(str(path))
            structure = gemmi.make_structure_from_block(doc[0])
        else:
            structure = gemmi.read_pdb(str(path))
    except Exception:
        return []

    models: list[tuple[int, str]] = []
    for idx, model in enumerate(structure):
        model_id = str(getattr(model, "num", "") or idx + 1)
        models.append((idx, model_id))
    return models


def _read_structure(path: Path):
    try:
        import gemmi
    except ImportError as exc:
        raise ValueError("gemmi library not installed; cannot split source models") from exc

    if path.suffix.lower() == ".cif":
        doc = gemmi.cif.read(str(path))
        return gemmi.make_structure_from_block(doc[0]), gemmi
    return gemmi.read_pdb(str(path)), gemmi


def _write_single_model(path: Path, model_index: int, out_file: Path) -> None:
    structure, gemmi = _read_structure(path)
    if model_index < 0 or model_index >= len(structure):
        raise ValueError(f"model_index {model_index} outside 0..{len(structure) - 1}")

    new_structure = gemmi.Structure()
    new_structure.name = structure.name
    new_structure.cell = structure.cell
    new_structure.spacegroup_hm = structure.spacegroup_hm
    model = structure[model_index].clone()
    if hasattr(model, "num"):
        model.num = "1"
    new_structure.add_model(model)
    try:
        new_structure.setup_entities()
    except Exception:
        pass

    out_file.parent.mkdir(parents=True, exist_ok=True)
    if out_file.suffix.lower() == ".cif":
        new_structure.make_mmcif_document().write_file(str(out_file))
    else:
        new_structure.write_pdb(str(out_file))


def _copy_candidate(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve(strict=False) == dst.resolve(strict=False):
        return
    shutil.copy2(src, dst)


def _append_candidate_record(
    *,
    structures: list[dict[str, Any]],
    source_node_dir: Path,
    candidate_file: Path,
    raw_file: Path,
    source_type: str,
    input_index: int,
    model_index: int | None = None,
    model_id: str | None = None,
    annotation: dict[str, Any] | None = None,
) -> None:
    candidate_id = f"candidate_{len(structures) + 1:03d}"
    rel_candidate = _node_relative(candidate_file, source_node_dir)
    rel_raw = _node_relative(raw_file, source_node_dir)
    origin: dict[str, Any] = {
        "kind": source_type,
        "input_index": input_index,
        "raw_file": rel_raw,
    }
    if model_index is not None:
        origin["model_index"] = model_index
        origin["model_rank"] = model_index + 1
    if model_id is not None:
        origin["model_id"] = model_id

    annotation = annotation or {}
    if isinstance(annotation.get("origin"), dict):
        origin.update(annotation["origin"])

    record = {
        "structure_id": candidate_id,
        "candidate_id": candidate_id,
        "path": rel_candidate,
        "file": rel_candidate,
        "candidate_file": rel_candidate,
        "raw_file": rel_raw,
        "format": _infer_format(candidate_file),
        "rank": len(structures) + 1,
        "is_primary": not structures,
        "storage_mode": "candidate_file",
        "requires_materialization": False,
        "origin": origin,
    }
    for key in ("label", "description", "metrics", "scores", "tags"):
        if key in annotation:
            record[key] = annotation[key]
    structures.append(record)


def _merge_annotation_dicts(
    base: dict[str, Any] | None,
    overlay: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not base and not overlay:
        return None
    merged = {
        key: value
        for key, value in (base or {}).items()
        if key not in _MODEL_ANNOTATION_KEYS
    }
    for key, value in (overlay or {}).items():
        if (
            key in {"origin", "metrics", "scores"}
            and isinstance(merged.get(key), dict)
            and isinstance(value, dict)
        ):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _model_specific_annotation(
    annotation: dict[str, Any] | None,
    *,
    model_index: int | None,
    model_id: str | None,
) -> dict[str, Any] | None:
    if not annotation:
        return None

    selected: dict[str, Any] | None = None
    for key in _MODEL_ANNOTATION_KEYS:
        per_model = annotation.get(key)
        if isinstance(per_model, list) and model_index is not None:
            if 0 <= model_index < len(per_model) and isinstance(per_model[model_index], dict):
                selected = per_model[model_index]
                break
        if isinstance(per_model, dict):
            lookup_keys = []
            if model_index is not None:
                lookup_keys.extend([model_index, str(model_index), model_index + 1, str(model_index + 1)])
            if model_id is not None:
                lookup_keys.append(str(model_id))
            for lookup_key in lookup_keys:
                value = per_model.get(lookup_key)
                if isinstance(value, dict):
                    selected = value
                    break
            if selected:
                break

    return _merge_annotation_dicts(annotation, selected)


def build_source_bundle(
    *,
    source_type: str,
    source_id: str,
    structure_paths: list[Path],
    source_node_dir: Path,
    metadata: dict[str, Any] | None = None,
    candidate_metadata: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalize source inputs into candidate files and return bundle metadata."""
    if not structure_paths:
        raise ValueError("structure_paths must contain at least one path")

    source_node_dir = source_node_dir.resolve()
    candidates_dir = source_node_dir / "artifacts" / "candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)

    structures: list[dict[str, Any]] = []
    metadata_out = dict(metadata or {})
    candidate_metadata_provided = candidate_metadata is not None
    candidate_metadata = candidate_metadata or []
    if candidate_metadata_provided and len(candidate_metadata) != len(structure_paths):
        metadata_out.setdefault("warnings", []).append(
            "candidate_metadata length does not match structure_paths; "
            "missing entries were filled with source-file provenance only."
        )
    for input_index, raw_path in enumerate(structure_paths):
        annotation = (
            candidate_metadata[input_index]
            if input_index < len(candidate_metadata)
            else None
        )
        raw_path = Path(raw_path).expanduser().resolve()
        if not raw_path.is_file():
            raise ValueError(f"source structure file does not exist: {raw_path}")

        models = _gemmi_models(raw_path)
        if len(models) > 1:
            for model_index, model_id in models:
                model_annotation = _model_specific_annotation(
                    annotation,
                    model_index=model_index,
                    model_id=model_id,
                )
                candidate_id = f"candidate_{len(structures) + 1:03d}"
                candidate_file = candidates_dir / f"{candidate_id}{_candidate_suffix(raw_path)}"
                _write_single_model(raw_path, model_index, candidate_file)
                _append_candidate_record(
                    structures=structures,
                    source_node_dir=source_node_dir,
                    candidate_file=candidate_file,
                    raw_file=raw_path,
                    source_type=source_type,
                    input_index=input_index,
                    model_index=model_index,
                    model_id=model_id,
                    annotation=model_annotation,
                )
        else:
            model_index = 0 if len(models) == 1 else None
            model_id = models[0][1] if len(models) == 1 else None
            model_annotation = _model_specific_annotation(
                annotation,
                model_index=model_index,
                model_id=model_id,
            )
            candidate_id = f"candidate_{len(structures) + 1:03d}"
            candidate_file = candidates_dir / f"{candidate_id}{_candidate_suffix(raw_path)}"
            _copy_candidate(raw_path, candidate_file)
            _append_candidate_record(
                structures=structures,
                source_node_dir=source_node_dir,
                candidate_file=candidate_file,
                raw_file=raw_path,
                source_type=source_type,
                input_index=input_index,
                model_index=model_index,
                model_id=model_id,
                annotation=model_annotation,
            )

    return {
        "schema_version": SOURCE_BUNDLE_SCHEMA_VERSION,
        "source_type": source_type,
        "source_id": source_id,
        "storage_contract": "candidate_files",
        "structures": structures,
        "metadata": metadata_out,
    }


def write_source_bundle(source_node_dir: Path, bundle: dict[str, Any]) -> str:
    """Write ``source_bundle.json`` under a source node's artifacts directory."""
    out = source_node_dir / "artifacts" / "source_bundle.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, indent=2, default=str) + "\n")
    return _node_relative(out, source_node_dir)


def _select_frame_indices(
    n_frames: int,
    *,
    max_candidates: int | None = None,
    subsample_strategy: str = "uniform",
) -> list[int]:
    if n_frames <= 0:
        raise ValueError("trajectory contains no frames")
    if max_candidates is None or max_candidates <= 0 or max_candidates >= n_frames:
        return list(range(n_frames))

    if subsample_strategy == "first_n":
        return list(range(max_candidates))
    if subsample_strategy == "stride":
        stride = max(1, n_frames // max_candidates)
        return list(range(0, n_frames, stride))[:max_candidates]
    if subsample_strategy == "uniform":
        if max_candidates == 1:
            return [0]
        return [
            round(i * (n_frames - 1) / (max_candidates - 1))
            for i in range(max_candidates)
        ]
    raise ValueError(
        "subsample_strategy must be one of: uniform, stride, first_n"
    )


def candidate_paths_from_trajectory(
    topology_path: str | Path,
    trajectory_path: str | Path,
    candidates_dir: str | Path,
    *,
    max_candidates: int | None = None,
    subsample_strategy: str = "uniform",
    output_format: str = "pdb",
) -> tuple[list[Path], list[int]]:
    """Split an MDTraj-readable trajectory into per-frame candidate files."""
    if output_format != "pdb":
        raise ValueError("candidate trajectory export currently supports only output_format='pdb'")

    try:
        import mdtraj as md
    except ImportError as exc:
        raise ValueError("mdtraj library not installed; cannot split trajectory candidates") from exc

    topology_path = Path(topology_path).expanduser().resolve()
    trajectory_path = Path(trajectory_path).expanduser().resolve()
    candidates_dir = Path(candidates_dir).expanduser().resolve()
    if not topology_path.is_file():
        raise ValueError(f"topology file does not exist: {topology_path}")
    if not trajectory_path.is_file():
        raise ValueError(f"trajectory file does not exist: {trajectory_path}")

    traj = md.load(str(trajectory_path), top=str(topology_path))
    frame_indices = _select_frame_indices(
        traj.n_frames,
        max_candidates=max_candidates,
        subsample_strategy=subsample_strategy,
    )

    candidates_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for out_idx, frame_idx in enumerate(frame_indices, start=1):
        out_path = candidates_dir / f"candidate_{out_idx:03d}.pdb"
        traj[frame_idx].save_pdb(str(out_path))
        paths.append(out_path)
    return paths, frame_indices


def load_source_bundle(bundle_file: str | Path) -> dict[str, Any]:
    bundle = json.loads(Path(bundle_file).read_text())
    version = bundle.get("schema_version")
    if version != SOURCE_BUNDLE_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported source_bundle schema_version {version!r}; "
            f"expected {SOURCE_BUNDLE_SCHEMA_VERSION}"
        )
    structures = bundle.get("structures")
    if not isinstance(structures, list) or not structures:
        raise ValueError("source_bundle must contain at least one structure")
    return bundle


def source_selection_from_values(
    *,
    source_structure_id: str | None = None,
    source_candidate_id: str | None = None,
    source_model_index: int | None = None,
    source_model_id: str | None = None,
) -> dict[str, Any]:
    """Build a source-selection dict from explicit prepare_complex inputs."""
    selection: dict[str, Any] = {}
    if source_structure_id:
        selection["structure_id"] = source_structure_id
    if source_candidate_id:
        selection["candidate_id"] = source_candidate_id
    if source_model_index is not None:
        selection["model_index"] = source_model_index
    if source_model_id:
        selection["model_id"] = source_model_id
    return selection


def select_source_structure(
    bundle: dict[str, Any],
    selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select exactly one candidate record from a source bundle."""
    structures = [s for s in bundle.get("structures", []) if isinstance(s, dict)]
    if not structures:
        raise ValueError("source_bundle contains no candidate structure records")

    selection = selection or {}
    requested_id = None
    for key in _SELECTION_ID_KEYS:
        if selection.get(key):
            requested_id = str(selection[key])
            break

    if requested_id:
        for record in structures:
            ids = {
                str(record.get("structure_id", "")),
                str(record.get("candidate_id", "")),
            }
            if requested_id in ids:
                return record
        raise ValueError(f"source candidate {requested_id!r} was not found in source_bundle")

    if selection.get("model_id") is not None:
        requested_model = str(selection["model_id"])
        for record in structures:
            origin = record.get("origin") or {}
            if str(origin.get("model_id")) == requested_model:
                return record
        raise ValueError(f"model_id {requested_model!r} was not found in source_bundle")

    if selection.get("model_index") is not None:
        requested_index = int(selection["model_index"])
        for get_value in (
            lambda record: (record.get("origin") or {}).get("model_index"),
            lambda record: (record.get("origin") or {}).get("model_rank"),
            lambda record: record.get("rank"),
        ):
            for record in structures:
                if get_value(record) == requested_index:
                    return record
        raise ValueError(f"model_index {requested_index!r} was not found in source_bundle")

    if len(structures) == 1:
        return structures[0]

    options = [str(s.get("structure_id")) for s in structures]
    raise ValueError(
        "source_bundle contains multiple candidate structures; pass "
        f"source_structure_id or source_model_index. Options: {options}"
    )


def source_record_path(record: dict[str, Any], source_node_dir: Path) -> Path:
    candidate_file = record.get("candidate_file") or record.get("file") or record.get("path")
    if not candidate_file:
        raise ValueError("source candidate record has no candidate_file/file/path")
    path = Path(str(candidate_file)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (source_node_dir / path).resolve()


def materialize_source_selection(
    *,
    bundle_file: str | Path,
    selection: dict[str, Any] | None,
    prep_artifacts_dir: str | Path,
) -> dict[str, Any]:
    """Resolve a source bundle selection to a concrete candidate file for prep."""
    bundle_path = Path(bundle_file).expanduser().resolve()
    bundle = load_source_bundle(bundle_path)
    record = select_source_structure(bundle, selection)
    source_node_dir = bundle_path.parent.parent
    candidate_path = source_record_path(record, source_node_dir)
    if not candidate_path.exists():
        raise ValueError(f"selected source candidate file does not exist: {candidate_path}")

    out_dir = Path(prep_artifacts_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    normalized_selection = dict(selection or {})
    normalized_selection.setdefault("structure_id", record.get("structure_id"))
    selection_record = {
        "schema_version": SOURCE_BUNDLE_SCHEMA_VERSION,
        "source_bundle": str(bundle_path),
        "selected_structure": record,
        "selection": normalized_selection,
    }
    selection_file = out_dir / "source_selection.json"
    selection_file.write_text(json.dumps(selection_record, indent=2, default=str))

    return {
        "success": True,
        "structure_file": str(candidate_path),
        "source_bundle_file": str(bundle_path),
        "source_selection_file": str(selection_file),
        "selected_structure": record,
        "source_selection": normalized_selection,
        "materialized": False,
    }
