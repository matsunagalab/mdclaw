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
from typing import Any, Optional


logger = logging.getLogger(__name__)

from mdclaw.node.graph import find_ancestor_artifact, get_ancestors  # noqa: E402
from mdclaw.node.io import _load_json_artifact, _read_artifact_from_node, _read_continued_from, _read_metadata_field, _read_node_metadata, _sanitize_label  # noqa: E402
from mdclaw.node.lifecycle import read_node, validate_node_execution_context  # noqa: E402
from mdclaw.node.prod_chain import _collect_prod_energy_chain, _collect_prod_trajectory_chain, _find_ancestor_node_id, _select_md_restart_ancestor, _walk_prod_chain_from  # noqa: E402
from mdclaw.node.progress import _load_progress_v3  # noqa: E402


def explain_node(
    job_dir: str,
    node_id: str,
    expected_node_type: Optional[str] = None,
    actual_conditions: Optional[dict] = None,
) -> dict:
    """Return read-only node details plus validation and resolved inputs."""
    jd = Path(job_dir).resolve()
    node_json = jd / "nodes" / node_id / "node.json"
    if not node_json.exists():
        return {
            "success": False,
            "code": "node_missing",
            "message": f"Node '{node_id}' does not exist under {jd}",
            "job_dir": str(jd),
            "node_id": node_id,
        }

    node = read_node(str(jd), node_id)
    node_type = node.get("node_type")
    expected = expected_node_type or node_type
    progress = _load_progress_v3(jd / "progress.json") or {}
    nodes_index = progress.get("nodes", {})
    progress_entry = nodes_index.get(node_id, {})
    parent_statuses = {
        parent_id: (nodes_index.get(parent_id) or {}).get("status")
        for parent_id in node.get("parent_node_ids", [])
    }
    dependency_statuses = {
        dep_id: (nodes_index.get(dep_id) or {}).get("status")
        for dep_id in node.get("dependency_node_ids", [])
    }

    validation = validate_node_execution_context(
        str(jd),
        node_id,
        expected,
        actual_conditions=actual_conditions,
    )
    resolved_inputs = resolve_node_inputs(str(jd), node_id, node_type)
    input_errors = resolved_inputs.get("input_resolution_errors", [])

    return {
        "success": True,
        "code": "ok",
        "job_dir": str(jd),
        "node_id": node_id,
        "node_type": node_type,
        "status": node.get("status"),
        "label": node.get("label"),
        "parents": node.get("parent_node_ids", []),
        "dependencies": node.get("dependency_node_ids", []),
        "parent_statuses": parent_statuses,
        "dependency_statuses": dependency_statuses,
        "conditions": node.get("conditions", {}),
        "artifact_keys": sorted((node.get("artifacts") or {}).keys()),
        "metadata_errors": (node.get("metadata") or {}).get("errors", []),
        "warnings": node.get("warnings", []),
        "progress_entry": progress_entry,
        "validation": validation,
        "ready_to_run": validation.get("success") is True and not input_errors,
        "resolved_inputs": resolved_inputs,
        "missing_inputs": input_errors,
    }


def _input_resolution_status_errors(job_dir: str, node_id: str) -> list[str]:
    """Return parent/dependency status errors for resolver auto-discovery.

    ``resolve_node_inputs`` is intentionally non-throwing so callers can
    still fall back to explicit parameters. The resolver must nevertheless
    avoid trusting artifacts through an incomplete DAG edge.
    """
    jd = Path(job_dir)
    progress = _load_progress_v3(jd / "progress.json")
    if progress is None:
        return [f"progress.json is missing or invalid under {job_dir}"]

    nodes_index = progress.get("nodes", {})
    node_entry = nodes_index.get(node_id)
    if node_entry is None:
        return [f"Node '{node_id}' is missing from progress.json"]

    errors: list[str] = []
    refs = [
        ("Parent", pid) for pid in node_entry.get("parents", [])
    ] + [
        ("Dependency", did) for did in node_entry.get("dependencies", [])
    ]
    for ref_label, ref_id in refs:
        ref_entry = nodes_index.get(ref_id)
        if ref_entry is None:
            errors.append(
                f"{ref_label} node '{ref_id}' is missing from progress.json"
            )
            continue
        status = ref_entry.get("status")
        if status != "completed":
            errors.append(
                f"{ref_label} node '{ref_id}' must be completed before "
                f"resolving inputs for '{node_id}' (status={status!r})"
            )
    return errors


