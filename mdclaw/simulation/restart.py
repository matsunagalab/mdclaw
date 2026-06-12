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

import json  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402

from mdclaw._common import (  # noqa: E402
    ensure_directory,
)

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)



def _find_ancestor_for_explicit_restart(
    job_dir: Optional[str],
    node_id: Optional[str],
    restart_from: Optional[str],
) -> tuple[Optional[str], dict]:
    """Match an explicit ``--restart-from`` path to a DAG ancestor.

    When the caller passes a literal ``restart_from`` path (rather than
    letting the DAG resolver pick one), the run side still needs to know
    which ancestor — if any — that path corresponds to so the cumulative
    step counter and signature checks track the same node. This helper
    walks ``get_ancestors`` and returns the first ancestor whose
    ``state`` or ``checkpoint`` artifact resolves to the same absolute
    path as ``restart_from``.

    Returns ``(node_id, metadata_dict)``. If the path matches no
    ancestor (external file, hand-edited DAG, etc.), returns
    ``(None, {})`` — callers should treat this as an external restart
    source and skip the per-ancestor lookups.
    """
    if not (job_dir and node_id and restart_from):
        return None, {}
    from mdclaw._node import get_ancestors, read_node, resolve_artifact
    restart_path = str(Path(restart_from).resolve())
    for anc_id in get_ancestors(job_dir, node_id)[1:]:
        try:
            anc = read_node(job_dir, anc_id)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        for artifact_key in ("state", "checkpoint"):
            rel = anc.get("artifacts", {}).get(artifact_key)
            if not isinstance(rel, str):
                continue
            if str(resolve_artifact(job_dir, anc_id, rel)) == restart_path:
                return anc_id, (anc.get("metadata", {}) or {})
    return None, {}


def _restart_source_metadata(
    job_dir: Optional[str],
    node_id: Optional[str],
    restart_from: Optional[str],
) -> dict:
    """Backwards-compatible wrapper around
    ``_find_ancestor_for_explicit_restart`` for callers that only need
    the metadata dict (signature mismatch comparisons in run_production)."""
    _anc_id, meta = _find_ancestor_for_explicit_restart(
        job_dir, node_id, restart_from,
    )
    return meta


def _resolve_restart_node_id_for_run(
    *,
    job_dir: Optional[str],
    node_id: Optional[str],
    restart_from: Optional[str],
    explicit_restart_from: bool,
    inputs: dict,
) -> Optional[str]:
    """Return the ancestor node id whose ``metadata.final_step`` should
    govern the restored ``simulation.currentStep`` for this run.

    Two cases:

    - The DAG resolver picked the restart artifact (``explicit_restart_from``
      is False and ``inputs`` carries ``restart_from`` /
      ``restart_from_node_id``): use the resolver's choice. The picker
      already enforces "same ancestor for path and step counter".

    - The caller passed an explicit ``--restart-from`` path: only trust
      a node id when the path *matches* a DAG ancestor's ``state`` /
      ``checkpoint`` artifact via
      ``_find_ancestor_for_explicit_restart``. An external / manually-
      placed file has no ancestor metadata to draw from; the run side
      should leave ``simulation.currentStep`` at whatever the loader
      sets (0 for ``saveState`` XML; the persisted counter for
      ``saveCheckpoint`` ``.chk``).
    """
    if not explicit_restart_from:
        return inputs.get("restart_from_node_id")
    anc_id, _meta = _find_ancestor_for_explicit_restart(
        job_dir, node_id, restart_from,
    )
    return anc_id


def _restart_node_type_for_run(
    job_dir: Optional[str],
    restart_node_id: Optional[str],
) -> Optional[str]:
    """Return the DAG type of the restart-source node, when known."""
    if not (job_dir and restart_node_id):
        return None
    try:
        from mdclaw._node import read_node
        node = read_node(job_dir, restart_node_id)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    value = node.get("type") or node.get("node_type")
    return value if isinstance(value, str) else None


