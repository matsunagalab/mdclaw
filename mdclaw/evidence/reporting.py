"""Deterministic target-scoped DAG reports; no trajectory pooling or inferred methods."""

import hashlib
import json
from pathlib import Path
from typing import Optional
import xml.etree.ElementTree as ET

from mdclaw.node.io import _atomic_write_json, _read_node_json_path
from mdclaw.study._base import _study_plan_path
from mdclaw.evidence.citations import select_citations


def _json(path):
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return data


def _job(path):
    jd = Path(path).expanduser().resolve()
    nodes = {}
    for file in sorted((jd / "nodes").glob("*/node.json")):
        node = _read_node_json_path(file, strict=True)
        if not isinstance(node, dict) or node.get("node_id") != file.parent.name:
            raise ValueError(f"Invalid node identity: {file}")
        for field in ("parent_node_ids", "dependency_node_ids"):
            refs = node.get(field, [])
            if not isinstance(refs, list) or not all(isinstance(x, str) for x in refs):
                raise ValueError(f"Invalid {field}: {file}")
        nodes[node["node_id"]] = node
    if not nodes:
        raise ValueError(f"No node records under {jd}")
    return jd, nodes


def _lineage(nodes, target):
    ordered, active, seen = [], set(), set()

    def visit(nid):
        if nid in active:
            raise ValueError(f"Cycle in DAG at {nid}")
        if nid in seen:
            return
        if nid not in nodes:
            raise ValueError(f"Missing node: {nid}")
        active.add(nid)
        node = nodes[nid]
        for parent in sorted(set(node.get("parent_node_ids", []) +
                                 node.get("dependency_node_ids", []))):
            visit(parent)
        active.remove(nid)
        seen.add(nid)
        ordered.append(nid)

    visit(target)
    return ordered


def _ancestors(nodes, target):
    """Parent edges only: dependencies are not simulation ancestry."""
    found, pending = set(), [target]
    while pending:
        nid = pending.pop()
        if nid not in found:
            found.add(nid)
            pending.extend(nodes[nid].get("parent_node_ids", []))
    return found


def _force_settings(force):
    # Per-bond/particle parameters and nested forces live below Force attributes.
    # Keep the report bounded while comparing their complete serialized definition;
    # canonical XML ignores indentation and attribute order.
    definition = ET.canonicalize(ET.tostring(force, encoding="unicode"), strip_text=True)
    return {**force.attrib, "definition_sha256": hashlib.sha256(definition.encode()).hexdigest()}