def input_resolution_recovery(job_dir: str, node_id: str) -> Optional[dict]:
    """Structured recovery hint when a node is blocked by a non-completed parent.

    When ``node_id`` cannot resolve inputs because a parent/dependency is stuck
    ``running``/``failed``/``pending`` (not ``completed``), return an
    ``action=create_node`` suggestion targeting the *blocking node's stage* so a
    weak agent re-creates the stuck ancestor instead of re-running the blocked
    child against an unreachable parent. Returns ``None`` when nothing is
    blocking, or on any read error — this is a best-effort hint, not part of the
    tool contract.
    """
    try:
        progress = _load_progress_v3(Path(job_dir) / "progress.json")
        if progress is None:
            return None
        nodes_index = progress.get("nodes", {})
        node_entry = nodes_index.get(node_id)
        if node_entry is None:
            return None
        refs = [
            ("parent", pid) for pid in node_entry.get("parents", [])
        ] + [
            ("dependency", did) for did in node_entry.get("dependencies", [])
        ]
        for ref_kind, ref_id in refs:
            ref_entry = nodes_index.get(ref_id)
            if ref_entry is None or ref_entry.get("status") == "completed":
                continue
            status = ref_entry.get("status")
            ref_type = ref_entry.get("type")
            grandparents = list(ref_entry.get("parents", []) or [])
            cmd = f"mdclaw create_node --job-dir {job_dir} --node-type {ref_type}"
            if grandparents:
                cmd += f" --parent-node-ids {','.join(grandparents)}"
            return {
                "code": "parent_node_not_completed",
                "blocking_node_id": ref_id,
                "blocking_node_type": ref_type,
                "blocking_status": status,
                "action": "create_node",
                "node_type": ref_type,
                "suggested_parent_node_ids": grandparents,
                "next_command": cmd,
                "message": (
                    f"'{node_id}' is blocked because {ref_kind} node '{ref_id}' is "
                    f"{status!r}, not 'completed'. Re-running '{node_id}' will keep "
                    f"failing — create a NEW '{ref_type}' node from "
                    f"{grandparents or 'an earlier completed ancestor'}, run it, "
                    f"then retry '{node_id}'."
                ),
            }
        return None
    except Exception:  # noqa: BLE001
        return None


def _record_input_resolution_error(result: dict, error: str) -> None:
    result.setdefault("input_resolution_errors", []).append(error)
    result.setdefault("input_resolution_error", error)


def _nearest_ancestor_artifact_or_error(
    job_dir: str,
    node_id: str,
    ancestor_type: str,
    artifact_key: str,
):
    """Resolve a required artifact from the nearest ancestor of a given type.

    Unlike ``find_ancestor_artifact``, this helper does not walk past a nearest
    same-type ancestor with a missing artifact. For required workflow inputs,
    doing so can silently bind an older branch's artifact after a weak agent
    created an incomplete nearer node.
    """
    ancestor_id = _find_ancestor_node_id(job_dir, node_id, ancestor_type)
    if ancestor_id is None:
        return None, None, None
    value = _read_artifact_from_node(job_dir, ancestor_id, artifact_key)
    if value is None:
        return (
            None,
            ancestor_id,
            f"Nearest {ancestor_type} ancestor '{ancestor_id}' is completed "
            f"but missing required artifact '{artifact_key}' for '{node_id}'",
        )
    return value, ancestor_id, None


