"""Node-based job graph management (schema v3).

Each pipeline step (prep, solv, topo, min, eq, prod) is a *node* with its own
directory, ``node.json``, lock file, and ``artifacts/`` folder.  Parent-child
relationships form a DAG.  ``progress.json`` is a thin index of nodes.

Design principle:
    skill = what to run (orchestration, no state mutation)
    tool  = run + record (execution + state via this module)
"""

import json
import logging
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)

from mdclaw.node.constants import _RESTART_NODE_ID_UNSET  # noqa: E402
from mdclaw.node.graph import _iter_ancestor_ids  # noqa: E402
from mdclaw.node.io import _read_artifact_from_node, _read_continued_from, _read_metadata_field  # noqa: E402
from mdclaw.node.progress import _load_progress_v3  # noqa: E402


def _select_md_restart_ancestor(
    job_dir: str, node_id: str,
) -> dict:
    """Walk parents in BFS order and pick the first min/prod/eq ancestor that
    *should* be the restart source for *node_id*.

    For each min/prod/eq ancestor visited, three outcomes are possible:

    1. ``state`` is present — pick it (preferred; XML is cross-node
       portable). Stop the walk.
    2. ``state`` is missing, ``checkpoint`` is present — pick the
       checkpoint (same-GPU bit-exact replay). Stop the walk.
    3. The ancestor is *completed* but carries **neither** artifact.
       Treat the DAG as broken and refuse to skip this ancestor.
       Restarting from an older ancestor would silently roll the run
       back across whatever the empty ancestor produced (think: a
       prod that only wrote a trajectory and forgot the state). The
       caller should surface this as ``restart_from_error``.
    4. The ancestor is *not completed* (e.g. failed, pending). The
       upstream ``_input_resolution_status_errors`` catches direct
       parents in that state. For non-direct ancestors we keep
       walking — the run side reaches a stale-but-completed node
       further up. This matches the pre-Phase-18 behaviour.

    Returns one of:
      - ``{"restart_from": <abs path>, "restart_from_node_id": <str>}``
        on success.
      - ``{"restart_from_error": <message>}`` when a completed min/prod/eq
        ancestor carries neither artifact.
      - ``{}`` when no min/prod/eq ancestor exists at all (the run side
        falls back to the topo state.xml for legacy fresh eq runs).
    """
    jd = Path(job_dir)
    progress = _load_progress_v3(jd / "progress.json")
    if progress is None:
        return {}
    nodes_index = progress.get("nodes", {})

    for nid in _iter_ancestor_ids(nodes_index, node_id):
        info = nodes_index.get(nid, {})
        node_type = info.get("type")
        if node_type in ("min", "prod", "eq"):
            state = _read_artifact_from_node(job_dir, nid, "state")
            checkpoint = (
                _read_artifact_from_node(job_dir, nid, "checkpoint")
                if state is None else None
            )
            if state is not None:
                return {
                    "restart_from": state,
                    "restart_from_node_id": nid,
                    "restart_from_node_type": node_type,
                }
            if checkpoint is not None:
                return {
                    "restart_from": checkpoint,
                    "restart_from_node_id": nid,
                    "restart_from_node_type": node_type,
                }
            # Neither artifact. If the ancestor is completed, refuse
            # to skip it — restarting from an older ancestor would
            # silently roll the run back across whatever this node
            # produced. Failed / pending direct parents are already
            # caught by ``_input_resolution_status_errors``; for older
            # non-completed ancestors we keep walking.
            if info.get("status") == "completed":
                return {
                    "restart_from_error": (
                        f"Nearest completed {node_type} ancestor "
                        f"'{nid}' has neither 'state' nor 'checkpoint'; "
                        f"refusing to skip it and restart from an older "
                        f"ancestor. Re-run that node to produce a "
                        f"saveState / saveCheckpoint artifact (or fix "
                        f"the DAG to drop it from the lineage)."
                    ),
                }
    return {}


