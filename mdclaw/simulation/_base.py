"""
MD Simulation Server - Molecular dynamics simulation & analysis tools.

Provides MCP tools for:
- OpenMM MD simulation (NVT/NPT equilibration, production)
- MDTraj trajectory analysis (RMSD, RMSF, distances, hydrogen bonds, etc.)
- Energy analysis
- Secondary structure analysis
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

from pathlib import Path  # noqa: E402
from typing import Any, Dict, Optional, Tuple  # noqa: E402

from mdclaw._common import (  # noqa: E402
    ensure_directory,
)

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)



def _node_artifact_path(path: Optional[str]) -> str:
    """Convert an absolute output path into a node-relative artifact path."""
    if not path:
        return ""
    return f"artifacts/{Path(path).name}"


def _fail_node_if_running(
    job_dir: Optional[str],
    node_id: Optional[str],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """Mark the node as ``failed`` when an early-return path fires after
    ``begin_node()``. Idempotent: safe to call when not in node mode, and
    safe to call when the node has already been marked failed elsewhere.

    Returns ``result`` unchanged so call sites can ``return _fail_node_if_running(...)``.
    """
    if job_dir and node_id and not result.get("success"):
        from mdclaw._node import fail_node
        try:
            fail_node(job_dir, node_id, errors=result.get("errors") or ["run_* aborted"])
        except Exception:  # noqa: BLE001
            # fail_node is best-effort — never let a bookkeeping failure mask
            # the original error the caller is about to surface.
            pass
    return result


def _resolve_implicit_solvent_model(
    implicit_solvent: str,
    openmm_models: Dict[str, Any],
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """Resolve a user-supplied implicit-solvent name to an OpenMM GB symbol.

    Goes through ``forcefield_catalog.normalize_implicit_solvent`` so the
    same alias rules apply on the run side as on the build side
    (case-insensitive ``HCT`` / ``OBC1`` / ``OBC2`` / ``GBn`` / ``GBn2`` plus
    the ``gbneck2`` / ``igb1``–``igb8`` shortcuts). Unknown names — and the
    historical ``.upper()``-vs-mixed-case bug that silently mapped
    ``"GBn2"`` → ``"GBN2"`` → ``OBC2`` — return a structured error instead
    of falling back, so typos surface as a clean
    ``implicit_solvent_model_unsupported`` failure rather than a silent
    accuracy regression.

    Returns:
        Tuple ``(model, None)`` on success; ``(None, error_dict)`` on
        failure. ``error_dict`` carries ``code`` and ``errors`` ready to
        splice into ``result`` before ``_fail_node_if_running``.
    """
    from mdclaw import forcefield_catalog as _fc

    if str(implicit_solvent).strip().lower() == "custom":
        return None, None

    canon = _fc.normalize_implicit_solvent(implicit_solvent)
    if canon not in _fc.IMPLICIT_SOLVENT_XML:
        supported = ", ".join((*_fc.supported_implicit_solvent_models(), "custom"))
        return None, {
            "code": "implicit_solvent_model_unsupported",
            "errors": [
                f"Unknown implicit-solvent model {implicit_solvent!r}. "
                f"Supported: {supported}."
            ],
        }
    if canon not in openmm_models:
        # Catalog and the run-side OpenMM symbol map disagree. This should
        # not happen — flag it as a structured failure so the bug is
        # visible rather than masked by an OBC2 fallback.
        return None, {
            "code": "implicit_solvent_model_unsupported",
            "errors": [
                f"Implicit-solvent model {canon!r} is in the catalog but the "
                f"run-side OpenMM symbol map is missing it; this is a bug."
            ],
        }
    return openmm_models[canon], None


def _check_topology_implicit_solvent_match(
    *,
    topology_implicit_solvent: Optional[str],
    runtime_implicit_solvent: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Compare the topo node's build-time implicit-solvent metadata against
    the run-side request, before any System is deserialized from
    ``system.xml``.

    The run-side XML system validator
    (``modern_system_implicit_solvent_unsupported``) catches the worst
    case (vacuum System paired with ``--implicit-solvent``) but cannot
    tell ``OBC2``-built from ``GBn2``-built — both Systems carry a
    ``CustomGBForce``, so a model mismatch would silently run with the
    wrong GB radii. This guard reads the build-time choice from the
    resolver's ``topology_implicit_solvent`` (sourced from
    ``metadata.implicit_solvent`` of the topo ancestor) and compares it
    against the canonical runtime name. Aliases are normalized through
    ``forcefield_catalog.normalize_implicit_solvent`` so ``"GBn2"`` and
    ``"gbneck2"`` count as a match.

    Args:
        topology_implicit_solvent: ``metadata.implicit_solvent`` from the
            topo ancestor. ``None`` indicates the topo was built without
            GB (explicit / vacuum) or the metadata is missing.
        runtime_implicit_solvent: The ``--implicit-solvent`` value passed
            to run_minimization / run_equilibration / run_production.

    Returns:
        ``None`` when the choices line up; otherwise a structured-error
        dict carrying ``code``, ``errors``, and a recovery-friendly
        ``message``. Callers splice it into ``result`` and bail via
        ``_fail_node_if_running``.
    """
    from mdclaw import forcefield_catalog as _fc

    # Canonicalize the build-time metadata. ``None`` stays ``None``;
    # anything that does not normalize to a catalog key indicates node
    # corruption (someone hand-edited node.json) — surface it explicitly
    # rather than blocking on what looks like a runtime typo.
    if topology_implicit_solvent is None:
        canon_topo: Optional[str] = None
    else:
        if str(topology_implicit_solvent).strip().lower() == "custom":
            normalized = "custom"
        else:
            normalized = _fc.normalize_implicit_solvent(topology_implicit_solvent)
        if normalized != "custom" and normalized not in _fc.IMPLICIT_SOLVENT_XML:
            return {
                "code": "implicit_solvent_topology_metadata_invalid",
                "errors": [
                    f"Topo node metadata records implicit_solvent="
                    f"{topology_implicit_solvent!r}, which is not a "
                    f"recognized GB model or 'custom'. The node.json metadata may "
                    f"be corrupt; rebuild the topo node via "
                    f"build_amber_system."
                ],
                "message": (
                    f"Topo metadata.implicit_solvent="
                    f"{topology_implicit_solvent!r} is not a known model."
                ),
            }
        canon_topo = normalized

    if runtime_implicit_solvent is None:
        canon_run: Optional[str] = None
    else:
        if str(runtime_implicit_solvent).strip().lower() == "custom":
            normalized = "custom"
        else:
            normalized = _fc.normalize_implicit_solvent(runtime_implicit_solvent)
        if normalized != "custom" and normalized not in _fc.IMPLICIT_SOLVENT_XML:
            supported = ", ".join((*_fc.supported_implicit_solvent_models(), "custom"))
            return {
                "code": "implicit_solvent_model_unsupported",
                "errors": [
                    f"Unknown implicit-solvent model {runtime_implicit_solvent!r}. "
                    f"Supported: {supported}."
                ],
                "message": (
                    f"Unknown runtime implicit_solvent={runtime_implicit_solvent!r}."
                ),
            }
        canon_run = normalized

    if canon_topo == canon_run:
        return None

    if canon_topo is None and canon_run is not None:
        return {
            "code": "implicit_solvent_topology_mismatch",
            "errors": [
                f"Topo node was built without implicit solvent, but "
                f"requested implicit_solvent={runtime_implicit_solvent!r}. "
                f"Rebuild a topo node with "
                f"build_amber_system --implicit-solvent {canon_run}, or "
                f"rerun without --implicit-solvent."
            ],
            "message": (
                f"Topo built without GB; runtime requested {canon_run!r}."
            ),
        }

    if canon_topo is not None and canon_run is None:
        return {
            "code": "implicit_solvent_topology_mismatch",
            "errors": [
                f"Topo node was built with implicit_solvent={canon_topo!r}, "
                f"but the run did not pass --implicit-solvent. Rerun with "
                f"--implicit-solvent {canon_topo}, or rebuild a topo node "
                f"without GB."
            ],
            "message": (
                f"Topo built with GB={canon_topo!r}; runtime omitted "
                f"--implicit-solvent."
            ),
        }

    # Both sides carry a model but they disagree.
    return {
        "code": "implicit_solvent_topology_mismatch",
        "errors": [
            f"Topo node was built with implicit_solvent={canon_topo!r}, "
            f"but run requested implicit_solvent={canon_run!r}. Rebuild a "
            f"new topo node with build_amber_system --implicit-solvent "
            f"{canon_run}, or rerun with --implicit-solvent {canon_topo}."
        ],
        "message": (
            f"Topo built with GB={canon_topo!r}; runtime requested "
            f"{canon_run!r}."
        ),
    }