def _resolve_topology_files(job_dir: str, node_id: str) -> dict:
    """Resolve topology artifacts emitted by the nearest topo ancestor.

    Topo nodes built via ``build_amber_system`` / ``build_openmm_system``
    emit a ``system_xml`` + ``topology_pdb`` + ``state_xml`` triple. The
    XML triple is the only supported topology contract on the run side —
    min / eq / prod / analyze refuse to consume any other artifact set.

    **Atomicity guarantee**: all triple components must come from the
    same topo node. We never fall back to an older topo ancestor for
    ``topology_pdb`` / ``state_xml``, because mixing artifacts across
    topo nodes would point min/eq/prod at a different physical System.
    Missing components on the chosen topo node surface as
    ``input_resolution_error`` rather than a silent walk upward.
    """
    result: dict = {}

    topo_id = _find_ancestor_node_id(job_dir, node_id, "topo")
    if topo_id is None:
        _record_input_resolution_error(
            result,
            f"No topo ancestor found for '{node_id}'.",
        )
        return result

    sys_xml = _read_artifact_from_node(job_dir, topo_id, "system_xml")
    topo_pdb = _read_artifact_from_node(job_dir, topo_id, "topology_pdb")
    state_xml = _read_artifact_from_node(job_dir, topo_id, "state_xml")
    if sys_xml is None or topo_pdb is None or state_xml is None:
        _record_input_resolution_error(
            result,
            f"Topo ancestor '{topo_id}' is missing the XML triple: "
            f"system_xml={'ok' if sys_xml else 'MISSING'}, "
            f"topology_pdb={'ok' if topo_pdb else 'MISSING'}, "
            f"state_xml={'ok' if state_xml else 'MISSING'}. The "
            f"triple must be emitted atomically by build_amber_system "
            f"/ build_openmm_system; do not mix artifacts across topo "
            f"nodes.",
        )
        return result
    result["system_xml_file"] = sys_xml
    result["topology_pdb_file"] = topo_pdb
    result["state_xml_file"] = state_xml
    result["topology_resolved_from_node_id"] = topo_id
    # Surface build-time choices the run side needs to validate against
    # runtime kwargs. ``implicit_solvent`` is the load-bearing one
    # (mismatch silently runs the wrong GB model); ``hmr`` and
    # ``solvent_type`` are along for the ride so min/eq/prod can produce
    # cleaner diagnostics without re-deserializing system.xml. Missing
    # metadata keys (hand-built node.json) keep the value as ``None`` so
    # downstream guards can skip the check rather than blocking on noise.
    try:
        topo_meta = read_node(job_dir, topo_id).get("metadata") or {}
    except (OSError, json.JSONDecodeError):
        topo_meta = {}
    result["topology_implicit_solvent"] = topo_meta.get("implicit_solvent")
    result["topology_hmr"] = topo_meta.get("hmr")
    result["topology_solvent_type"] = topo_meta.get("solvent_type")
    return result


def _resolve_topo_inputs(job_dir: str, node_id: str) -> dict:
    result: dict = {}
    solv_anc = _find_ancestor_node_id(job_dir, node_id, "solv")
    if solv_anc is not None:
        v = _read_artifact_from_node(job_dir, solv_anc, "solvated_pdb")
        if v:
            result["pdb_file"] = v
            result["pdb_resolved_from_node_id"] = solv_anc
        else:
            _record_input_resolution_error(
                result,
                f"Nearest solv ancestor '{solv_anc}' is completed but missing "
                f"required artifact 'solvated_pdb' for '{node_id}'",
            )
    else:
        v, prep_id, error = _nearest_ancestor_artifact_or_error(
            job_dir, node_id, "prep", "merged_pdb"
        )
        if error:
            _record_input_resolution_error(result, error)
        if v:
            result["pdb_file"] = v
            result["pdb_resolved_from_node_id"] = prep_id

    for result_key, artifact_key in (
        ("ligand_chemistry", "ligand_chemistry"),
        ("metal_params", "metal_params"),
    ):
        value = find_ancestor_artifact(job_dir, node_id, "prep", artifact_key)
        if value:
            result[result_key] = value

    for result_key, artifact_key, expected_type in (
        ("modxna_params", "modxna_params", list),
        ("disulfide_bonds", "disulfide_bonds", list),
        ("glycan_metadata", "glycan_metadata", dict),
        ("glycan_linkages", "glycan_linkages", list),
    ):
        value = find_ancestor_artifact(job_dir, node_id, "prep", artifact_key)
        loaded = _load_json_artifact(value, expected_type)
        if loaded is not None:
            result[result_key] = loaded

    bd = find_ancestor_artifact(job_dir, node_id, "solv", "box_dimensions")
    loaded_box = _load_json_artifact(bd, dict)
    if loaded_box is not None:
        result["box_dimensions"] = loaded_box

    if solv_anc is not None:
        is_membrane = _read_metadata_field(job_dir, solv_anc, "is_membrane")
        if isinstance(is_membrane, bool):
            result["is_membrane"] = is_membrane
        solv_water_model = _read_metadata_field(job_dir, solv_anc, "water_model")
        if isinstance(solv_water_model, str):
            result["solvation_water_model"] = solv_water_model
    return result