def _detect_ensemble_mismatch(
    state_xml_path: Path, system_has_barostat: bool
) -> Optional[str]:
    """Classify a barostat / saved-state inconsistency for warning purposes.

    The ensemble-agnostic loader (``_load_state_into_simulation``)
    transfers only positions/velocities/box and does not touch Context
    parameters, so neither case raises. Callers may still surface a
    warning when the saved ensemble differs from the new System's
    ensemble — the simulation is safe to start, but a brief
    re-equilibration is expected (e.g. NVT→NPT will need a few ps for
    the barostat to settle the volume).

    Returns one of:
        - ``"npt_state_nvt_system"`` — state has NPT barostat parameters
          but the new System has no barostat.
        - ``"nvt_state_npt_system"`` — new System has a barostat but the
          saved state lacks NPT parameters.
        - ``None`` — matched (NPT/NPT or NVT/NVT).
    """
    state_has_pressure = "MonteCarloPressure" in state_xml_path.read_text()
    if state_has_pressure and not system_has_barostat:
        return "npt_state_nvt_system"
    if system_has_barostat and not state_has_pressure:
        return "nvt_state_npt_system"
    return None


def _load_state_into_simulation(
    simulation,
    restart_path: Path,
    *,
    is_periodic: bool,
    temperature_kelvin: Optional[float] = None,
    random_seed: Optional[int] = None,
) -> dict:
    """Load a saved OpenMM state into ``simulation`` regardless of ensemble.

    For ``.xml`` (saveState output): deserialize via
    ``XmlSerializer.deserialize`` and transfer only positions, velocities,
    and (when periodic) box vectors. Unlike ``simulation.loadState()`` —
    which routes through ``Context.setState`` and restores every saved
    Context parameter by name — this skips parameter restoration entirely
    and is therefore safe across ensemble changes:

    - NPT state → NVT system: barostat parameters in the saved state are
      ignored (the new System has no barostat to receive them).
    - NVT state → NPT system: the new barostat starts in its default
      relaxed state and re-equilibrates within a few ps.

    For ``.chk`` (binary checkpoint): fall back to
    ``simulation.loadCheckpoint`` which requires identical System layout
    and GPU architecture. This path is for same-machine bit-exact restart
    only; cross-ensemble or cross-GPU should use XML.

    Note: ``simulation.currentStep`` is NOT restored by this helper. The
    caller restores it from ``metadata.final_step`` (see
    ``read_ancestor_final_step``).

    Returns ``{"format": "xml"|"checkpoint"}``.
    """
    if restart_path.suffix == ".xml":
        from openmm import XmlSerializer
        from openmm.unit import kelvin
        state_text = restart_path.read_text()
        try:
            state = XmlSerializer.deserialize(state_text)
        except Exception as exc:
            raise ValueError(
                f"Restart state could not be deserialized: {exc}"
            ) from exc
        expected_particles = simulation.system.getNumParticles()
        positions = state.getPositions()
        if len(positions) != expected_particles:
            raise ValueError(
                "Restart state particle count mismatch: "
                f"state positions={len(positions)}, system particles={expected_particles}"
            )
        simulation.context.setPositions(positions)
        info = {
            "format": "xml",
            "velocities_present": True,
            "velocities_rethermalized": False,
            "box_vectors_dropped": False,
        }
        try:
            velocities = state.getVelocities()
        except Exception as e:
            info["velocities_present"] = False
            logger.warning(
                f"Saved state at {restart_path} has no velocities "
                f"({e}); resuming with re-thermalized velocities."
            )
            if temperature_kelvin is None:
                raise ValueError(
                    "Restart state has no velocities and no temperature was "
                    "provided for re-thermalization"
                ) from e
            if random_seed is not None:
                simulation.context.setVelocitiesToTemperature(
                    temperature_kelvin * kelvin, int(random_seed)
                )
            else:
                simulation.context.setVelocitiesToTemperature(
                    temperature_kelvin * kelvin
                )
            info["velocities_rethermalized"] = True
        else:
            if len(velocities) != expected_particles:
                raise ValueError(
                    "Restart state velocity count mismatch: "
                    f"state velocities={len(velocities)}, "
                    f"system particles={expected_particles}"
                )
            simulation.context.setVelocities(velocities)
        if is_periodic:
            box = state.getPeriodicBoxVectors()
            if box is not None:
                simulation.context.setPeriodicBoxVectors(*box)
        elif "<PeriodicBoxVectors" in state_text:
            logger.warning(
                "Saved state contains periodic box vectors, but the current "
                "System is non-periodic; dropping box vectors during restart."
            )
            info["box_vectors_dropped"] = True
        return info
    simulation.loadCheckpoint(str(restart_path))
    return {"format": "checkpoint"}


_OPENMM_RANDOM_SEED_MODULUS = 2_147_483_647


