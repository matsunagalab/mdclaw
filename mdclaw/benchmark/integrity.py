"""Integrity checks for MD benchmark submissions.

These functions are the difference between v0.1 (JSON-trust) and v1.0
(re-verify): they re-hash claimed files, re-load trajectories, and check
internal consistency between manifest, metrics, and provenance.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Optional


_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)
_PMID_RE = re.compile(r"\bPMID\s*:?\s*\d+\b", re.IGNORECASE)
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_DCD_MAGIC = b"\x54\x00\x00\x00CORD"


def hash_file(path: str | Path, algorithm: str = "md5") -> Optional[str]:
    """Stream-hash a file. Returns ``None`` if the file does not exist."""
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.new(algorithm)
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_provenance_hashes(
    submission_dir: Path, provenance: dict[str, Any]
) -> list[str]:
    """Re-hash files referenced in ``provenance.scripts`` and
    ``provenance.raw_outputs``. Returns a list of warning strings, empty when
    every reported md5 matches the recomputed one.

    Paths in provenance are relative to the submission directory; entries that
    point outside the submission tree (``../work/...``) are resolved against
    the submission's parent directory.
    """
    warnings: list[str] = []
    for section in ("scripts", "raw_outputs"):
        entries = provenance.get(section) or []
        if not isinstance(entries, list):
            continue
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            rel = entry.get("path")
            md5_claim = entry.get("md5")
            if not rel or not md5_claim:
                continue
            target = (submission_dir / rel).resolve()
            actual = hash_file(target)
            if actual is None:
                warnings.append(
                    f"provenance.{section}[{i}].path={rel!r}: file not found at {target}"
                )
                continue
            if actual != md5_claim:
                warnings.append(
                    f"provenance.{section}[{i}].path={rel!r}: md5 mismatch "
                    f"(claimed={md5_claim}, actual={actual})"
                )
    return warnings


def required_raw_output_hash_warnings(
    submission_dir: Path,
    manifest: dict[str, Any],
    provenance: dict[str, Any],
    *,
    require_manifest_outputs: bool = True,
) -> list[str]:
    """Require ``provenance.raw_outputs`` hashes for submitted artifacts.

    The benchmark normalizer or optional packagers write these hashes before
    scoring. The scorer then re-hashes the final bytes, which catches the
    common failure mode where files are edited after normalization.

    ``provenance.json`` itself is intentionally excluded because it cannot
    carry a stable hash of itself.
    """
    warnings: list[str] = []
    expected: set[str] = {"manifest.json"}
    if require_manifest_outputs:
        outputs = manifest.get("outputs") or {}
        if isinstance(outputs, dict):
            for dotted, rel in _manifest_output_paths(outputs):
                if dotted == "provenance" or rel == "provenance.json":
                    continue
                if isinstance(rel, str) and rel.strip():
                    expected.add(Path(rel).as_posix())

    raw_outputs = provenance.get("raw_outputs")
    if not isinstance(raw_outputs, list):
        warnings.append(
            "provenance.raw_outputs is missing or not a list; cannot verify "
            "submitted artifact hashes"
        )
        return warnings

    claims: dict[str, str] = {}
    for index, entry in enumerate(raw_outputs):
        if not isinstance(entry, dict):
            warnings.append(f"provenance.raw_outputs[{index}] is not an object")
            continue
        rel = entry.get("path")
        md5_claim = entry.get("md5")
        if not isinstance(rel, str) or not rel.strip():
            warnings.append(f"provenance.raw_outputs[{index}] missing path")
            continue
        if not isinstance(md5_claim, str) or not md5_claim.strip():
            warnings.append(f"provenance.raw_outputs[{index}] missing md5")
            continue
        claims[Path(rel).as_posix()] = md5_claim

    missing = sorted(expected - set(claims))
    if missing:
        warnings.append(
            "provenance.raw_outputs missing md5 for submitted artifact(s): "
            + ", ".join(missing)
        )
    return warnings


def openmm_minimized_state_consistency_warnings(
    submission_dir: Path,
    manifest: dict[str, Any],
    *,
    coordinate_tolerance_nm: float = 1.0e-3,
) -> list[str]:
    """Check that ``minimized_structure.pdb`` matches topology ``state.xml``.

    MDPrepBench uses the OpenMM ``system.xml`` + ``topology.pdb`` +
    ``state.xml`` triple as the topology/coordinate contract.  The submitted
    ``minimized_structure.pdb`` must be a PDB view of the same state
    coordinates; otherwise an agent can mix a topology-time state with an
    unrelated minimized PDB/report.
    """
    outputs = manifest.get("outputs") or {}
    if not isinstance(outputs, dict):
        return ["manifest.outputs is not an object"]

    minimized_rel = _safe_path(manifest, "outputs.minimized_structure")
    topology_rels = _safe_path(manifest, "outputs.topology")
    if not isinstance(minimized_rel, str):
        return ["outputs.minimized_structure is missing or not a path"]
    if isinstance(topology_rels, str):
        topology_rels = [topology_rels]
    if not isinstance(topology_rels, list):
        return ["outputs.topology is missing or not a list"]

    state_rel: str | None = None
    for rel in topology_rels:
        if not isinstance(rel, str):
            continue
        name = Path(rel).name.lower()
        if name == "state.xml" or name.endswith(".state.xml"):
            state_rel = rel
            break
    if state_rel is None:
        return ["outputs.topology does not include a state.xml artifact"]

    minimized_path = (submission_dir / minimized_rel).resolve()
    state_path = (submission_dir / state_rel).resolve()
    if not minimized_path.is_file():
        return [f"minimized_structure.pdb not found: {minimized_path}"]
    if not state_path.is_file():
        return [f"state.xml not found: {state_path}"]

    try:
        from openmm import XmlSerializer, unit
        from openmm.app import PDBFile
    except Exception as exc:  # noqa: BLE001
        return [f"OpenMM import failed for state/PDB consistency: {exc}"]

    try:
        pdb = PDBFile(str(minimized_path))
        state = XmlSerializer.deserialize(state_path.read_text())
        state_positions = state.getPositions()
        if state_positions is None:
            return ["state.xml does not contain positions"]
        pdb_positions_nm = pdb.positions.value_in_unit(unit.nanometer)
        state_positions_nm = state_positions.value_in_unit(unit.nanometer)
    except Exception as exc:  # noqa: BLE001
        return [
            "failed to load minimized_structure.pdb/state.xml for consistency "
            f"check: {type(exc).__name__}: {exc}"
        ]

    if len(pdb_positions_nm) != len(state_positions_nm):
        return [
            "minimized_structure/state atom count mismatch: "
            f"pdb={len(pdb_positions_nm)}, state={len(state_positions_nm)}"
        ]

    max_delta = 0.0
    for pdb_pos, state_pos in zip(pdb_positions_nm, state_positions_nm):
        dx = float(pdb_pos[0]) - float(state_pos[0])
        dy = float(pdb_pos[1]) - float(state_pos[1])
        dz = float(pdb_pos[2]) - float(state_pos[2])
        delta = math.sqrt(dx * dx + dy * dy + dz * dz)
        if not math.isfinite(delta):
            return ["minimized_structure/state coordinate comparison is non-finite"]
        if delta > max_delta:
            max_delta = delta
    if max_delta > coordinate_tolerance_nm:
        return [
            "minimized_structure.pdb is not exported from topology/state.xml: "
            f"max coordinate delta {max_delta:.6g} nm > "
            f"{coordinate_tolerance_nm:.6g} nm"
        ]
    return []


def rescan_trajectory_for_nan(
    trajectory_path: Path, topology_path: Path
) -> tuple[Optional[int], bool, str]:
    """Load a trajectory with mdtraj and check for NaN coordinates.

    Returns ``(n_frames, any_nan, message)``. n_frames is None if the load
    failed. any_nan is True if any frame contains NaN xyz. message describes
    the outcome (used in CheckResult).
    """
    try:
        import mdtraj as md  # imported lazily so non-MD environments still work
    except ImportError:
        return None, False, "mdtraj not available; cannot rescan trajectory"

    try:
        traj = md.load(str(trajectory_path), top=str(topology_path))
    except Exception as exc:  # pragma: no cover -- depends on file content
        return None, False, f"trajectory load failed: {exc}"

    n_frames = int(traj.n_frames)
    if n_frames == 0:
        return 0, False, "trajectory has zero frames"

    import numpy as np

    has_nan = bool(np.isnan(traj.xyz).any())
    return n_frames, has_nan, f"loaded {n_frames} frames, any_nan={has_nan}"


def recompute_ligand_rmsd(
    prepared_structure: Path,
    reference_pdb: Path,
    selection: str,
    align_selection: Optional[str] = None,
    image_molecules: bool = False,
) -> tuple[Optional[float], str]:
    """Recompute heavy-atom RMSD of a ligand selection between two PDB-like
    structures. Returns ``(rmsd_angstrom, message)`` with rmsd None on error.
    """
    try:
        import mdtraj as md
        import numpy as np
    except ImportError:
        return None, "mdtraj/numpy not available; cannot recompute RMSD"

    try:
        prepared = md.load(str(prepared_structure))
        reference = md.load(str(reference_pdb))
    except Exception as exc:
        return None, f"load failed: {exc}"

    # Re-imaging can be useful for some PBC ligand-pose exports, but mdtraj's
    # image_molecules path is a compiled routine and has crashed on sparse or
    # synthetic structures. Keep it opt-in so protein/model/assembly checks stay
    # robust and deterministic.
    reimaged = False
    if image_molecules and prepared.unitcell_lengths is not None:
        try:
            prepared.image_molecules(inplace=True)
            reimaged = True
        except Exception:
            pass

    try:
        prep_idx = prepared.topology.select(selection)
        ref_idx = reference.topology.select(selection)
    except Exception as exc:
        return None, f"selection {selection!r} failed: {exc}"

    if len(prep_idx) == 0 or len(ref_idx) == 0:
        return None, (
            f"selection {selection!r} matched 0 atoms "
            f"(prepared={len(prep_idx)}, reference={len(ref_idx)})"
        )
    if len(prep_idx) != len(ref_idx):
        return None, (
            f"selection mismatch: prepared has {len(prep_idx)} atoms, "
            f"reference has {len(ref_idx)}"
        )

    if align_selection:
        try:
            prep_align = prepared.topology.select(align_selection)
            ref_align = reference.topology.select(align_selection)
        except Exception as exc:
            return None, f"align selection {align_selection!r} failed: {exc}"
        if len(prep_align) and len(prep_align) == len(ref_align):
            prepared.superpose(reference, atom_indices=prep_align,
                               ref_atom_indices=ref_align)

    diff = prepared.xyz[0, prep_idx] - reference.xyz[0, ref_idx]
    rmsd_nm = float(np.sqrt(np.mean(np.sum(diff * diff, axis=-1))))
    suffix = ", re-imaged" if reimaged else ""
    return rmsd_nm * 10.0, f"recomputed rmsd over {len(prep_idx)} atoms{suffix}"


def metrics_caption_consistency(
    metrics: dict[str, Any],
    captions: list[str],
    relative_tolerance: float = 0.01,
) -> tuple[bool, list[str]]:
    """Extract numeric values from caption strings and check that each one
    can be matched to some value in ``metrics`` within ``relative_tolerance``.

    This is a heuristic: we extract floats from each caption and require
    that every extracted value has at least one near-match in the flattened
    metrics dictionary. Any caption with no extracted floats is skipped.
    """
    flat_values = list(_flatten_floats(metrics))
    issues: list[str] = []
    for i, caption in enumerate(captions):
        for token in re.findall(r"-?\d+\.\d+|-?\d+", str(caption)):
            try:
                val = float(token)
            except ValueError:
                continue
            if not flat_values:
                issues.append(f"caption[{i}] cites {val} but metrics has no numeric values")
                continue
            ok = False
            for ref in flat_values:
                if ref == 0:
                    if abs(val) <= relative_tolerance:
                        ok = True
                        break
                else:
                    if abs(val - ref) / abs(ref) <= relative_tolerance:
                        ok = True
                        break
            if not ok:
                issues.append(
                    f"caption[{i}] cites {val} which is not within "
                    f"{relative_tolerance*100:.1f}% of any metrics value"
                )
    return (len(issues) == 0), issues


def manifest_metrics_consistency(
    manifest: dict[str, Any], metrics: dict[str, Any]
) -> list[str]:
    """Cross-check claims in metrics.json against artifact references in
    manifest.json. Returns a list of warning strings.
    """
    warnings: list[str] = []
    exec_completed = _safe_path(metrics, "execution.completed")
    trajectories = (manifest.get("outputs") or {}).get("trajectories") or []
    if exec_completed is True and not trajectories:
        warnings.append(
            "metrics.execution.completed=true but manifest.outputs.trajectories is empty"
        )

    no_nan_claim = _safe_path(metrics, "execution.no_nan")
    samples = _safe_path(metrics, "execution.energy_samples_kjmol")
    if no_nan_claim is True and isinstance(samples, list):
        bad = [v for v in samples if isinstance(v, (int, float)) and not math.isfinite(v)]
        if bad:
            warnings.append(
                f"metrics.execution.no_nan=true but energy_samples_kjmol contains "
                f"{len(bad)} non-finite value(s)"
            )
    return warnings


def manifest_path_safety_warnings(
    manifest: dict[str, Any],
    submission_dir: Path,
) -> list[str]:
    """Return warnings for manifest output paths that escape ``submission/``."""
    warnings: list[str] = []
    outputs = manifest.get("outputs") or {}
    if not isinstance(outputs, dict):
        return ["manifest.outputs is not an object"]
    for dotted, rel in _manifest_output_paths(outputs):
        issue = unsafe_relative_path_issue(submission_dir, rel)
        if issue:
            warnings.append(f"manifest.outputs.{dotted}: {issue}")
    return warnings


def unsafe_relative_path_issue(submission_dir: Path, rel: Any) -> Optional[str]:
    """Return a path-safety issue for a submitted artifact reference."""
    if not isinstance(rel, str) or not rel.strip():
        return "artifact path must be a non-empty relative string"
    path = Path(rel)
    if path.is_absolute():
        return f"absolute artifact path is not allowed: {rel!r}"
    try:
        base = submission_dir.resolve()
        resolved = (base / path).resolve()
        resolved.relative_to(base)
    except ValueError:
        return f"artifact path escapes submission directory: {rel!r}"
    return None


# ---------------------------------------------------------------------------
# helpers


def _flatten_floats(obj: Any):
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        try:
            yield float(obj)
        except (TypeError, ValueError):
            return
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten_floats(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _flatten_floats(v)


def _safe_path(obj: Any, dotted: str) -> Any:
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def read_json_safe(path: str | Path) -> dict[str, Any]:
    """Read a JSON file; return {} on error so the scorer can carry on."""
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Artifact integrity checks (v1.0.x)
#
# These verify the bytes on disk, not JSON values. They catch the failure mode
# where an agent leaves submission template stubs in place (53-byte PDB,
# 57-byte methods.md, text masquerading as PNG) while flipping
# manifest.status to "completed". String-equality checks in the deterministic
# layer cannot see these.

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_STAGE_ALIASES = {
    "minimize": "min",
    "minimized": "min",
    "minimisation": "min",
    "minimization": "min",
    "run_minimization": "min",
}


def check_artifact_min_bytes(
    submission_dir: Path, path: str, min_bytes: int
) -> Optional[str]:
    """Return a warning message if the artifact is missing or shorter than
    ``min_bytes``; ``None`` if it meets the floor.
    """
    target = (submission_dir / path).resolve()
    if not target.is_file():
        return f"{path}: file not found (required >= {min_bytes} bytes)"
    size = target.stat().st_size
    if size < min_bytes:
        return f"{path}: {size} bytes < required {min_bytes} (likely template stub)"
    return None


def check_template_markers(
    submission_dir: Path, path: str, forbid_markers: list[str]
) -> Optional[str]:
    """Return a warning message if ``path`` contains any of ``forbid_markers``.

    Used to detect leftover scaffold text from legacy templates, such as
    ``"Template placeholder. Replace before scoring."`` or
    ``"Replace with task-specific methods."``.
    """
    target = (submission_dir / path).resolve()
    if not target.is_file():
        return f"{path}: file not found (cannot scan for template markers)"
    try:
        text = target.read_text(errors="replace")
    except OSError as exc:
        return f"{path}: read failed ({exc})"
    hits = [m for m in forbid_markers if m.lower() in text.lower()]
    if hits:
        return f"{path}: contains template marker(s) {hits!r} — replace before submitting"
    return None


def check_markdown_structure(
    submission_dir: Path,
    path: str,
    min_h2: int = 0,
    required_sections: Optional[list[str]] = None,
) -> list[str]:
    """Return warnings for a markdown artifact (e.g. methods.md) that lacks
    the expected structure.

    - ``min_h2``: minimum number of level-2 headings.
    - ``required_sections``: case-insensitive heading titles (any level) that
      must appear. E.g. ["Methods", "Limitations", "References"].
    """
    target = (submission_dir / path).resolve()
    if not target.is_file():
        return [f"{path}: markdown file not found"]
    try:
        lines = target.read_text(errors="replace").splitlines()
    except OSError as exc:
        return [f"{path}: read failed ({exc})"]

    h2_count = 0
    headings_lower: list[str] = []
    for raw in lines:
        line = raw.strip()
        if line.startswith("## "):
            h2_count += 1
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            headings_lower.append(m.group(2).strip().lower())

    warnings: list[str] = []
    if h2_count < min_h2:
        warnings.append(f"{path}: only {h2_count} H2 heading(s); require >= {min_h2}")
    for section in required_sections or []:
        if section.strip().lower() not in headings_lower:
            warnings.append(
                f"{path}: missing required section heading {section!r}"
            )
    return warnings


def check_evidence_completeness(
    evidence: dict[str, Any], required_keys: list[str]
) -> list[str]:
    """Return warnings for ``evidence_report.json`` keys that are missing,
    null, or empty.

    Each entry in ``required_keys`` is a dotted path like
    ``"evidence.citations"`` or ``"limitations"``. The check fails if the
    resolved value is None, "", [], or {}.
    """
    warnings: list[str] = []
    for key in required_keys:
        value = _safe_path(evidence, key)
        if value is None:
            warnings.append(f"evidence_report.json missing key {key!r}")
            continue
        if isinstance(value, (str, list, dict)) and len(value) == 0:
            warnings.append(f"evidence_report.json key {key!r} is empty")
    return warnings


def check_citation_pool(
    evidence: dict[str, Any],
    allowed_pool_file: Path,
    citation_field: str = "evidence.citations",
) -> list[str]:
    """Verify that every citation in the agent's evidence_report draws from
    the curator-supplied pool.

    ``allowed_pool_file`` is a JSON file with shape::

        {
          "allowed_source_pools": ["FireProtDB", "S669", ...],
          "primary_reference": {"doi": "10.1126/...", "citation": "..."}
        }

    Citation entries can be either strings or dicts with structured
    ``doi``/``pmid``/``source``/``pool`` fields. The primary reference DOI is
    accepted directly. Data-pool citations must name an allowed pool and carry
    a concrete anchor (DOI, PMID, URL, record_id, or accession). A pool name
    appearing only in prose is not enough.
    """
    if not allowed_pool_file.is_file():
        return [f"citation pool file not found: {allowed_pool_file}"]
    try:
        pool = json.loads(allowed_pool_file.read_text())
    except Exception as exc:  # pragma: no cover -- malformed pool file
        return [f"citation pool unreadable: {exc}"]

    allowed_pools = [str(p).strip().lower()
                     for p in pool.get("allowed_source_pools") or []]
    primary_doi = str((pool.get("primary_reference") or {}).get("doi") or "").lower()

    citations = _safe_path(evidence, citation_field)
    if not citations:
        return [f"evidence_report {citation_field}: no citations to validate"]
    if not isinstance(citations, list):
        return [f"evidence_report {citation_field}: expected list, got {type(citations).__name__}"]

    def _norm(value: Any) -> str:
        return str(value or "").strip().lower()

    def _valid_doi(value: Any) -> bool:
        return bool(_DOI_RE.search(str(value or "")))

    def _valid_pmid(value: Any) -> bool:
        text = str(value or "").strip()
        return text.isdigit() or bool(_PMID_RE.search(text))

    def _valid_url(value: Any) -> bool:
        return bool(_URL_RE.match(str(value or "").strip()))

    warnings: list[str] = []
    for i, entry in enumerate(citations):
        if isinstance(entry, str):
            blob = entry.strip()
            blob_lower = blob.lower()
            has_primary_doi = bool(primary_doi and primary_doi in blob_lower)
            has_pool = any(pool_name in blob_lower for pool_name in allowed_pools)
            has_anchor = _valid_doi(blob) or bool(_PMID_RE.search(blob)) or ("http" in blob_lower)
        elif isinstance(entry, dict):
            blob = " ".join(str(entry.get(k, "")) for k in (
                "doi", "source", "pool", "pmid", "url", "citation", "note",
            ))
            source = _norm(entry.get("source"))
            pool_name = _norm(entry.get("pool"))
            has_pool = source in allowed_pools or pool_name in allowed_pools
            doi = _norm(entry.get("doi"))
            citation = _norm(entry.get("citation"))
            has_primary_doi = bool(
                primary_doi and (doi == primary_doi or primary_doi in citation)
            )
            has_anchor = (
                _valid_doi(entry.get("doi"))
                or _valid_pmid(entry.get("pmid"))
                or _valid_url(entry.get("url"))
                or bool(str(entry.get("record_id") or entry.get("accession") or "").strip())
            )
        else:
            warnings.append(f"{citation_field}[{i}]: unrecognized entry type")
            continue
        if not blob.strip():
            warnings.append(f"{citation_field}[{i}]: empty citation entry")
            continue
        if has_primary_doi:
            continue
        if has_pool and has_anchor:
            continue
        if has_pool:
            warnings.append(
                f"{citation_field}[{i}]: allowed pool citation lacks DOI, "
                "PMID, URL, record_id, or accession anchor"
            )
            continue
        warnings.append(
            f"{citation_field}[{i}]: not anchored to allowed pool "
            f"{allowed_pools!r} or primary DOI {primary_doi!r}"
        )
    return warnings


def check_png_magic(path: Path) -> Optional[str]:
    """Return a warning if ``path`` does not start with the PNG magic bytes.

    Catches the failure mode of writing a text caption file with a ``.png``
    name and listing it under ``manifest.outputs.figures``.
    """
    if not path.is_file():
        return f"{path.name}: file not found"
    try:
        with path.open("rb") as handle:
            head = handle.read(len(_PNG_MAGIC))
    except OSError as exc:
        return f"{path.name}: read failed ({exc})"
    if head != _PNG_MAGIC:
        return f"{path.name}: not a PNG (magic bytes mismatch)"
    return None


def check_figures_are_png(
    submission_dir: Path,
    figures: list[str],
    min_figure_bytes: int = 1024,
) -> list[str]:
    """Run :func:`check_png_magic` and a byte-floor check across every figure
    listed in ``manifest.outputs.figures``.
    """
    warnings: list[str] = []
    for fig in figures or []:
        target = (submission_dir / fig).resolve()
        bytes_warn = check_artifact_min_bytes(submission_dir, fig, min_figure_bytes)
        if bytes_warn:
            warnings.append(bytes_warn)
            continue
        magic_warn = check_png_magic(target)
        if magic_warn:
            warnings.append(magic_warn)
    return warnings


def check_status_artifact_floor(
    manifest: dict[str, Any],
    submission_dir: Path,
    status_floor: dict[str, int],
) -> list[str]:
    """Only when ``manifest.status == "completed"`` enforce minimum byte sizes
    for the artifacts in ``status_floor``. A status of partial / blocked /
    failed implicitly waives these floors (since the agent already admitted
    the work was incomplete).
    """
    if (manifest.get("status") or "completed") != "completed":
        return []
    warnings: list[str] = []
    for rel, floor in status_floor.items():
        warn = check_artifact_min_bytes(submission_dir, rel, floor)
        if warn:
            warnings.append(
                f"manifest.status='completed' but {warn}"
            )
    return warnings


def check_manifest_artifact_floor(
    manifest: dict[str, Any],
    submission_dir: Path,
    manifest_path: str,
    min_count: int = 1,
    min_bytes: int = 1,
) -> list[str]:
    """Require artifacts listed in a manifest field to exist on disk.

    Unlike ``json_min_length``, this verifies the files themselves rather than
    trusting the manifest list. Paths are resolved relative to ``submission/``.
    """
    value = _safe_path(manifest, manifest_path)
    if not isinstance(value, list):
        return [
            f"manifest {manifest_path}: expected list, got "
            f"{type(value).__name__ if value is not None else 'missing'}"
        ]

    warnings: list[str] = []
    if len(value) < min_count:
        warnings.append(
            f"manifest {manifest_path}: listed {len(value)} artifacts, "
            f"require >= {min_count}"
        )

    for i, rel in enumerate(value):
        if not isinstance(rel, str) or not rel.strip():
            warnings.append(f"manifest {manifest_path}[{i}]: empty artifact path")
            continue
        warn = check_artifact_min_bytes(submission_dir, rel, min_bytes)
        if warn:
            warnings.append(f"manifest {manifest_path}[{i}]: {warn}")
    return warnings


def check_trajectory_file_signatures(
    manifest: dict[str, Any],
    submission_dir: Path,
    manifest_path: str,
) -> list[str]:
    """Require trajectory artifacts to have a recognized trajectory signature.

    StudyBench currently publishes DCD examples in its submission blueprint.
    A DCD must start with the fixed 84-record + ``CORD`` magic; plain text,
    JSON, or arbitrary padded bytes are not accepted as trajectory artifacts.
    """
    value = _safe_path(manifest, manifest_path)
    if not isinstance(value, list):
        return [
            f"manifest {manifest_path}: expected list, got "
            f"{type(value).__name__ if value is not None else 'missing'}"
        ]

    warnings: list[str] = []
    for i, rel in enumerate(value):
        if not isinstance(rel, str) or not rel.strip():
            warnings.append(f"manifest {manifest_path}[{i}]: empty trajectory path")
            continue
        path = (submission_dir / rel).resolve()
        if not path.is_file():
            warnings.append(
                f"manifest {manifest_path}[{i}]: file not found: {rel}"
            )
            continue
        suffix = path.suffix.lower()
        if suffix != ".dcd":
            warnings.append(
                f"manifest {manifest_path}[{i}]: unsupported trajectory "
                f"extension {suffix!r}; expected .dcd"
            )
            continue
        try:
            with path.open("rb") as handle:
                head = handle.read(len(_DCD_MAGIC))
        except OSError as exc:
            warnings.append(
                f"manifest {manifest_path}[{i}]: could not read {rel}: {exc}"
            )
            continue
        if head != _DCD_MAGIC:
            warnings.append(
                f"manifest {manifest_path}[{i}]: {rel} is not a DCD "
                "trajectory (missing DCD CORD header)"
            )
    return warnings


def check_provenance_execution_evidence(
    provenance: dict[str, Any],
    required_stages: list[str],
    min_command_count: int = 1,
    *,
    harness_record: Any = None,
    require_harness_record: bool = False,
) -> list[str]:
    """Require structured evidence that real workflow commands/actions ran.

    This does not forbid Python or custom scripts; it rejects the weaker pattern
    of listing script filenames without structured evidence that commands or
    actions actually ran. When a task supplies required_stages, entries must
    also carry matching stage labels; otherwise stage names are optional so the
    benchmark remains tool-agnostic.
    """
    if not isinstance(provenance, dict):
        return ["provenance.json is not a JSON object"]

    if require_harness_record:
        harness_log = _execution_log_from_payload(harness_record)
        if not harness_log:
            return [
                "harness execution record required but missing or empty; "
                "solver-written provenance.json is not sufficient execution evidence"
            ]
        return _check_execution_log_entries(
            harness_log,
            source_label="harness execution record",
            entry_label="harness execution record",
            required_stages=required_stages,
            min_command_count=min_command_count,
            require_measured_walltime=True,
        )

    command_log = _execution_log_from_payload(provenance)
    if not command_log:
        return [
            "provenance lacks command_log/commands/execution_log entries; "
            "scripts alone are not execution evidence"
        ]
    return _check_execution_log_entries(
        command_log,
        source_label="provenance execution log",
        entry_label="provenance command_log",
        required_stages=required_stages,
        min_command_count=min_command_count,
        require_measured_walltime=False,
    )


def _execution_log_from_payload(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    return _first_list(
        payload,
        "command_log",
        "records",
        "commands",
        "execution_log",
        "attempts",
    )


def _check_execution_log_entries(
    command_log: list[Any],
    *,
    source_label: str,
    entry_label: str,
    required_stages: list[str],
    min_command_count: int,
    require_measured_walltime: bool,
) -> list[str]:
    structured: list[dict[str, Any]] = [
        entry for entry in command_log if isinstance(entry, dict)
    ]
    warnings: list[str] = []
    if len(structured) < min_command_count:
        warnings.append(
            f"{source_label} has {len(structured)} structured "
            f"entry(ies); require >= {min_command_count}"
        )

    require_stage_labels = bool(required_stages)
    stages_seen: set[str] = set()
    for index, entry in enumerate(structured):
        stage = _canonical_stage(entry.get("stage"))
        command = _first_nonempty(entry, "command", "action", "tool")
        status = entry.get("exit_code", entry.get("status", entry.get("result")))
        if require_stage_labels and not stage:
            warnings.append(f"{entry_label}[{index}] missing stage")
        elif stage:
            stages_seen.add(stage)
        if not command:
            warnings.append(
                f"{entry_label}[{index}] missing command/action/tool"
            )
        if status is None or status == "":
            warnings.append(
                f"{entry_label}[{index}] missing exit_code/status/result"
            )
        if require_measured_walltime:
            walltime = entry.get(
                "walltime_seconds",
                entry.get("duration_seconds", entry.get("elapsed_seconds")),
            )
            if not _is_nonnegative_finite_number(walltime):
                warnings.append(
                    f"{entry_label}[{index}] missing measured walltime_seconds"
                )

    missing_stages = [
        stage for stage in required_stages
        if _canonical_stage(stage) not in stages_seen
    ]
    if missing_stages:
        warnings.append(
            f"{source_label} missing required stage(s): {missing_stages}"
        )
    return warnings


def _is_nonnegative_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value)) and float(value) >= 0.0


def _canonical_stage(stage: Any) -> str:
    text = str(stage or "").strip().lower()
    return _STAGE_ALIASES.get(text, text)


def run_artifact_integrity(
    submission_dir: Path,
    integrity_checks: list[Any],
    manifest: dict[str, Any],
    evidence: dict[str, Any],
    task_dir: Optional[Path] = None,
    harness_record: Any = None,
) -> list[str]:
    """Dispatch every ``IntegrityCheck`` for a task and collect warning strings.

    ``integrity_checks`` is iterated by ``check_type`` and dispatched to the
    appropriate helper above. Unknown check types are recorded as warnings
    rather than raising, so a forward-compatible scorer can read older
    task.json files.
    """
    warnings: list[str] = []
    for check in integrity_checks or []:
        ctype = getattr(check, "check_type", None)
        try:
            if ctype == "artifact_min_bytes":
                w = check_artifact_min_bytes(
                    submission_dir, check.path, int(check.min_bytes or 0),
                )
                if w:
                    warnings.append(f"[{check.check_id}] {w}")
            elif ctype == "template_markers":
                w = check_template_markers(
                    submission_dir, check.path, check.forbid_markers or [],
                )
                if w:
                    warnings.append(f"[{check.check_id}] {w}")
            elif ctype == "markdown_structure":
                ws = check_markdown_structure(
                    submission_dir, check.path,
                    min_h2=int(check.min_h2 or 0),
                    required_sections=check.required_sections or [],
                )
                warnings.extend(f"[{check.check_id}] {w}" for w in ws)
            elif ctype == "evidence_completeness":
                ws = check_evidence_completeness(
                    evidence, check.required_keys or [],
                )
                warnings.extend(f"[{check.check_id}] {w}" for w in ws)
            elif ctype == "citation_pool":
                if task_dir is None or not check.allowed_pool_file:
                    warnings.append(
                        f"[{check.check_id}] citation_pool needs task_dir and allowed_pool_file"
                    )
                    continue
                ws = check_citation_pool(
                    evidence,
                    (task_dir / check.allowed_pool_file).resolve(),
                    citation_field=check.citation_field or "evidence.citations",
                )
                warnings.extend(f"[{check.check_id}] {w}" for w in ws)
            elif ctype == "figures_are_png":
                figs = _safe_path(
                    manifest,
                    check.figures_manifest_path or "outputs.figures",
                ) or []
                ws = check_figures_are_png(
                    submission_dir,
                    list(figs) if isinstance(figs, list) else [],
                    min_figure_bytes=int(check.min_figure_bytes or 1024),
                )
                warnings.extend(f"[{check.check_id}] {w}" for w in ws)
            elif ctype == "status_artifact_floor":
                ws = check_status_artifact_floor(
                    manifest, submission_dir, check.status_floor or {},
                )
                warnings.extend(f"[{check.check_id}] {w}" for w in ws)
            elif ctype == "manifest_artifact_floor":
                ws = check_manifest_artifact_floor(
                    manifest,
                    submission_dir,
                    check.manifest_path or "",
                    min_count=int(check.min_count or 1),
                    min_bytes=int(check.min_bytes or 1),
                )
                warnings.extend(f"[{check.check_id}] {w}" for w in ws)
            elif ctype == "trajectory_file_signature":
                ws = check_trajectory_file_signatures(
                    manifest,
                    submission_dir,
                    check.manifest_path or "outputs.trajectories",
                )
                warnings.extend(f"[{check.check_id}] {w}" for w in ws)
            elif ctype == "provenance_execution_evidence":
                provenance_path = submission_dir / "provenance.json"
                provenance = read_json_safe(provenance_path)
                ws = check_provenance_execution_evidence(
                    provenance,
                    required_stages=check.required_stages or [],
                    min_command_count=int(check.min_command_count or 1),
                    harness_record=harness_record,
                    require_harness_record=bool(
                        getattr(check, "require_harness_record", False)
                    ),
                )
                warnings.extend(f"[{check.check_id}] {w}" for w in ws)
            elif ctype == "submission_artifact_hashes":
                provenance = read_json_safe(submission_dir / "provenance.json")
                ws = required_raw_output_hash_warnings(
                    submission_dir,
                    manifest,
                    provenance,
                    require_manifest_outputs=bool(
                        getattr(check, "require_manifest_output_hashes", True)
                    ),
                )
                warnings.extend(f"[{check.check_id}] {w}" for w in ws)
            elif ctype == "openmm_minimized_state_consistency":
                tolerance = getattr(check, "coordinate_tolerance_nm", None)
                ws = openmm_minimized_state_consistency_warnings(
                    submission_dir,
                    manifest,
                    coordinate_tolerance_nm=(
                        float(tolerance) if tolerance is not None else 1.0e-3
                    ),
                )
                warnings.extend(f"[{check.check_id}] {w}" for w in ws)
            else:
                warnings.append(
                    f"[{check.check_id}] unknown integrity check_type {ctype!r}"
                )
        except Exception as exc:  # pragma: no cover -- defensive
            warnings.append(
                f"[{check.check_id}] integrity check raised {type(exc).__name__}: {exc}"
            )
    return warnings


def _manifest_output_paths(outputs: dict[str, Any]):
    for key, value in outputs.items():
        yield from _walk_manifest_output_value(str(key), value)


def _walk_manifest_output_value(prefix: str, value: Any):
    if value is None:
        return
    if isinstance(value, str):
        yield prefix, value
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_manifest_output_value(f"{prefix}.{index}", item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_manifest_output_value(f"{prefix}.{key}", item)
        return
    yield prefix, value


def _first_list(obj: dict[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, list):
            return value
    return []


def _first_nonempty(obj: dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