def _resolve_md_restart(job_dir: str, node_id: str) -> dict:
    """Locate the restart artifact for an MD node (eq or prod).

    Search order: explicit ``continue_from`` ancestor first, then walk
    parents in BFS order. For each ancestor whose ``node_type`` is
    ``"min"``, ``"prod"``, or ``"eq"``, prefer the portable ``state``
    (XML) artifact and fall back to ``checkpoint`` (binary, same-GPU
    bit-exact replay) *on that same ancestor* before considering older
    ancestors. Two invariants:

    1. A near prod/eq/min ancestor that only carries a checkpoint wins against
       a far ancestor that has a state — the alternative silently rolls
       the run back across an unsaved node step.
    2. A *completed* min/prod/eq ancestor that carries neither artifact
       blocks resolution outright (``restart_from_error``). Skipping
       past it would lose whatever the user's tool produced on that
       node; the right answer is to re-run that node, not pretend it
       didn't run.

    Returns a dict with one of:
      - ``restart_from`` (str path) + ``restart_from_node_id`` (str) on
        a successful match. ``read_ancestor_final_step`` must read
        ``metadata.final_step`` from that same node id so the
        cumulative step counter matches the loaded artifact.
      - ``restart_from_error`` (str) for ``continue_from`` that names a
        node without either artifact, or for a completed min/prod/eq
        ancestor that carries neither artifact.
      - empty when no min/prod/eq ancestor exists at all (legacy fresh
        eq run).

    Eq and prod nodes use the same resolver — min → eq, eq → eq, and
    prod → prod all share the same ancestor-selection invariants.
    """
    result: dict = {}
    continued_from = _read_continued_from(job_dir, node_id)
    if continued_from is not None:
        src = _read_artifact_from_node(job_dir, continued_from, "state")
        if src is None:
            src = _read_artifact_from_node(job_dir, continued_from, "checkpoint")
        if src is not None:
            result["restart_from"] = src
            result["restart_from_node_id"] = continued_from
            try:
                node = read_node(job_dir, continued_from)
                result["restart_from_node_type"] = (
                    node.get("type") or node.get("node_type")
                )
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                pass
        else:
            result["restart_from_error"] = (
                f"continue_from='{continued_from}' but that node has neither a "
                f"'state' nor 'checkpoint' artifact — extension cannot start. "
                f"Wait for that node to finish (or fix the DAG to point at a "
                f"completed min/eq/prod ancestor)."
            )
        return result

    return _select_md_restart_ancestor(job_dir, node_id)


# Backwards-compatible alias for callers that import the prod-specific name.


_resolve_prod_restart = _resolve_md_restart


def _resolve_eq_ensemble_metadata(job_dir: str, node_id: str) -> dict:
    result: dict = {}
    eq_anc = _find_ancestor_node_id(job_dir, node_id, "eq")
    if eq_anc is None:
        return result
    fe = _read_metadata_field(job_dir, eq_anc, "final_ensemble")
    if isinstance(fe, str):
        result["eq_final_ensemble"] = fe
    pb = _read_metadata_field(job_dir, eq_anc, "pressure_bar")
    if isinstance(pb, (int, float)):
        result["eq_pressure_bar"] = float(pb)
    return result