def _collect_prod_artifact_chain(
    job_dir: str,
    analyze_node_id: str,
    artifact_key: str,
) -> list[str]:
    """Walk the prod lineage above *analyze_node_id* and return a list of
    *artifact_key* paths in chronological order (oldest first).

    Generic BFS through the prod ancestor chain (via
    ``metadata.continued_from``, falling back to ``parent_node_ids``):
    for each prod ancestor, if it carries the named artifact,
    prepend-then-reverse to get chronological order; ancestors that
    lack the artifact are skipped silently (a prod run that never
    completed does not contribute). Non-prod ancestors (typically eq)
    terminate the walk.

    Used for both ``artifact_key="trajectory"`` (concat_trajectory
    input) and ``artifact_key="energy"`` (StateDataReporter CSV
    concatenation alongside). An empty return means no matching prod
    ancestor was found — downstream tools must surface that to the
    caller with a clear error.
    """
    jd = Path(job_dir)
    start_nj = jd / "nodes" / analyze_node_id / "node.json"
    if not start_nj.exists():
        return []
    try:
        start_data = json.loads(start_nj.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    parents = start_data.get("parent_node_ids", [])
    if not parents:
        return []
    # Follow single prod ancestor chain upward. We don't try to merge
    # branching trajectories — an analyze node must have exactly one
    # prod parent; forks are handled by making multiple analyze nodes.
    reversed_chain: list[str] = []
    current = parents[0]
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        nj = jd / "nodes" / current / "node.json"
        if not nj.exists():
            break
        try:
            cur_data = json.loads(nj.read_text())
        except (json.JSONDecodeError, OSError):
            break
        if cur_data.get("node_type") != "prod":
            break
        art = _read_artifact_from_node(job_dir, current, artifact_key)
        if art is not None:
            reversed_chain.append(art)
        # Prefer continued_from metadata (explicit chain marker); fall
        # back to the first parent. Both should point at a prod or eq.
        next_id = cur_data.get("metadata", {}).get("continued_from")
        if not next_id:
            cur_parents = cur_data.get("parent_node_ids", [])
            next_id = cur_parents[0] if cur_parents else None
        current = next_id
    # reversed_chain is leaf→root; flip to chronological.
    reversed_chain.reverse()
    return reversed_chain


def _collect_prod_trajectory_chain(
    job_dir: str, analyze_node_id: str
) -> list[str]:
    """Back-compat wrapper: trajectory chain (see
    :func:`_collect_prod_artifact_chain`)."""
    return _collect_prod_artifact_chain(
        job_dir, analyze_node_id, "trajectory"
    )


def _walk_prod_chain_from(
    job_dir: str, leaf_prod_id: str, artifact_key: str
) -> list[str]:
    """Walk strictly upward from *leaf_prod_id* (which must itself be
    a prod) through prod ancestors, collecting *artifact_key* paths
    in chronological order (oldest first).

    Used by the Phase 3 multi-prod resolver so each parent prod's
    lineage becomes its own independent branch. The shape mirrors
    :func:`_collect_prod_artifact_chain` but starts from the prod
    node directly (not from an analyze node above it) and includes
    the start node in the walk.
    """
    jd = Path(job_dir)
    reversed_chain: list[str] = []
    current: Optional[str] = leaf_prod_id
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        nj = jd / "nodes" / current / "node.json"
        if not nj.exists():
            break
        try:
            cur_data = json.loads(nj.read_text())
        except (json.JSONDecodeError, OSError):
            break
        if cur_data.get("node_type") != "prod":
            break
        art = _read_artifact_from_node(job_dir, current, artifact_key)
        if art is not None:
            reversed_chain.append(art)
        next_id = cur_data.get("metadata", {}).get("continued_from")
        if not next_id:
            cur_parents = cur_data.get("parent_node_ids", [])
            next_id = cur_parents[0] if cur_parents else None
        current = next_id
    reversed_chain.reverse()
    return reversed_chain


def _collect_prod_energy_chain(
    job_dir: str, analyze_node_id: str
) -> list[str]:
    """Energy CSV chain from the same prod lineage that produced the
    trajectory chain. StateDataReporter writes on the same interval as
    DCDReporter in ``run_production``, so energy rows line up with DCD
    frames file-for-file — concat in parallel and a ``--stride N`` on
    the DCD keeps the energy in sync with the same stride applied."""
    return _collect_prod_artifact_chain(job_dir, analyze_node_id, "energy")


def read_ancestor_final_step(
    job_dir: str,
    node_id: str,
    *,
    restart_node_id: object = _RESTART_NODE_ID_UNSET,
) -> Optional[int]:
    """Return the ``metadata.final_step`` of the ancestor whose artifact
    was chosen as the restart source for *node_id*.

    The cumulative step counter the run side restores after
    :meth:`Simulation.loadState` (XML State does not persist
    ``currentStep``) must come from the *same* ancestor whose state /
    checkpoint was loaded. ``run_equilibration`` / ``run_production``
    decide the right node id via ``_resolve_restart_node_id_for_run``
    and then pass it here.

    ``restart_node_id`` has three distinct meanings, all of which must
    be preserved by callers:

      - **Omitted** (no keyword passed): backwards-compatible BFS
        fallback. The helper replays the same per-ancestor BFS as
        ``_resolve_md_restart`` to pick the ancestor that *would* have
        been chosen, then reads its ``metadata.final_step``. Useful for
        non-node-mode callers and pre-Phase-19 code paths; ``run_*``
        always passes an explicit value.
      - **A node id string** (e.g. ``"eq_001"``): read
        ``metadata.final_step`` directly from that node. This is what
        the resolver auto-resolve path and the matched-explicit-path
        path supply.
      - **``None``** (explicit, distinct from omitted): assert
        "external restart file — there is no DAG ancestor whose
        ``final_step`` applies to ``simulation.currentStep``". The
        helper returns ``None`` *without* running the BFS fallback,
        so the run side leaves ``currentStep`` at whatever the loader
        sets (``saveState`` XML → 0; ``saveCheckpoint`` ``.chk`` →
        the persisted counter).

    The omitted-vs-``None`` distinction is enforced by a private
    sentinel default; passing ``None`` explicitly is therefore a
    different signal than not passing the keyword at all.

    Returns ``None`` when:
    - the chosen ancestor has no ``final_step`` metadata (a node whose
      run didn't write it yet);
    - no min / prod / eq ancestor exists at all;
    - the caller explicitly asserted "external restart file" by passing
      ``restart_node_id=None``.
    """
    if restart_node_id is None:
        # Explicit "no DAG ancestor for this restart" — e.g. the run
        # side loaded a user-supplied external state file. The loader
        # decides ``simulation.currentStep`` by itself; we must not
        # overwrite it from a DAG ancestor.
        return None

    if restart_node_id is not _RESTART_NODE_ID_UNSET:
        if not isinstance(restart_node_id, str):
            return None
        v = _read_metadata_field(job_dir, restart_node_id, "final_step")
        return v if isinstance(v, int) else None

    continued_from = _read_continued_from(job_dir, node_id)
    if continued_from is not None:
        v = _read_metadata_field(job_dir, continued_from, "final_step")
        return v if isinstance(v, int) else None

    # Default path: share ``_select_md_restart_ancestor`` so the
    # final_step we return comes from the *same* ancestor whose
    # state / checkpoint the resolver picked. When the resolver
    # surfaces ``restart_from_error`` (e.g. a completed prod with no
    # restart artifact), we return ``None`` rather than walking past
    # the broken ancestor — restoring an older ancestor's
    # ``final_step`` would silently roll the timeline back.
    chosen = _select_md_restart_ancestor(job_dir, node_id)
    chosen_id = chosen.get("restart_from_node_id")
    if chosen_id is None:
        return None
    v = _read_metadata_field(job_dir, chosen_id, "final_step")
    return v if isinstance(v, int) else None


def _find_ancestor_node_id(
    job_dir: str, node_id: str, anc_type: str
) -> Optional[str]:
    """Return the nearest ancestor of *node_id* whose ``node_type`` matches
    *anc_type*, using the same BFS ordering :func:`find_ancestor_artifact`
    uses. Needed so ``read_ancestor_final_step`` reads the metadata from
    the exact ancestor whose artifact ``resolve_node_inputs`` picked."""
    jd = Path(job_dir)
    progress = _load_progress_v3(jd / "progress.json")
    if progress is None:
        return None
    nodes_index = progress.get("nodes", {})
    for cur_id in _iter_ancestor_ids(nodes_index, node_id):
        if nodes_index.get(cur_id, {}).get("type") == anc_type:
            return cur_id
    return None
