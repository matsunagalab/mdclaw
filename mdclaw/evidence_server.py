"""Evidence report generation for MDClaw jobs and optional studies."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import ensure_directory, setup_logger
from mdclaw.evidence_schema import base_evidence_report

logger = setup_logger(__name__)

_ANALYZE_METRIC_KEYS = {
    "n_frames",
    "total_frames",
    "mean_rmsd_nm",
    "std_rmsd_nm",
    "max_rmsd_nm",
    "mean_fit_rmsd_nm",
    "mean_q",
    "final_q",
    "n_series",
}

_LEAF_PRIORITY = {
    "analyze": 0,
    "prod": 1,
    "eq": 2,
    "topo": 3,
    "solv": 4,
    "prep": 5,
    "source": 6,
    "fetch": 6,
}

_CITATION_INVENTORY_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "research" / "mdclaw_citation_inventory.md"
)

_CITATION_KEY_BY_TOKEN = {
    "openmm": "Eastman2024OpenMM8",
    "ambertools": "Case2023AmberTools",
    "packmol": "Martinez2009Packmol",
    "packmol_memgen": "SchottVerdugo2019PackmolMemgen",
    "memembed": "Nugent2013Memembed",
    "ff19sb": "Tian2020ff19SB",
    "ff14sb": "Maier2015ff14SB",
    "gaff": "Wang2004GAFF",
    "gaff2": "Wang2004GAFF",
    "am1bcc": "Jakalian2002AM1BCC",
    "opc": "Izadi2014OPC",
    "opc3": "Izadi2016OPC3",
    "tip3p": "Jorgensen1983TIP3P",
    "spce": "Berendsen1987SPCE",
    "tip4pew": "Horn2004TIP4PEw",
    "glycam": "Kirschner2008GLYCAM06",
    "ol15": "Zgarbova2015OL15",
    "ol3": "Zgarbova2011OL3",
    "phosaa": "Raguette2024Phosaa",
    "pdb2pqr": "Dolinsky2004PDB2PQR",
    "propka": "Olsson2011PROPKA3",
    "rdkit": "RDKitZenodo",
    "gemmi": "Wojdyr2022Gemmi",
    "dimorphite": "Ropp2019DimorphiteDL",
    "faspr": "Huang2020FASPR",
    "modxna": "Love2024modXNA",
    "plip": "Salentin2015PLIP",
    "plip2021": "Adasme2021PLIP",
    "boltz2": "Passaro2025Boltz2",
    "modeller": "Sali1993MODELLER",
    "alphafold": "Jumper2021AlphaFold",
    "alphafold_db": "Varadi2022AlphaFoldDB",
    "rcsb_pdb": "Burley2025RCSBPDB",
    "uniprot": "UniProt2025",
    "mdtraj": "McGibbon2015MDTraj",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(str(tmp), str(path))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(str(tmp), str(path))


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _read_citation_inventory(path: str | Path | None = None) -> dict[str, str]:
    """Return BibTeX entries keyed by citation key from the inventory markdown."""
    inventory_path = Path(path).expanduser() if path else _CITATION_INVENTORY_PATH
    if not inventory_path.exists():
        return {}
    entries: dict[str, str] = {}
    current: list[str] = []
    current_key: str | None = None
    for line in inventory_path.read_text().splitlines():
        if line.startswith("@"):
            current = [line]
            match = re.match(r"@\w+\{([^,]+),", line)
            current_key = match.group(1) if match else None
            continue
        if current:
            current.append(line)
            if line == "}":
                if current_key:
                    entries[current_key] = "\n".join(current)
                current = []
                current_key = None
    return entries


def _read_job(job_dir: str | Path) -> tuple[Path, dict, dict[str, dict]]:
    jd = Path(job_dir).expanduser().resolve()
    progress = _read_json(jd / "progress.json") or {}
    nodes: dict[str, dict] = {}
    nodes_dir = jd / "nodes"
    if nodes_dir.is_dir():
        for node_dir in sorted(nodes_dir.iterdir()):
            node_json = node_dir / "node.json"
            if not node_json.exists():
                continue
            data = _read_json(node_json)
            if isinstance(data, dict):
                nodes[str(data.get("node_id") or node_dir.name)] = data
    return jd, progress, nodes


def _node_type(node: dict) -> str:
    return str(node.get("node_type") or "unknown")


def _node_metadata(node: dict) -> dict:
    metadata = node.get("metadata", {})
    return metadata if isinstance(metadata, dict) else {}


def _node_conditions(node: dict) -> dict:
    conditions = node.get("conditions", {})
    return conditions if isinstance(conditions, dict) else {}


def _node_artifacts(node: dict) -> dict:
    artifacts = node.get("artifacts", {})
    return artifacts if isinstance(artifacts, dict) else {}


def _node_label(node: dict) -> str | None:
    label = node.get("label")
    return str(label) if label is not None else None


def _choose_terminal_node(
    nodes: dict[str, dict],
    node_id: str | None = None,
) -> tuple[str | None, list[str], list[str]]:
    """Choose a terminal node, preferring completed analysis/production leaves."""
    if node_id:
        if node_id in nodes:
            return node_id, [], []
        return None, [], [f"node_id not found: {node_id}"]

    children: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    for child_id, node in nodes.items():
        for parent_id in node.get("parent_node_ids", []) or []:
            children.setdefault(str(parent_id), set()).add(child_id)
        continued_from = _node_metadata(node).get("continued_from")
        if isinstance(continued_from, str) and continued_from:
            children.setdefault(continued_from, set()).add(child_id)

    leaves = [
        node_id
        for node_id, node in nodes.items()
        if not children.get(node_id) and node.get("status") == "completed"
    ]
    if not leaves:
        leaves = [node_id for node_id, node in nodes.items() if node.get("status") == "completed"]
    if not leaves:
        return None, [], ["No completed terminal nodes were found."]

    leaves.sort(key=lambda nid: (
        _LEAF_PRIORITY.get(_node_type(nodes[nid]), 99),
        str(nodes[nid].get("updated_at") or nodes[nid].get("created_at") or ""),
        nid,
    ))
    return leaves[0], leaves, []


def _lineage_for_terminal(
    nodes: dict[str, dict],
    terminal_node_id: str,
) -> tuple[list[tuple[str, dict]], list[str]]:
    """Walk parent links from a terminal node and return oldest-to-newest lineage."""
    errors: list[str] = []
    ordered: list[tuple[str, dict]] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(current_id: str) -> None:
        if current_id in visited:
            return
        if current_id in visiting:
            errors.append(f"Cycle detected while tracing node lineage at {current_id}")
            return
        node = nodes.get(current_id)
        if node is None:
            errors.append(f"Referenced parent node not found: {current_id}")
            return
        visiting.add(current_id)
        parents = [str(p) for p in (node.get("parent_node_ids") or [])]
        continued_from = _node_metadata(node).get("continued_from")
        if isinstance(continued_from, str) and continued_from and continued_from not in parents:
            parents.insert(0, continued_from)
        for parent_id in parents:
            visit(parent_id)
        visiting.remove(current_id)
        visited.add(current_id)
        ordered.append((current_id, node))

    visit(terminal_node_id)
    return ordered, errors


def _events_for_lineage(job_dir: Path, lineage_ids: list[str]) -> list[dict]:
    events_dir = job_dir / "events"
    if not events_dir.is_dir():
        return []
    wanted = set(lineage_ids)
    records: list[dict] = []
    for event_file in sorted(events_dir.glob("*.json")):
        data = _read_json(event_file)
        if not isinstance(data, dict):
            continue
        node_id = str(data.get("node_id") or "")
        if node_id and node_id not in wanted:
            continue
        if not node_id and not any(f"_{lineage_id}_" in event_file.name for lineage_id in wanted):
            continue
        records.append({
            "file": str(event_file),
            "node_id": node_id or None,
            "event_type": data.get("event_type"),
            "created_at": data.get("created_at") or data.get("timestamp"),
        })
    return records


def _node_type_counts(nodes: dict[str, dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in nodes.values():
        node_type = str(node.get("node_type") or "unknown")
        counts[node_type] = counts.get(node_type, 0) + 1
    return counts


def _status_counts(nodes: dict[str, dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in nodes.values():
        status = str(node.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _completed_nodes(nodes: dict[str, dict], node_type: str) -> list[tuple[str, dict]]:
    return [
        (node_id, data)
        for node_id, data in sorted(nodes.items())
        if data.get("node_type") == node_type and data.get("status") == "completed"
    ]


def _artifact_records(job_dir: Path, nodes: dict[str, dict]) -> list[dict]:
    records: list[dict] = []
    for node_id, data in sorted(nodes.items()):
        artifacts = data.get("artifacts", {})
        if not isinstance(artifacts, dict):
            continue
        for key, value in artifacts.items():
            records.append({
                "job_dir": str(job_dir),
                "node_id": node_id,
                "artifact_key": key,
                "value": value,
            })
    return records


def _analyze_metrics(nodes: dict[str, dict]) -> dict:
    metrics: dict[str, Any] = {}
    analyses: list[dict] = []
    for node_id, node in _completed_nodes(nodes, "analyze"):
        metadata = node.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        picked = {
            key: metadata[key]
            for key in sorted(_ANALYZE_METRIC_KEYS)
            if key in metadata
        }
        if picked:
            analyses.append({
                "node_id": node_id,
                "label": node.get("label"),
                "metrics": picked,
            })
    if analyses:
        metrics["analyze"] = analyses
    return metrics


def _first_node(lineage: list[tuple[str, dict]], *node_types: str) -> tuple[str, dict] | None:
    wanted = set(node_types)
    for node_id, node in lineage:
        if _node_type(node) in wanted:
            return node_id, node
    return None


def _nodes_of_type(lineage: list[tuple[str, dict]], *node_types: str) -> list[tuple[str, dict]]:
    wanted = set(node_types)
    return [(node_id, node) for node_id, node in lineage if _node_type(node) in wanted]


def _value_from_node(node: dict | None, *keys: str) -> Any:
    if not node:
        return None
    for container in (_node_conditions(node), _node_metadata(node), _node_artifacts(node)):
        for key in keys:
            if key in container and container[key] not in (None, "", []):
                return container[key]
    return None


def _format_value(value: Any, unit: str = "") -> str:
    if value is None:
        return "not recorded"
    if isinstance(value, float):
        value_str = f"{value:g}"
    elif isinstance(value, list):
        value_str = ", ".join(str(v) for v in value)
    else:
        value_str = str(value)
    return f"{value_str}{unit}"


def _join_sentence_parts(parts: list[str]) -> str:
    return "; ".join(part for part in parts if part)


def _source_description(source_node: dict | None) -> str:
    if not source_node:
        return "the recorded input structure"
    metadata = _node_metadata(source_node)
    source_type = str(metadata.get("source_type") or _node_type(source_node)).lower()
    source_id = metadata.get("source_id")
    if source_type == "pdb" and source_id:
        return f"RCSB PDB entry {source_id}"
    if source_type == "alphafold" and source_id:
        return f"AlphaFold DB model {source_id}"
    if source_type == "local":
        return "a local input structure"
    if source_type == "boltz2":
        return "a Boltz-2 predicted structure"
    if source_type == "modeller":
        return "a MODELLER comparative model"
    if source_id:
        return f"{source_type} structure {source_id}"
    artifact = _value_from_node(source_node, "structure_file", "file_path")
    if artifact:
        return f"the recorded input structure ({artifact})"
    return "the recorded input structure"


def _preparation_description(prep_node: dict | None) -> tuple[str, str, str]:
    if not prep_node:
        return "not recorded", "not recorded", "not recorded"
    metadata = _node_metadata(prep_node)
    method = str(metadata.get("protonation_method") or "not recorded")
    ph = _format_value(metadata.get("protonation_ph"))
    tools: list[str] = []
    lowered = method.lower()
    if "pdb2pqr" in lowered:
        tools.append("PDB2PQR")
    if "propka" in lowered:
        tools.append("PROPKA")
    if not tools and method != "not recorded":
        tools.append(method)
    artifacts = _node_artifacts(prep_node)
    special: list[str] = []
    for label, keys in (
        ("ligands", ("ligand_params", "ligands")),
        ("glycans", ("glycan_metadata", "glycan_linkages")),
        ("nucleic acids", ("residue_mapping", "modxna_params")),
        ("phosphorylated residues", ("phosphorylated_pdb",)),
        ("mutations", ("mutated_pdb",)),
    ):
        if any(key in artifacts or key in metadata for key in keys):
            special.append(label)
    special_components = ", ".join(special) if special else "no special components were recorded"
    return ", ".join(tools) if tools else "not recorded", ph, special_components


def _solvation_description(solv_node: dict | None) -> tuple[str, str, str]:
    if not solv_node:
        return "not recorded", "not recorded", "not recorded"
    metadata = _node_metadata(solv_node)
    water_model = str(metadata.get("water_model") or "not recorded")
    shape = metadata.get("box_shape")
    buffer_distance = metadata.get("buffer_distance_angstrom")
    if shape and buffer_distance is not None:
        box_description = f"a {shape} box and a {buffer_distance:g} Å buffer"
    elif shape:
        box_description = f"a {shape} box"
    else:
        box_description = "the recorded solvent box"
    salt = metadata.get("salt_concentration_M")
    if isinstance(salt, (int, float)):
        salt_description = f"{salt:g} M salt concentration"
    else:
        salt_description = "the recorded ionic conditions"
    return water_model, box_description, salt_description


def _topology_description(topo_node: dict | None) -> tuple[str, str, str, str]:
    if not topo_node:
        return "not recorded", "not recorded", "", "not recorded"
    metadata = _node_metadata(topo_node)
    artifacts = _node_artifacts(topo_node)
    forcefield = str(metadata.get("forcefield") or "not recorded")
    water_model = str(metadata.get("water_model") or "not recorded")
    extra: list[str] = []
    if metadata.get("nucleic_libraries"):
        libraries = metadata["nucleic_libraries"]
        if isinstance(libraries, list):
            libraries = ", ".join(str(item) for item in libraries)
        extra.append(f"{libraries} for nucleic acids")
    elif metadata.get("nucleic_forcefield"):
        extra.append(f"{metadata['nucleic_forcefield']} for nucleic acids")
    if metadata.get("glycan_forcefield"):
        extra.append(f"{metadata['glycan_forcefield']} for glycans")
    if metadata.get("phosaa_library"):
        extra.append(f"{metadata['phosaa_library']} for phosphorylated residues")
    if artifacts.get("modxna_params") or metadata.get("modxna_params"):
        extra.append("modXNA parameters for modified nucleic acids")
    additional = ""
    if extra:
        additional = ", with " + ", ".join(extra)
    ligand = "not used"
    if artifacts.get("ligand_params") or metadata.get("ligand_params"):
        ligand = "AmberTools/antechamber using GAFF-family parameters"
    return forcefield, water_model, additional, ligand


def _equilibration_description(eq_node: dict | None) -> str:
    if not eq_node:
        return "not recorded"
    metadata = _node_metadata(eq_node)
    conditions = _node_conditions(eq_node)
    parts = ["energy minimization"]
    if metadata.get("nvt_steps") is not None:
        parts.append(f"NVT equilibration for {metadata['nvt_steps']} steps")
    if metadata.get("npt_steps") is not None:
        parts.append(f"NPT equilibration for {metadata['npt_steps']} steps")
    temp = metadata.get("temperature_kelvin", conditions.get("temperature_kelvin"))
    pressure = metadata.get("pressure_bar", conditions.get("pressure_bar"))
    if temp is not None:
        parts.append(f"{temp:g} K")
    if pressure is not None:
        parts.append(f"{pressure:g} bar")
    restraints = metadata.get("restraint_atoms")
    if restraints:
        parts.append(f"restraints on {restraints}")
    return _join_sentence_parts(parts)


def _production_description(prod_node: dict | None) -> dict[str, str]:
    if not prod_node:
        return {
            "simulation_time": "not recorded",
            "temperature": "not recorded",
            "timestep": "not recorded",
            "constraints_or_hmr": "not recorded",
            "output_frequency": "not recorded",
            "platform": "not recorded",
        }
    metadata = _node_metadata(prod_node)
    conditions = _node_conditions(prod_node)
    hmr = metadata.get("hmr")
    if hmr is True:
        constraints_or_hmr = "hydrogen mass repartitioning"
    elif hmr is False:
        constraints_or_hmr = "standard hydrogen masses"
    else:
        constraints_or_hmr = "mass repartitioning not recorded"
    return {
        "simulation_time": _format_value(
            metadata.get("simulation_time_ns", conditions.get("simulation_time_ns")), " ns"
        ),
        "temperature": _format_value(metadata.get("temperature_kelvin")),
        "timestep": _format_value(metadata.get("timestep_fs"), " fs"),
        "constraints_or_hmr": constraints_or_hmr,
        "output_frequency": _format_value(
            metadata.get("output_frequency_ps", conditions.get("output_frequency_ps")), " ps"
        ),
        "platform": _format_value(metadata.get("platform")),
    }


def _lineage_summary(lineage: list[tuple[str, dict]]) -> list[dict]:
    summary = []
    for node_id, node in lineage:
        metadata = _node_metadata(node)
        conditions = _node_conditions(node)
        summary.append({
            "node_id": node_id,
            "node_type": _node_type(node),
            "label": _node_label(node),
            "status": node.get("status"),
            "parents": node.get("parent_node_ids", []),
            "conditions": conditions,
            "metadata_keys": sorted(metadata.keys()),
            "artifact_keys": sorted(_node_artifacts(node).keys()),
        })
    return summary


def _extract_method_facts(
    job_dir: Path,
    progress: dict,
    lineage: list[tuple[str, dict]],
    events: list[dict],
) -> dict:
    source_pair = _first_node(lineage, "source", "fetch")
    prep_pair = _first_node(lineage, "prep")
    solv_pair = _first_node(lineage, "solv")
    topo_pair = _first_node(lineage, "topo")
    eq_pair = _first_node(lineage, "eq")
    prod_nodes = _nodes_of_type(lineage, "prod")
    analyze_nodes = _nodes_of_type(lineage, "analyze")
    prod_pair = prod_nodes[-1] if prod_nodes else None

    source_node = source_pair[1] if source_pair else None
    prep_node = prep_pair[1] if prep_pair else None
    solv_node = solv_pair[1] if solv_pair else None
    topo_node = topo_pair[1] if topo_pair else None
    eq_node = eq_pair[1] if eq_pair else None
    prod_node = prod_pair[1] if prod_pair else None

    prep_tools, ph, special_components = _preparation_description(prep_node)
    solv_water, box_description, salt_description = _solvation_description(solv_node)
    (
        forcefield,
        topo_water,
        additional_forcefields,
        ligand_parameterization,
    ) = _topology_description(topo_node)
    production = _production_description(prod_node)
    chains = _node_metadata(source_node).get("chains") if source_node else None
    if not chains and isinstance(progress.get("system"), dict):
        chains = progress["system"].get("chains")

    lineage_ids = [node_id for node_id, _node in lineage]
    return {
        "job_id": progress.get("job_id") or job_dir.name,
        "job_dir": str(job_dir),
        "terminal_node_id": lineage_ids[-1] if lineage_ids else None,
        "lineage": lineage_ids,
        "lineage_summary": _lineage_summary(lineage),
        "event_count": len(events),
        "source_description": _source_description(source_node),
        "chains": chains or "not recorded",
        "preparation_tools": prep_tools,
        "ph": ph,
        "special_components": special_components,
        "water_model": topo_water if topo_water != "not recorded" else solv_water,
        "box_description": box_description,
        "salt_description": salt_description,
        "forcefield": forcefield,
        "additional_forcefields_sentence": additional_forcefields,
        "ligand_parameterization": ligand_parameterization,
        "equilibration_protocol": _equilibration_description(eq_node),
        **production,
        "production_segments": len(prod_nodes),
        "analysis_nodes": [node_id for node_id, _node in analyze_nodes],
        "has_analysis": bool(analyze_nodes),
    }


def _draft_methods_paragraphs(facts: dict) -> list[str]:
    source_para = (
        f"The initial structure was obtained from {facts['source_description']}. "
        f"The selected chain(s), {_format_value(facts['chains'])}, were prepared with MDClaw. "
        f"Missing atoms and standard residue preparation were handled using "
        f"{facts['preparation_tools']}, and protonation states were assigned at pH {facts['ph']}. "
        f"Special components were recorded as follows: {facts['special_components']}."
    )

    topology_para = (
        f"The prepared structure was solvated using an explicit {facts['water_model']} water model "
        f"with {facts['box_description']}. Ions were added according to "
        f"{facts['salt_description']}. "
        f"Amber topology and coordinate files were generated with AmberTools/tleap using the "
        f"{facts['forcefield']} protein force field{facts['additional_forcefields_sentence']}. "
        f"Small-molecule ligand parameterization was {facts['ligand_parameterization']}."
    )

    if facts["output_frequency"] == "not recorded":
        output_sentence = (
            "and trajectory/energy outputs were recorded using the job's recorded reporter "
            "configuration"
        )
    else:
        output_sentence = (
            f"and coordinates/energies were saved every {facts['output_frequency']}"
        )

    production_para = (
        f"Molecular dynamics simulations were performed with OpenMM. The system was equilibrated "
        f"using "
        f"{facts['equilibration_protocol']}. Production simulations were run for "
        f"{facts['simulation_time']} at {facts['temperature']} K using a "
        f"{facts['timestep']} timestep, "
        f"{facts['constraints_or_hmr']}, {output_sentence}. "
        f"The workflow lineage used for this Methods draft was "
        f"{' -> '.join(facts['lineage'])}."
    )

    return [source_para, topology_para, production_para]


def _methods_template() -> str:
    return "\n".join([
        "Template paragraph 1: structure preparation",
        (
            "The initial structure was obtained from {source_description}. The selected chain(s), "
            "{chains}, were prepared with MDClaw. Missing atoms and standard residue preparation "
            "were handled using {preparation_tools}, and protonation states were assigned at pH "
            "{ph}. If present, ligands, glycans, nucleic acids, metal ions, or post-translational "
            "modifications were treated as follows: {special_components}."
        ),
        "",
        "Template paragraph 2: system construction",
        (
            "The prepared structure was solvated using an explicit {water_model} water model with "
            "{box_description}. Ions were added to {salt_description}. Amber topology and "
            "coordinate "
            "files were generated with AmberTools/tleap using the {forcefield} protein force field"
            "{additional_forcefields_sentence}. Small-molecule ligands, if present, were "
            "parameterized "
            "using {ligand_parameterization}."
        ),
        "",
        "Template paragraph 3: MD protocol",
        (
            "Molecular dynamics simulations were performed with OpenMM. The system was "
            "energy-minimized "
            "and equilibrated using {equilibration_protocol}. Production simulations were run for "
            "{simulation_time} at {temperature} K using a {timestep} timestep, "
            "{constraints_or_hmr}, "
            "and coordinates/energies were saved every {output_frequency}. The exact workflow "
            "lineage "
            "was {lineage}."
        ),
        "",
        "Citation insertion hint",
        (
            "Use only citations corresponding to filled placeholders. For example, include OpenMM "
            "and "
            "AmberTools for all OpenMM/Amber runs; include ff19SB and OPC only when "
            "`{forcefield}=ff19SB` and `{water_model}=OPC`; include MODELLER, Boltz-2, "
            "AlphaFold DB, "
            "GLYCAM, OL15/OL3, GAFF, modXNA, or PLIP only when those tools or models appear "
            "in the lineage."
        ),
    ])


def _mermaid_workflow(lineage: list[tuple[str, dict]]) -> str:
    lines = ["flowchart LR"]
    previous = None
    for index, (node_id, node) in enumerate(lineage):
        mermaid_id = f"N{index}"
        label = f"{node_id}: {_node_type(node)}"
        lines.append(f'    {mermaid_id}["{label}"]')
        if previous is not None:
            lines.append(f"    {previous} --> {mermaid_id}")
        previous = mermaid_id
    if len(lines) == 1:
        lines.append('    Empty["No lineage"]')
    return "\n".join(lines)


def _collect_citation_tokens(lineage: list[tuple[str, dict]], facts: dict) -> list[str]:
    tokens: list[str] = []

    def add(token: str) -> None:
        if token not in tokens:
            tokens.append(token)

    node_types = {_node_type(node) for _node_id, node in lineage}
    if "eq" in node_types or "prod" in node_types:
        add("openmm")
    if "topo" in node_types:
        add("ambertools")
    if "analyze" in node_types:
        add("mdtraj")

    source_node = _first_node(lineage, "source", "fetch")
    if source_node:
        source_type = str(_node_metadata(source_node[1]).get("source_type") or "").lower()
        if source_type == "pdb":
            add("rcsb_pdb")
        elif source_type == "alphafold":
            add("alphafold_db")
            add("alphafold")
        elif source_type == "boltz2":
            add("boltz2")
        elif source_type == "modeller":
            add("modeller")
        elif source_type == "uniprot":
            add("uniprot")

    for _node_id, node in lineage:
        haystack = json.dumps({
            "type": _node_type(node),
            "metadata": _node_metadata(node),
            "conditions": _node_conditions(node),
            "artifacts": _node_artifacts(node),
        }, default=str).lower()
        if "boltz" in haystack:
            add("boltz2")
        if "modeller" in haystack:
            add("modeller")
        if "pdb2pqr" in haystack:
            add("pdb2pqr")
        if "propka" in haystack:
            add("propka")
        if "rdkit" in haystack:
            add("rdkit")
        if "gemmi" in haystack:
            add("gemmi")
        if "dimorphite" in haystack:
            add("dimorphite")
        if "faspr" in haystack:
            add("faspr")
        if "modxna" in haystack:
            add("modxna")
        if "plip" in haystack:
            add("plip")
        if "packmol-memgen" in haystack or "memgen" in haystack:
            add("packmol_memgen")
        if "packmol" in haystack:
            add("packmol")
        if "memembed" in haystack:
            add("memembed")
        if "glycam" in haystack or "glycan" in haystack:
            add("glycam")
        if "ol15" in haystack:
            add("ol15")
        if "ol3" in haystack:
            add("ol3")
        if "phosaa" in haystack or "sep" in haystack or "tpo" in haystack or "ptr" in haystack:
            add("phosaa")
        if "gaff" in haystack:
            add("gaff")
        if "am1-bcc" in haystack or "am1bcc" in haystack:
            add("am1bcc")

    forcefield = str(facts.get("forcefield") or "").lower()
    if "ff19sb" in forcefield:
        add("ff19sb")
    if "ff14sb" in forcefield:
        add("ff14sb")

    water_model = str(facts.get("water_model") or "").lower().replace("-", "")
    if water_model in {"opc", "opc3", "tip3p", "spce", "tip4pew"}:
        add(water_model)

    return tokens


def _selected_bibtex_entries(
    lineage: list[tuple[str, dict]],
    facts: dict,
    citation_inventory: str | None = None,
) -> tuple[list[str], list[str], list[str]]:
    entries_by_key = _read_citation_inventory(citation_inventory)
    selected_keys: list[str] = []
    missing: list[str] = []
    for token in _collect_citation_tokens(lineage, facts):
        key = _CITATION_KEY_BY_TOKEN.get(token)
        if not key:
            continue
        if key not in selected_keys:
            selected_keys.append(key)
        if key not in entries_by_key and key not in missing:
            missing.append(key)
    entries = [entries_by_key[key] for key in selected_keys if key in entries_by_key]
    return selected_keys, entries, missing


def _markdown_table(rows: list[dict]) -> str:
    if not rows:
        return "_No lineage nodes found._"
    lines = [
        "| Node | Type | Label | Parents | Conditions | Artifacts |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        conditions = ", ".join(f"{k}={v}" for k, v in row["conditions"].items()) or "-"
        artifacts = ", ".join(row["artifact_keys"]) or "-"
        parents = ", ".join(row["parents"]) or "-"
        lines.append(
            f"| `{row['node_id']}` | {row['node_type']} | {row['label'] or '-'} | "
            f"{parents} | {conditions} | {artifacts} |"
        )
    return "\n".join(lines)


def _render_methods_markdown(
    *,
    job_dir: Path,
    terminal_node_id: str,
    facts: dict,
    methods_paragraphs: list[str],
    mermaid: str,
    citation_keys: list[str],
    bibtex_entries: list[str],
    missing_citations: list[str],
    candidate_terminal_nodes: list[str],
) -> str:
    missing_text = ""
    if missing_citations:
        missing_text = (
            "\n\n> Citation inventory entries not found for keys: "
            + ", ".join(f"`{key}`" for key in missing_citations)
        )
    return "\n\n".join([
        f"# MDClaw Methods Draft: {facts['job_id']} / {terminal_node_id}",
        "## Methods Draft",
        "\n\n".join(methods_paragraphs),
        "## LLM-Friendly Template",
        "```text\n" + _methods_template() + "\n```",
        "## Workflow Schematic",
        "```mermaid\n" + mermaid + "\n```",
        "## Lineage Summary",
        _markdown_table(facts["lineage_summary"]),
        "## Citation Keys",
        ", ".join(f"`{key}`" for key in citation_keys) or "_No citations selected._",
        "## BibTeX",
        "```bibtex\n" + "\n\n".join(bibtex_entries) + "\n```" + missing_text,
        "## Provenance",
        "\n".join([
            f"- Job directory: `{job_dir}`",
            f"- Terminal node: `{terminal_node_id}`",
            "- Candidate terminal nodes: "
            + (", ".join(f"`{n}`" for n in candidate_terminal_nodes) or "`none`"),
            f"- Lineage: {' -> '.join(f'`{n}`' for n in facts['lineage'])}",
            f"- Lineage event count: {facts['event_count']}",
        ]),
        "",
    ])


def _build_job_methods_material(
    job_dir: str | Path,
    node_id: str | None = None,
    citation_inventory: str | None = None,
) -> dict:
    """Collect lineage facts and draft material for one MDClaw job."""
    material: dict[str, Any] = {
        "success": False,
        "job_dir": None,
        "terminal_node_id": None,
        "candidate_terminal_nodes": [],
        "lineage": [],
        "facts": None,
        "methods_paragraphs": [],
        "mermaid": "",
        "citation_keys": [],
        "bibtex_entries": [],
        "missing_citations": [],
        "errors": [],
        "warnings": [],
    }

    jd, progress, nodes = _read_job(job_dir)
    material["job_dir"] = str(jd)
    if not (jd / "progress.json").exists():
        material["errors"].append(f"progress.json not found under {jd}")
        return material
    if not nodes:
        material["errors"].append(f"No nodes found under {jd / 'nodes'}")
        return material

    terminal_node_id, candidates, selection_errors = _choose_terminal_node(nodes, node_id)
    material["candidate_terminal_nodes"] = candidates
    if selection_errors:
        material["errors"].extend(selection_errors)
    if terminal_node_id is None:
        return material

    lineage, lineage_errors = _lineage_for_terminal(nodes, terminal_node_id)
    if lineage_errors:
        material["errors"].extend(lineage_errors)
        return material

    events = _events_for_lineage(jd, [lineage_node_id for lineage_node_id, _node in lineage])
    facts = _extract_method_facts(jd, progress, lineage, events)
    methods_paragraphs = _draft_methods_paragraphs(facts)
    mermaid = _mermaid_workflow(lineage)
    citation_keys, bibtex_entries, missing_citations = _selected_bibtex_entries(
        lineage, facts, citation_inventory
    )
    if missing_citations:
        material["warnings"].append(
            "Missing citation inventory entries: " + ", ".join(missing_citations)
        )

    material.update({
        "success": True,
        "terminal_node_id": terminal_node_id,
        "lineage": facts["lineage"],
        "facts": facts,
        "methods_paragraphs": methods_paragraphs,
        "mermaid": mermaid,
        "citation_keys": citation_keys,
        "bibtex_entries": bibtex_entries,
        "missing_citations": missing_citations,
    })
    return material


def generate_md_evidence_report(
    job_dir: str,
    evidence_type: str = "md_job_summary",
    question: Optional[str] = None,
    summary: Optional[str] = None,
    target: Optional[dict] = None,
    output_dir: Optional[str] = None,
    output_name: str = "md_evidence_report.json",
) -> dict:
    """Generate a minimal evidence report from one MDClaw ``job_dir``.

    This report summarizes completed nodes, available analysis metrics, and
    provenance. It does not interpret raw trajectories or call an LLM.
    """
    result: dict[str, Any] = {
        "success": False,
        "report": None,
        "report_file": None,
        "errors": [],
        "warnings": [],
    }
    try:
        jd, progress, nodes = _read_job(job_dir)
        if not (jd / "progress.json").exists():
            result["errors"].append(f"progress.json not found under {jd}")
            return result

        completed_prod = _completed_nodes(nodes, "prod")
        completed_analyze = _completed_nodes(nodes, "analyze")
        limitations: list[str] = []
        status = "complete" if completed_prod else "incomplete"
        if not completed_prod:
            limitations.append("No completed production nodes were found.")
        if not completed_analyze:
            limitations.append("No completed analyze nodes were found.")

        metrics = {
            "num_nodes": len(nodes),
            "node_type_counts": _node_type_counts(nodes),
            "node_status_counts": _status_counts(nodes),
            "completed_prod_nodes": [node_id for node_id, _ in completed_prod],
            "completed_analyze_nodes": [node_id for node_id, _ in completed_analyze],
        }
        metrics.update(_analyze_metrics(nodes))

        report_summary = summary
        if report_summary is None:
            report_summary = (
                f"MDClaw job {jd.name} contains {len(nodes)} nodes, "
                f"{len(completed_prod)} completed production node(s), and "
                f"{len(completed_analyze)} completed analysis node(s)."
            )

        report = base_evidence_report(
            evidence_type=evidence_type,
            status=status,
            question=question,
            target=target,
            summary=report_summary,
            metrics=metrics,
            limitations=limitations,
            artifacts=_artifact_records(jd, nodes),
            provenance={
                "generated_at": _now_iso(),
                "mdclaw_job_dir": str(jd),
                "progress_file": str(jd / "progress.json"),
                "nodes": sorted(nodes.keys()),
                "progress_job_id": progress.get("job_id"),
            },
        )

        out_dir = Path(output_dir).expanduser().resolve() if output_dir else jd / "evidence"
        ensure_directory(out_dir)
        report_file = out_dir / output_name
        _atomic_write_json(report_file, report)
        result.update({
            "success": True,
            "report": report,
            "report_file": str(report_file),
            "warnings": result["warnings"],
        })
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error(f"generate_md_evidence_report failed: {exc}")
        result["errors"].append(
            f"generate_md_evidence_report failed: {type(exc).__name__}: {exc}"
        )
        return result


def generate_md_methods_report(
    job_dir: str,
    node_id: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_name: Optional[str] = None,
    citation_inventory: Optional[str] = None,
) -> dict:
    """Generate a manuscript-oriented Methods draft from an MDClaw node lineage.

    The report follows the parent chain from ``node_id``. If ``node_id`` is not
    provided, the function selects a completed leaf node, preferring analysis and
    production nodes. The output is Markdown containing a 2-3 paragraph Methods
    draft, a reusable LLM template, a Mermaid workflow schematic, lineage facts,
    and DOI-containing BibTeX entries selected from the citation inventory.
    """
    result: dict[str, Any] = {
        "success": False,
        "methods_file": None,
        "terminal_node_id": None,
        "candidate_terminal_nodes": [],
        "lineage": [],
        "facts": None,
        "methods_paragraphs": [],
        "citation_keys": [],
        "bibtex_entries": [],
        "errors": [],
        "warnings": [],
    }
    try:
        material = _build_job_methods_material(job_dir, node_id, citation_inventory)
        result["candidate_terminal_nodes"] = material["candidate_terminal_nodes"]
        result["warnings"].extend(material["warnings"])
        if not material["success"]:
            result["errors"].extend(material["errors"])
            return result

        jd = Path(material["job_dir"])
        terminal_node_id = str(material["terminal_node_id"])
        facts = material["facts"]
        safe_job_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(facts["job_id"]))
        safe_node_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", terminal_node_id)
        default_name = f"mdclaw_methods_{safe_job_id}_{safe_node_id}.md"
        out_dir = Path(output_dir).expanduser().resolve() if output_dir else jd / "evidence"
        methods_file = out_dir / (output_name or default_name)
        markdown = _render_methods_markdown(
            job_dir=jd,
            terminal_node_id=terminal_node_id,
            facts=facts,
            methods_paragraphs=material["methods_paragraphs"],
            mermaid=material["mermaid"],
            citation_keys=material["citation_keys"],
            bibtex_entries=material["bibtex_entries"],
            missing_citations=material["missing_citations"],
            candidate_terminal_nodes=material["candidate_terminal_nodes"],
        )
        _atomic_write_text(methods_file, markdown)

        result.update({
            "success": True,
            "methods_file": str(methods_file),
            "terminal_node_id": terminal_node_id,
            "lineage": facts["lineage"],
            "facts": facts,
            "methods_paragraphs": material["methods_paragraphs"],
            "citation_keys": material["citation_keys"],
            "bibtex_entries": material["bibtex_entries"],
        })
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error(f"generate_md_methods_report failed: {exc}")
        result["errors"].append(
            f"generate_md_methods_report failed: {type(exc).__name__}: {exc}"
        )
        return result


def _load_study(study_dir: Path) -> dict:
    study_file = study_dir / "study.json"
    data = _read_json(study_file)
    if data is None:
        raise FileNotFoundError(f"study.json not found or unreadable at {study_file}")
    return data


def _resolve_study_job_dir(study_dir: Path, job_dir: str) -> Path:
    path = Path(job_dir).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (study_dir / path).resolve()


def _safe_filename_part(value: Any) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))
    return safe.strip("_") or "report"


def _study_job_display(job: dict) -> str:
    job_id = str(job.get("job_id") or "job")
    pieces = [f"{job_id}"]
    role = job.get("role")
    label = job.get("label")
    if role:
        pieces.append(f"role={role}")
    if label:
        pieces.append(f"label={label}")
    metadata = job.get("metadata", {})
    if isinstance(metadata, dict):
        for key in ("mutation", "variant", "condition"):
            if metadata.get(key):
                pieces.append(f"{key}={metadata[key]}")
    return " (".join([pieces[0], ", ".join(pieces[1:]) + ")"]) if len(pieces) > 1 else pieces[0]


def _terminal_node_for_study_job(
    job: dict,
    terminal_node_ids: dict | None,
) -> str | None:
    if not isinstance(terminal_node_ids, dict):
        return None
    for key in (job.get("job_id"), job.get("label"), job.get("role")):
        if key is not None and str(key) in terminal_node_ids:
            value = terminal_node_ids[str(key)]
            return str(value) if value else None
    return None


def _field_by_job(job_reports: list[dict], field: str) -> str:
    values = []
    for report in job_reports:
        facts = report["facts"]
        values.append((str(report["job_id"]), _format_value(facts.get(field))))
    unique_values = {value for _job_id, value in values}
    if len(unique_values) == 1:
        return values[0][1]
    return "; ".join(f"{job_id}: {value}" for job_id, value in values)


def _study_methods_paragraphs(study: dict, job_reports: list[dict]) -> list[str]:
    title = study.get("title") or "MDClaw study"
    objective = study.get("objective")
    objective_sentence = f" to address {objective}" if objective else ""
    job_descriptions = "; ".join(_study_job_display(report["study_job"]) for report in job_reports)
    source_summary = _field_by_job(job_reports, "source_description")
    design_para = (
        f"The MDClaw study {title} organized {len(job_reports)} independent molecular dynamics "
        f"job(s){objective_sentence}. The registered systems were {job_descriptions}. "
        f"Input structures were recorded as {source_summary}. Each registered job was treated "
        f"as an independent physical system with its own source-rooted MDClaw DAG."
    )

    prep_tools = _field_by_job(job_reports, "preparation_tools")
    ph = _field_by_job(job_reports, "ph")
    water_model = _field_by_job(job_reports, "water_model")
    box_description = _field_by_job(job_reports, "box_description")
    salt_description = _field_by_job(job_reports, "salt_description")
    forcefield = _field_by_job(job_reports, "forcefield")
    special_components = _field_by_job(job_reports, "special_components")
    protocol_para = (
        f"Structures were prepared with {prep_tools}, with protonation states assigned at pH "
        f"{ph}. Special components were recorded as {special_components}. Prepared systems "
        f"were solvated using {water_model} water with {box_description}, and ions were added "
        f"according to {salt_description}. Amber topology and coordinate files were generated "
        f"with AmberTools/tleap using {forcefield}."
    )

    equilibration = _field_by_job(job_reports, "equilibration_protocol")
    simulation_time = _field_by_job(job_reports, "simulation_time")
    temperature = _field_by_job(job_reports, "temperature")
    timestep = _field_by_job(job_reports, "timestep")
    output_frequency = _field_by_job(job_reports, "output_frequency")
    hmr = _field_by_job(job_reports, "constraints_or_hmr")
    lineage_summary = "; ".join(
        f"{report['job_id']}: {' -> '.join(report['lineage'])}" for report in job_reports
    )
    md_para = (
        f"Molecular dynamics simulations were performed with OpenMM. Equilibration protocols "
        f"were recorded as {equilibration}. Production simulations were run for "
        f"{simulation_time} at {temperature} K using {timestep} timesteps and {hmr}. "
        f"Trajectory and energy output frequency was recorded as {output_frequency}. "
        f"The per-job workflow lineages were {lineage_summary}."
    )

    return [design_para, protocol_para, md_para]


def _study_mermaid(job_reports: list[dict]) -> str:
    lines = [
        "flowchart LR",
        '    StudyInput["study.json"] --> JobList["Registered jobs"]',
    ]
    for index, report in enumerate(job_reports):
        job_node = f"Job{index}"
        label = f"{report['job_id']}: {report['terminal_node_id']}"
        lines.append(f'    JobList --> {job_node}["{label}"]')
        previous = job_node
        for lineage_index, node_id in enumerate(report["lineage"]):
            lineage_node = f"Job{index}Node{lineage_index}"
            lines.append(f'    {lineage_node}["{node_id}"]')
            lines.append(f"    {previous} --> {lineage_node}")
            previous = lineage_node
        lines.append(f"    {previous} --> StudyMethods")
    lines.extend([
        '    StudyMethods["Study Methods Markdown"] --> StudyBibTeX["Union BibTeX citations"]',
    ])
    return "\n".join(lines)


def _union_citations(
    job_reports: list[dict],
    citation_inventory: str | None = None,
) -> tuple[list[str], list[str], list[str]]:
    entries_by_key = _read_citation_inventory(citation_inventory)
    citation_keys: list[str] = []
    missing: list[str] = []
    for report in job_reports:
        for key in report["citation_keys"]:
            if key not in citation_keys:
                citation_keys.append(key)
            if key not in entries_by_key and key not in missing:
                missing.append(key)
    entries = [entries_by_key[key] for key in citation_keys if key in entries_by_key]
    return citation_keys, entries, missing


def _study_job_table(job_reports: list[dict]) -> str:
    lines = [
        "| Job | Role | Label | Terminal Node | Lineage |",
        "| --- | --- | --- | --- | --- |",
    ]
    for report in job_reports:
        job = report["study_job"]
        lines.append(
            f"| `{report['job_id']}` | {job.get('role') or '-'} | {job.get('label') or '-'} | "
            f"`{report['terminal_node_id']}` | {' -> '.join(f'`{n}`' for n in report['lineage'])} |"
        )
    return "\n".join(lines)


def _render_study_methods_markdown(
    *,
    study_dir: Path,
    study: dict,
    job_reports: list[dict],
    methods_paragraphs: list[str],
    mermaid: str,
    citation_keys: list[str],
    bibtex_entries: list[str],
    missing_citations: list[str],
) -> str:
    missing_text = ""
    if missing_citations:
        missing_text = (
            "\n\n> Citation inventory entries not found for keys: "
            + ", ".join(f"`{key}`" for key in missing_citations)
        )
    jobs_text = ", ".join(f"`{report['job_id']}`" for report in job_reports)
    return "\n\n".join([
        f"# MDClaw Study Methods Draft: {study.get('title') or study_dir.name}",
        "## Study Methods Draft",
        "\n\n".join(methods_paragraphs),
        "## Study Workflow Schematic",
        "```mermaid\n" + mermaid + "\n```",
        "## Registered Job Lineages",
        _study_job_table(job_reports),
        "## Citation Keys",
        ", ".join(f"`{key}`" for key in citation_keys) or "_No citations selected._",
        "## BibTeX",
        "```bibtex\n" + "\n\n".join(bibtex_entries) + "\n```" + missing_text,
        "## Provenance",
        "\n".join([
            f"- Study directory: `{study_dir}`",
            f"- Study file: `{study_dir / 'study.json'}`",
            f"- Objective: {study.get('objective') or 'not recorded'}",
            f"- Jobs: {jobs_text}",
        ]),
        "",
    ])


def _collect_study_job_reports(
    study_dir: Path,
    jobs: list[dict],
    terminal_node_ids: dict | None,
    citation_inventory: str | None,
) -> tuple[list[dict], list[str], list[str]]:
    job_reports: list[dict] = []
    errors: list[str] = []
    warnings: list[str] = []
    for job in jobs:
        job_id = str(job.get("job_id") or "")
        if not job_id:
            errors.append("Study job entry is missing job_id")
            continue
        abs_job_dir = _resolve_study_job_dir(study_dir, str(job.get("job_dir", "")))
        terminal_node_id = _terminal_node_for_study_job(job, terminal_node_ids)
        material = _build_job_methods_material(abs_job_dir, terminal_node_id, citation_inventory)
        warnings.extend(f"{job_id}: {warning}" for warning in material["warnings"])
        if not material["success"]:
            errors.extend(f"{job_id}: {error}" for error in material["errors"])
            continue
        job_reports.append({
            "job_id": job_id,
            "study_job": job,
            "job_dir": str(abs_job_dir),
            "terminal_node_id": material["terminal_node_id"],
            "lineage": material["lineage"],
            "facts": material["facts"],
            "citation_keys": material["citation_keys"],
        })
    return job_reports, errors, warnings


def generate_study_methods_report(
    study_dir: str,
    output_name: Optional[str] = None,
    citation_inventory: Optional[str] = None,
    terminal_node_ids: Optional[dict] = None,
) -> dict:
    """Generate a manuscript-oriented Methods draft for a multi-job MDClaw study."""
    result: dict[str, Any] = {
        "success": False,
        "methods_file": None,
        "study_dir": None,
        "job_reports": [],
        "methods_paragraphs": [],
        "citation_keys": [],
        "bibtex_entries": [],
        "errors": [],
        "warnings": [],
    }
    try:
        sd = Path(study_dir).expanduser().resolve()
        study = _load_study(sd)
        jobs = [job for job in study.get("jobs", []) if isinstance(job, dict)]
        if not jobs:
            result["errors"].append("Study has no registered jobs.")
            return result

        job_reports, errors, warnings = _collect_study_job_reports(
            sd, jobs, terminal_node_ids, citation_inventory
        )
        result["warnings"].extend(warnings)
        if errors:
            result["errors"].extend(errors)
            return result
        if not job_reports:
            result["errors"].append("No study jobs could be converted to Methods facts.")
            return result

        methods_paragraphs = _study_methods_paragraphs(study, job_reports)
        mermaid = _study_mermaid(job_reports)
        citation_keys, bibtex_entries, missing_citations = _union_citations(
            job_reports, citation_inventory
        )
        if missing_citations:
            result["warnings"].append(
                "Missing citation inventory entries: " + ", ".join(missing_citations)
            )

        safe_study_name = _safe_filename_part(study.get("title") or sd.name)
        methods_file = sd / "evidence" / (
            output_name or f"mdclaw_study_methods_{safe_study_name}.md"
        )
        markdown = _render_study_methods_markdown(
            study_dir=sd,
            study=study,
            job_reports=job_reports,
            methods_paragraphs=methods_paragraphs,
            mermaid=mermaid,
            citation_keys=citation_keys,
            bibtex_entries=bibtex_entries,
            missing_citations=missing_citations,
        )
        _atomic_write_text(methods_file, markdown)

        result.update({
            "success": True,
            "methods_file": str(methods_file),
            "study_dir": str(sd),
            "job_reports": job_reports,
            "methods_paragraphs": methods_paragraphs,
            "citation_keys": citation_keys,
            "bibtex_entries": bibtex_entries,
        })
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error(f"generate_study_methods_report failed: {exc}")
        result["errors"].append(
            f"generate_study_methods_report failed: {type(exc).__name__}: {exc}"
        )
        return result


def generate_study_evidence_report(
    study_dir: str,
    evidence_type: str = "md_study_summary",
    question: Optional[str] = None,
    summary: Optional[str] = None,
    output_name: str = "study_evidence_report.json",
) -> dict:
    """Generate a minimal evidence report across jobs registered in a study."""
    result: dict[str, Any] = {
        "success": False,
        "report": None,
        "report_file": None,
        "errors": [],
        "warnings": [],
    }
    try:
        sd = Path(study_dir).expanduser().resolve()
        study = _load_study(sd)
        jobs = [j for j in study.get("jobs", []) if isinstance(j, dict)]
        job_reports: list[dict] = []
        aggregate_status_counts: dict[str, int] = {}
        aggregate_type_counts: dict[str, int] = {}
        for job in jobs:
            job_dir_value = str(job.get("job_dir", ""))
            abs_job_dir = _resolve_study_job_dir(sd, job_dir_value)
            jd, _progress, nodes = _read_job(abs_job_dir)
            status_counts = _status_counts(nodes)
            type_counts = _node_type_counts(nodes)
            for key, value in status_counts.items():
                aggregate_status_counts[key] = aggregate_status_counts.get(key, 0) + value
            for key, value in type_counts.items():
                aggregate_type_counts[key] = aggregate_type_counts.get(key, 0) + value
            job_reports.append({
                "job_id": job.get("job_id"),
                "role": job.get("role"),
                "job_dir": str(jd),
                "node_count": len(nodes),
                "node_status_counts": status_counts,
                "node_type_counts": type_counts,
                "completed_prod_nodes": [
                    node_id for node_id, _ in _completed_nodes(nodes, "prod")
                ],
                "completed_analyze_nodes": [
                    node_id for node_id, _ in _completed_nodes(nodes, "analyze")
                ],
            })

        limitations: list[str] = []
        if not jobs:
            limitations.append("Study has no registered jobs.")
        if not any(j["completed_prod_nodes"] for j in job_reports):
            limitations.append("No completed production nodes were found across study jobs.")

        report_summary = summary or (
            f"MDClaw study {study.get('title') or sd.name} contains "
            f"{len(jobs)} registered job(s)."
        )
        report = base_evidence_report(
            evidence_type=evidence_type,
            status="complete" if jobs else "incomplete",
            question=question or study.get("objective"),
            summary=report_summary,
            metrics={
                "num_jobs": len(jobs),
                "jobs": job_reports,
                "aggregate_node_status_counts": aggregate_status_counts,
                "aggregate_node_type_counts": aggregate_type_counts,
            },
            limitations=limitations,
            provenance={
                "generated_at": _now_iso(),
                "study_dir": str(sd),
                "study_file": str(sd / "study.json"),
                "job_dirs": [j["job_dir"] for j in job_reports],
            },
            metadata={
                "study_title": study.get("title"),
                "study_objective": study.get("objective"),
            },
        )
        out_dir = sd / "evidence"
        ensure_directory(out_dir)
        report_file = out_dir / output_name
        _atomic_write_json(report_file, report)
        result.update({
            "success": True,
            "report": report,
            "report_file": str(report_file),
            "warnings": result["warnings"],
        })
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error(f"generate_study_evidence_report failed: {exc}")
        result["errors"].append(
            f"generate_study_evidence_report failed: {type(exc).__name__}: {exc}"
        )
        return result


TOOLS = {
    "generate_md_evidence_report": generate_md_evidence_report,
    "generate_md_methods_report": generate_md_methods_report,
    "generate_study_methods_report": generate_study_methods_report,
    "generate_study_evidence_report": generate_study_evidence_report,
}