def _restart_random_seed(
    random_seed: Optional[int],
    restart_step: Optional[int],
) -> Optional[int]:
    """Derive a deterministic non-zero RNG seed for a restarted segment."""
    if random_seed is None:
        return None
    offset = max(1, int(restart_step or 0))
    seed = int(random_seed)
    return ((seed + offset - 1) % _OPENMM_RANDOM_SEED_MODULUS) + 1


def _save_checkpoint_atomic(simulation, path: Path) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        simulation.saveCheckpoint(str(tmp))
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _save_state_atomic(simulation, path: Path) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        simulation.saveState(str(tmp))
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


# DCD fixed-record-84 + "CORD" magic. OpenMM/CHARMM DCD always emit this
# as the first 8 bytes, so a file that lacks it cannot be appended to
# via DCDReporter(append=True).


_DCD_MAGIC = b"\x54\x00\x00\x00CORD"


def _dcd_has_valid_header(path: Path) -> bool:
    """Return True iff *path* is a non-empty DCD file whose first 8 bytes
    match the fixed 84-record + CORD magic.

    Used to guard ``DCDReporter(append=True)`` against 0-byte / truncated
    orphans left by a previously-failed run (e.g. reporter flushes delayed
    by a synced filesystem). Appending to such a file raises a cryptic
    ``ValueError: Cannot append to file with invalid DCD header`` inside
    OpenMM's constructor — much easier to handle up-front.
    """
    try:
        if not path.is_file() or path.stat().st_size < 8:
            return False
        with path.open("rb") as fh:
            return fh.read(8) == _DCD_MAGIC
    except OSError:
        return False


def _node_previously_failed(
    job_dir: Optional[str], node_id: Optional[str]
) -> bool:
    """Return True iff ``node.json`` exists and records ``status ==
    "failed"``. **Must be called before** :func:`begin_node` flips the
    sentinel to ``running`` — otherwise the prior failure is invisible."""
    if not (job_dir and node_id):
        return False
    from mdclaw._node import read_node
    try:
        return read_node(job_dir, node_id).get("status") == "failed"
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


def _resolve_dcd_append_mode(
    trajectory_file: Path,
    energy_file: Path,
    append_requested: bool,
    prior_failed: bool,
) -> tuple:
    """Decide whether DCD + energy reporters should open in append mode.

    Legacy mid-run restart into the same prod node requires a valid
    partial DCD; 0-byte / header-less files (e.g. reporter flush
    interrupted by synced-filesystem lag) and the ``failed``-status
    sentinel both mean the stale artifacts must be discarded before the
    reporters are constructed.

    Returns ``(do_append, warning_message, removed)`` where:

    - ``do_append``: final append flag to hand to both reporters (always
      shared, commit 1ccf607).
    - ``warning_message``: human-readable string to append to
      ``result["warnings"]`` when cleanup happens, else ``None``.
    - ``removed``: list of :class:`Path` objects whose contents were
      deleted (returned for logging/test assertions, even if the unlink
      itself raised — the caller sees exactly which files were targeted).
    """
    do_append = append_requested and trajectory_file.exists()
    if not do_append:
        return False, None, []
    if not (prior_failed or not _dcd_has_valid_header(trajectory_file)):
        return True, None, []

    reason = "failed status" if prior_failed else "invalid/empty DCD header"
    removed = []
    for stale in (trajectory_file, energy_file):
        removed.append(stale)
        try:
            if stale.exists():
                stale.unlink()
        except OSError as e:
            logger.warning(f"Could not remove stale artifact {stale}: {e}")
    warning = (
        f"Discarded stale artifacts from previous run "
        f"({reason}); starting trajectory/energy fresh while "
        f"resuming from checkpoint."
    )
    return False, warning, removed


def _flush_reporter_stream(reporter) -> None:
    """Best-effort flush for OpenMM reporters that own a file handle."""
    out = getattr(reporter, "_out", None)
    if out is not None and hasattr(out, "flush"):
        out.flush()


def _close_reporter_stream(reporter) -> None:
    """Best-effort close for OpenMM reporters that own a file handle."""
    out = getattr(reporter, "_out", None)
    if out is not None:
        if hasattr(out, "flush"):
            out.flush()
        if hasattr(out, "close"):
            out.close()


def _count_state_data_rows(path: Path) -> int:
    if not path.is_file():
        return 0
    rows = 0
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            rows += 1
    return rows