def _runtime(node_dir, artifacts):
    facts, sources, warnings = {}, {}, []
    for key in ("integrator", "runtime_system"):
        value = artifacts.get(key)
        if not isinstance(value, str):
            continue
        path = (node_dir / value).resolve()
        if not path.is_file():
            warnings.append(f"Missing runtime artifact: {path}")
            continue
        raw = path.read_bytes()
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            warnings.append(f"Invalid runtime XML {path}: {exc}")
            continue
        sources[key] = {"file": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
        if key == "integrator":
            if root.tag != "Integrator":
                raise ValueError(f"Expected Integrator XML: {path}")
            facts[key] = dict(root.attrib)
        else:
            if root.tag != "System":
                raise ValueError(f"Expected System XML: {path}")
            facts[key] = {
                "openmm_version": root.get("openmmVersion"),
                "constraint_count": len(root.findall("./Constraints/Constraint")),
                "forces": [_force_settings(force) for force in root.findall("./Forces/Force")],
            }
    return facts, sources, warnings


def _history(jd, nodes, ids):
    records = []
    for nid in ids:
        node = nodes[nid]
        path = jd / "nodes" / nid / "node.json"
        raw = path.read_bytes()
        if json.loads(raw) != node:
            raise ValueError(f"Node changed while reporting; retry: {path}")
        artifacts, metadata, conditions = (node.get(k, {}) for k in
                                            ("artifacts", "metadata", "conditions"))
        if not all(isinstance(x, dict) for x in (artifacts, metadata, conditions)):
            raise ValueError(f"Invalid conditions/metadata/artifacts: {path}")
        runtime, sources, warnings = _runtime(path.parent, artifacts)
        records.append({
            "node_id": nid, "node_type": node.get("node_type"),
            "status": node.get("status"), "label": node.get("label"),
            "parent_node_ids": node.get("parent_node_ids", []),
            "dependency_node_ids": node.get("dependency_node_ids", []),
            "declared_conditions": conditions, "recorded_metadata": metadata,
            "artifacts": artifacts, "artifact_base_dir": str(path.parent),
            "runtime": runtime, "runtime_sources": sources,
            "warnings": node.get("warnings", []) + warnings,
            "source": {"file": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                       "conditions_pointer": "/conditions", "metadata_pointer": "/metadata",
                       "artifacts_pointer": "/artifacts"},
        })
    return records


# Compare settings, never metrics/scheduler IDs. Other raw fields remain in history.
_SETTINGS = (
    "temperature_kelvin", "pressure_bar", "timestep_fs", "simulation_time_ns",
    "output_frequency_ps", "random_seed", "hmr", "platform", "solvent_type",
    "protein_forcefield", "water_model", "forcefield", "is_membrane",
    "integrator_signature", "implicit_solvent", "nonbonded_cutoff_nm",
    "effective_forcefield", "forcefield_provenance",
    "restraint_atoms", "restraint_force_constant", "restraint_count",
    "restraint_counts_by_component", "restraint_selection_source",
    "lipid_restraint_force_constant", "lipid_headgroup_restraint_force_constant",
    "lipid_headgroup_restraint_count", "distance_restraints", "distance_restraint_signature",
    "custom_force", "custom_force_signature", "custom_force_parameters",
    "steering_time_ns", "steering_update_interval_ps", "steering", "sampling_role", "plumed",
)


def _recorded_settings(metadata):
    selected = {key: metadata[key] for key in _SETTINGS if key in metadata}
    for key in ("steering", "plumed"):
        if isinstance(selected.get(key), dict):
            # Runtime summaries mix the protocol with completion metrics and file
            # locators. Compare the protocol, retaining the complete summary in history.
            selected[key] = {k: v for k, v in selected[key].items()
                             if k not in ("elapsed_steps", "schedule_complete", "progress",
                                          "initial_file")}
    return selected


def _flatten(value, prefix):
    if isinstance(value, dict) and value:
        return {k: v for key in sorted(value)
                for k, v in _flatten(value[key], prefix + "/" + key).items()}
    return {prefix: value}


def _comparison(subjects):
    settings = []
    for subject in subjects:
        values, counts = {}, {}
        for record in subject["history"]:
            if record["node_id"] not in subject["ancestor_node_ids"]:
                continue
            stage = record["node_type"] or "unknown"
            counts[stage] = counts.get(stage, 0) + 1
            prefix = f"{stage}/{counts[stage]}"
            values.update(_flatten(record["declared_conditions"], prefix + "/declared"))
            selected = _recorded_settings(record["recorded_metadata"])
            values.update(_flatten(selected, prefix + "/recorded"))
            values.update(_flatten(record["runtime"], prefix + "/runtime"))
        settings.append(values)
    common, differences = {}, {}
    for key in sorted(set().union(*(set(s) for s in settings))):
        rows = [{"label": sub["label"], "present": key in vals, "value": vals.get(key)}
                for sub, vals in zip(subjects, settings)]
        if all(r["present"] and r["value"] == rows[0]["value"] for r in rows):
            common[key] = rows[0]["value"]
        else:
            differences[key] = rows
    return {"common_recorded_settings": common, "differences": differences,
            "scope": "Stage occurrences along parent ancestry; declarations and runtime stay separate.",
            "same_physical_conditions_verified": False}


def generate_md_report(
    job_dir: Optional[str] = None,
    study_dir: Optional[str] = None,
    targets: Optional[list[dict]] = None,
    grouping: Optional[str] = None,
    plan_id: Optional[str] = None,
    question: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> dict:
    """Report selected DAG histories, settings, results and verified citations.

    Provide job_dir, study_dir (optionally plan_id), OR targets containing
    {job_dir, node_id, label}. Multiple leaves require explicit targets and
    grouping='replicas' or 'separate'; no statistical pooling is performed.
    Without output_dir, return JSON only. With it, create a NEW directory with
    report.json and references.bib; existing outputs are never overwritten.
    """
    try:
        if sum(x is not None for x in (job_dir, study_dir, targets)) != 1:
            raise ValueError("Provide exactly one of job_dir, study_dir, targets")
        if grouping not in (None, "replicas", "separate"):
            raise ValueError("grouping must be replicas or separate")
        if plan_id is not None and study_dir is None:
            raise ValueError("plan_id requires study_dir")
        jobs, candidates, study = {}, [], None
        if targets is not None:
            if not isinstance(targets, list) or not targets:
                raise ValueError("targets must be a nonempty list")
            for target in targets:
                if not isinstance(target, dict) or set(target) - {"job_dir", "node_id", "label"}:
                    raise ValueError("Each target accepts only job_dir, node_id, label")
                if not all(isinstance(target.get(k), str) and target[k]
                           for k in ("job_dir", "node_id", "label")):
                    raise ValueError("Each target requires nonempty job_dir, node_id, label")
                jd = Path(target["job_dir"]).expanduser().resolve()
                if str(jd) not in jobs:
                    jobs[str(jd)] = _job(jd)[1]
                candidates.append({**target, "job_dir": str(jd)})
        else:
            paths = [job_dir] if job_dir else []
            if study_dir:
                sd = Path(study_dir).expanduser().resolve()
                study = _json(sd / "study.json")
                plan_path = _study_plan_path(sd, plan_id)
                plan = _json(plan_path) if plan_path.exists() else None
                if plan_id is not None and plan is None:
                    raise ValueError(f"Study plan not found: {plan_path}")
                registered = {j["job_id"]: j for j in study.get("jobs", [])}
                ids = [j["job_id"] for j in plan["plan"].get("jobs", [])] if plan else list(registered)
                missing = sorted(set(ids) - set(registered))
                if missing:
                    raise ValueError(f"Planned jobs not registered: {missing}")
                paths = [sd / registered[j]["job_dir"] for j in ids]
                study = {"file": str(sd / "study.json"), "record": study,
                         "plan_file": str(plan_path) if plan else None, "plan": plan}
            for path in paths:
                jd, nodes = _job(path)
                jobs[str(jd)] = nodes
                referenced = {p for n in nodes.values() for p in n.get("parent_node_ids", [])}
                candidates.extend({"job_dir": str(jd), "node_id": nid,
                                   "label": f"{jd.name}:{nid}", "status": nodes[nid].get("status")}
                                  for nid in sorted(set(nodes) - referenced))
        if not candidates:
            raise ValueError("No targets/leaves found")
        if len(candidates) > 1 and (targets is None or grouping is None):
            return {"success": False, "code": "report_selection_required",
                    "candidates": candidates,
                    "message": "Ask the user to combine replicas, report separately, or select/omit targets. "
                               "Then pass explicit targets with unique labels and grouping.",
                    "study": study}
        labels = [t["label"] for t in candidates]
        identities = [(t["job_dir"], t["node_id"]) for t in candidates]
        if len(set(labels)) != len(labels) or len(set(identities)) != len(identities):
            raise ValueError("Duplicate target identity or label")
        subjects = []
        for target in candidates:
            jd, nid = Path(target["job_dir"]), target["node_id"]
            nodes = jobs[str(jd)]
            ids = _lineage(nodes, nid)
            ancestors = _ancestors(nodes, nid)
            history = _history(jd, nodes, ids)
            production = [i for i in ids if i in ancestors and nodes[i].get("node_type") == "prod"]
            frontier = [i for i in production if not any(
                i != other and i in _ancestors(nodes, other) for other in production)]
            subjects.append({**target, "status": nodes[nid].get("status"),
                             "ancestor_node_ids": sorted(ancestors), "history": history,
                             "production_node_ids": production, "production_frontier": frontier,
                             "analysis_results": [r for r in history if r["node_type"] == "analyze"
                                                  and r["status"] == "completed"]})
        relationships = []
        for i, a in enumerate(subjects):
            for b in subjects[i + 1:]:
                same_job = a["job_dir"] == b["job_dir"]
                shared = sorted(set(a["ancestor_node_ids"]) & set(b["ancestor_node_ids"])) if same_job else []
                shared_prod = sorted(set(a["production_node_ids"]) & set(b["production_node_ids"])) if same_job else []
                nested = a["node_id"] in shared or b["node_id"] in shared
                relationships.append({"labels": [a["label"], b["label"]],
                                      "shared_ancestors": shared, "shared_production": shared_prod,
                                      "ancestor_descendant": nested})
                same_trajectory = same_job and bool(set(a["production_frontier"]) & set(b["production_frontier"]))
                if grouping == "replicas" and (same_trajectory or nested):
                    raise ValueError("Replica targets overlap at their production frontier; use separate "
                                     "to describe continuations or analyses of the same trajectory")
        citations = select_citations(subjects)
        report = {"schema_version": 2, "question": question, "grouping": grouping or "single",
                  "subjects": subjects, "relationships": relationships,
                  "comparison": _comparison(subjects), "study": study,
                  "citations": citations,
                  "limitations": ["Recorded evidence is not proof of convergence, independence, or physical validity.",
                                  "No trajectories or replica statistics are pooled; missing facts are not inferred.",
                                  "Citation coverage is explicit and may be incomplete for custom or legacy methods."]}
        files = {}
        if output_dir is not None:
            out = Path(output_dir).expanduser().resolve()
            if any(out.is_relative_to(Path(j) / "nodes") for j in jobs):
                raise ValueError("output_dir must be outside node directories")
            out.mkdir(parents=True, exist_ok=False)
            _atomic_write_json(out / "report.json", report)
            (out / "references.bib").write_text(citations["bibtex"])
            files = {"report": str(out / "report.json"), "bibtex": str(out / "references.bib")}
        return {"success": True, "code": "ok", "report": report, "files": files}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {"success": False, "code": "report_invalid_input", "errors": [str(exc)]}