def resolve_node_inputs(
    job_dir: str,
    node_id: str,
    node_type: str,
) -> dict:
    """Auto-resolve standard input files for a node from its DAG ancestors.

    Returns a dict of resolved absolute paths.  Missing artifacts are
    omitted (the caller should fall back to explicit parameters).

    Mappings:

    - ``prep``: selected structure from the job's source bundle, falling back
                to legacy ``structure_file`` artifacts when needed.
    - ``solv``: ``merged_pdb`` from nearest ``prep`` ancestor
    - ``topo``: ``solvated_pdb`` / ``box_dimensions`` from nearest ``solv``
                ancestor, plus ``ligand_chemistry`` / ``metal_params`` from
                nearest ``prep`` ancestor
    - ``min``:  same topology artifacts as ``eq``; writes a minimized
                portable state for downstream equilibration
    - ``eq``:   ``system_xml`` + ``topology_pdb`` + ``state_xml`` from nearest
                ``topo`` ancestor (XML triple is the only supported
                topology contract on the run side), plus ``state`` from a
                nearest ``min`` or prior ``eq`` / ``prod`` ancestor when present
    - ``prod``: same topology artifacts as ``eq``;
                ``checkpoint`` / ``state`` from nearest ``eq`` ancestor (parent)
    """
    result: dict = {}
    status_errors = _input_resolution_status_errors(job_dir, node_id)
    if status_errors:
        return {
            "input_resolution_error": status_errors[0],
            "input_resolution_errors": status_errors,
        }

    if node_type == "prep":
        ancestors = get_ancestors(job_dir, node_id)
        pj = Path(job_dir) / "progress.json"
        progress = _load_progress_v3(pj)
        if progress is not None:
            nodes_index = progress.get("nodes", {})
            source_ancestors = [
                nid for nid in ancestors
                if nodes_index.get(nid, {}).get("type") == "source"
            ]
            if len(source_ancestors) == 1:
                source_id = _find_ancestor_node_id(job_dir, node_id, "source")
                bundle_file = (
                    _read_artifact_from_node(job_dir, source_id, "source_bundle")
                    if source_id is not None
                    else None
                )
                if bundle_file:
                    try:
                        from mdclaw.source_bundle import (
                            load_source_bundle,
                            source_record_path,
                        )

                        bundle = load_source_bundle(bundle_file)
                        structures = [
                            s for s in bundle.get("structures", [])
                            if isinstance(s, dict)
                        ]
                        source_node_dir = Path(job_dir) / "nodes" / str(source_id)
                        result.update({
                            "source_bundle_file": bundle_file,
                            "source_bundle_resolved_from_node_id": source_id,
                            "source_structure_count": len(structures),
                        })
                        if len(structures) == 1:
                            record = structures[0]
                            structure_file = source_record_path(record, source_node_dir)
                            result.update({
                                "structure_file": str(structure_file),
                                "structure_resolved_from_node_id": source_id,
                                "source_structure_id": record.get("structure_id"),
                                "source_structure": record,
                            })
                    except Exception as exc:
                        _record_input_resolution_error(result, str(exc))
                else:
                    v, source_id, error = _nearest_ancestor_artifact_or_error(
                        job_dir, node_id, "source", "structure_file"
                    )
                    if error:
                        _record_input_resolution_error(result, error)
                    if v:
                        result["structure_file"] = v
                        result["structure_resolved_from_node_id"] = source_id

    elif node_type == "solv":
        v, prep_id, error = _nearest_ancestor_artifact_or_error(
            job_dir, node_id, "prep", "merged_pdb"
        )
        if error:
            _record_input_resolution_error(result, error)
        if v:
            result["pdb_file"] = v
            result["pdb_resolved_from_node_id"] = prep_id

    elif node_type == "topo":
        result.update(_resolve_topo_inputs(job_dir, node_id))

    elif node_type == "min":
        result.update(_resolve_topology_files(job_dir, node_id))
        topo_anc = _find_ancestor_node_id(job_dir, node_id, "topo")
        if topo_anc is not None:
            is_membrane = _read_metadata_field(job_dir, topo_anc, "is_membrane")
            if isinstance(is_membrane, bool):
                result["is_membrane"] = is_membrane

    elif node_type == "eq":
        result.update(_resolve_topology_files(job_dir, node_id))
        topo_anc = _find_ancestor_node_id(job_dir, node_id, "topo")
        if topo_anc is not None:
            is_membrane = _read_metadata_field(job_dir, topo_anc, "is_membrane")
            if isinstance(is_membrane, bool):
                result["is_membrane"] = is_membrane
        # min → eq is the standard path: surface the min node's state XML so
        # equilibration starts from explicitly minimized coordinates. eq → eq
        # chaining continues to surface the parent eq state for multi-stage
        # equilibration. A legacy first eq node from topo has no restart
        # ancestor and runs directly from the topo state.xml.
        result.update(_resolve_md_restart(job_dir, node_id))

    elif node_type == "prod":
        result.update(_resolve_topology_files(job_dir, node_id))
        topo_anc = _find_ancestor_node_id(job_dir, node_id, "topo")
        if topo_anc is not None:
            is_membrane = _read_metadata_field(job_dir, topo_anc, "is_membrane")
            if isinstance(is_membrane, bool):
                result["is_membrane"] = is_membrane
        result.update(_resolve_md_restart(job_dir, node_id))

        # Surface the eq ancestor's ensemble as a default-pressure hint
        # so a prod that omits ``--pressure-bar`` still defaults to NPT
        # when its eq ran NPT. The ensemble-agnostic state loader handles
        # the cross-ensemble case safely (positions/velocities/box are
        # transferred without restoring barostat parameters), so this
        # inheritance is a UX convenience rather than a correctness
        # requirement.
        result.update(_resolve_eq_ensemble_metadata(job_dir, node_id))

    elif node_type == "analyze":
        # Analyze nodes resolve inputs based on how many parents they
        # have and whether those parents are prods or analyze nodes.
        # Four combinations, documented inline below. create_node
        # already validated that parents are non-empty and uniform
        # (all-prod or all-analyze); this branch trusts that contract.
        start_nj = Path(job_dir) / "nodes" / node_id / "node.json"
        if not start_nj.exists():
            return result
        try:
            start_data = json.loads(start_nj.read_text())
        except (json.JSONDecodeError, OSError):
            return result
        parents: list[str] = start_data.get("parent_node_ids", [])
        if not parents:
            return result

        parent_types: list[str] = []
        for pid in parents:
            pj = Path(job_dir) / "nodes" / pid / "node.json"
            if not pj.exists():
                parent_types.append("missing")
                continue
            try:
                parent_types.append(
                    json.loads(pj.read_text()).get("node_type", "unknown")
                )
            except (json.JSONDecodeError, OSError):
                parent_types.append("unreadable")

        n_parents = len(parents)
        # Every analyze branch needs the same topology for atom-selection
        # DSL evaluation — within one job_dir all prods share the topo.
        # The modern XML triple's ``topology_pdb`` is mdtraj-compatible
        # and is the only topology source the resolver returns.
        topology = find_ancestor_artifact(
            job_dir, node_id, "topo", "topology_pdb"
        )
        if topology:
            result["topology_file"] = topology

        if n_parents == 1 and parent_types[0] == "prod":
            # Phase 1 single-prod shape: trajectory + energy chain
            # collected chronologically along the prod lineage.
            result["trajectory_chain"] = _collect_prod_trajectory_chain(
                job_dir, node_id
            )
            result["energy_chain"] = _collect_prod_energy_chain(
                job_dir, node_id
            )
        elif n_parents >= 1 and all(pt == "prod" for pt in parent_types):
            # Phase 3 multi-prod shape: each parent is an independent
            # leaf prod; walk its own chain and produce one branch
            # input per parent.
            branches_input: list[dict[str, Any]] = []
            for pid in parents:
                # Borrow the chain collector by pointing a synthetic
                # analyze node at this prod. Simplest is to walk
                # directly from the parent id.
                traj_chain = _walk_prod_chain_from(
                    job_dir, pid, "trajectory"
                )
                energy_chain = _walk_prod_chain_from(
                    job_dir, pid, "energy"
                )
                conditions = _read_node_metadata(job_dir, pid).get(
                    "conditions", {}
                )
                branches_input.append(
                    {
                        "label": _sanitize_label(pid),
                        "leaf_prod_id": pid,
                        "trajectory_chain": traj_chain,
                        "energy_chain": energy_chain,
                        "conditions": conditions,
                    }
                )
            result["branches_input"] = branches_input
        elif n_parents == 1 and parent_types[0] == "analyze":
            # Phase 2/3 single-analyze parent. If the parent itself is
            # multi-branch, propagate its branches structured artifact
            # so downstream tools iterate the same way regardless of
            # how the multi-ness arose.
            parent_branches = _read_artifact_from_node(
                job_dir, parents[0], "branches"
            )
            if isinstance(parent_branches, list):
                # Multi-branch propagation. The `branches` artifact
                # stores per-branch paths as node-relative strings
                # (``artifacts/combined_<label>.dcd``); resolve them
                # to absolute paths here so downstream tools don't
                # have to re-derive the parent node's directory.
                parent_node_dir = (
                    Path(job_dir) / "nodes" / parents[0]
                )

                def _abs(rel: Optional[str]) -> Optional[str]:
                    if not rel:
                        return None
                    p = Path(rel)
                    return str(p if p.is_absolute() else (parent_node_dir / rel).resolve())

                result["branches_input"] = [
                    {
                        "label": b.get("label"),
                        "leaf_prod_id": b.get("leaf_prod_id"),
                        "trajectory_file": _abs(
                            b.get("fitted_trajectory")
                            or b.get("combined_trajectory")
                            or b.get("trajectory")
                        ),
                        "energy_file": _abs(
                            b.get("combined_energy") or b.get("energy")
                        ),
                        "conditions": b.get("conditions", {}),
                    }
                    for b in parent_branches
                ]
                # Shared reference_pdb lives at the parent's top-level
                ref_pdb = _read_artifact_from_node(
                    job_dir, parents[0], "reference_pdb"
                )
                if ref_pdb is not None:
                    result["reference_pdb"] = ref_pdb
            else:
                # Single-trajectory parent (Phase 1 concat output, or
                # a single-parent Phase 2 output). Prefer fitted over
                # combined so a fit→rmsd chain picks up aligned frames.
                traj = _read_artifact_from_node(
                    job_dir, parents[0], "fitted_trajectory"
                ) or _read_artifact_from_node(
                    job_dir, parents[0], "combined_trajectory"
                )
                if traj is not None:
                    result["trajectory_file"] = traj
                ref_pdb = _read_artifact_from_node(
                    job_dir, parents[0], "reference_pdb"
                )
                if ref_pdb is not None:
                    result["reference_pdb"] = ref_pdb
        elif n_parents >= 2 and all(pt == "analyze" for pt in parent_types):
            # Phase 3 multi-analyze shape: each parent contributes one
            # branch (its combined_trajectory, preferring fitted).
            # reference_pdb is taken from the first parent as a shared
            # topology reference — create_node does not enforce that
            # the parents share one; caller is responsible for sanity.
            ref_pdb: Optional[str] = None
            branches_input = []
            for pid in parents:
                traj = _read_artifact_from_node(
                    job_dir, pid, "fitted_trajectory"
                ) or _read_artifact_from_node(
                    job_dir, pid, "combined_trajectory"
                )
                energy = _read_artifact_from_node(
                    job_dir, pid, "combined_energy"
                )
                conditions = _read_node_metadata(job_dir, pid).get(
                    "conditions", {}
                )
                if ref_pdb is None:
                    ref_pdb = _read_artifact_from_node(
                        job_dir, pid, "reference_pdb"
                    )
                branches_input.append(
                    {
                        "label": _sanitize_label(pid),
                        "leaf_prod_id": None,
                        "trajectory_file": traj,
                        "energy_file": energy,
                        "conditions": conditions,
                    }
                )
            result["branches_input"] = branches_input
            if ref_pdb is not None:
                result["reference_pdb"] = ref_pdb
        # Other shapes were rejected at create_node time.

    return result
