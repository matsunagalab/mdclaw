"""The applied receipt: what the caller's options did, and the facts of the stage.

Agents do not trust a stage tool's exit status; they verify. In the 64 sealed
``cli_skill_sif`` attempts of the 2026-09-10 MDDataBench campaign the commands
after a stage tool were, in most attempts, scripts reading the artifacts to
check ligands and charges (46 attempts), node status and files (43), water
model / force field / HMR (38), lipids (37), atom counts (35), ions (33),
disulfides (32), protonation (27), the box (26) and bonds across a chain gap
(18). The answers were in the result, scattered across blocks of up to 100 KB;
one attempt wrote fourteen such scripts and ran out of its budget.

``build_receipt`` puts the same answers directly after ``message``:

- ``options``: every option the caller passed, with the value the tool
  actually used (``applied`` / ``changed`` / ``not_reported``) and, in node
  mode, ``ignored_options`` for inputs the DAG resolves instead.
- ``facts``: the stage's key figures, taken from what the tool already
  computed.
- ``summary``: one line, which also becomes the result's ``message``.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

# Inputs the DAG resolves for a stage: passing them in node mode has no effect
# (the tool uses the ancestor's artifact, or refuses a different path).
DAG_RESOLVED_INPUTS = {
    "prep": ("structure_file",),
    "solv": ("pdb_file",),
    "topo": ("pdb_file",),
    "min": ("system_xml_file", "topology_pdb_file", "state_xml_file"),
    "eq": ("system_xml_file", "topology_pdb_file", "state_xml_file"),
    "prod": ("system_xml_file", "topology_pdb_file", "state_xml_file"),
    "fep": ("system_xml_file", "topology_pdb_file", "state_xml_file", "fep_protocol_file"),
}
NODE_MODE_SUPERSEDED = ("output_dir",)


def _get(data: Any, path: str, default: Any = None) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return default
        if current is None:
            return default
    return current


def _compact(mapping: dict) -> dict:
    return {key: value for key, value in mapping.items()
            if value is not None and value != [] and value != {}}


def _same(requested: Any, effective: Any) -> bool:
    if isinstance(requested, bool) or isinstance(effective, bool):
        return bool(requested) == bool(effective)
    if isinstance(requested, (int, float)) and isinstance(effective, (int, float)):
        return abs(float(requested) - float(effective)) <= 1e-9
    if isinstance(requested, str) and isinstance(effective, str):
        return requested.strip().lower() == effective.strip().lower()
    return requested == effective


def _n(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def option_lines(result: dict, explicit: dict, *, node_type: Optional[str],
                 node_mode: bool) -> tuple[list[dict], list[dict]]:
    """One line per option the caller passed: what the tool did with it."""
    parameters = result.get("parameters") if isinstance(result.get("parameters"), dict) else {}
    superseded: set[str] = set()
    if node_mode:
        superseded = set(NODE_MODE_SUPERSEDED) | set(DAG_RESOLVED_INPUTS.get(node_type or "", ()))
    lines: list[dict] = []
    ignored: list[dict] = []
    for name in sorted(explicit):
        if name in ("job_dir", "node_id"):
            continue
        requested = explicit[name]
        if name in superseded:
            ignored.append({
                "name": name, "requested": requested, "status": "superseded_by_dag",
                "effective": result.get(name) if name == "output_dir" else "resolved from the parent node",
            })
            continue
        if name in parameters:
            effective = parameters[name]
        elif name in result and not isinstance(result[name], (dict, list)):
            effective = result[name]
        else:
            lines.append({"name": name, "requested": requested, "status": "not_reported"})
            continue
        line = {"name": name, "requested": requested, "effective": effective,
                "status": "applied" if _same(requested, effective) else "changed"}
        source = parameters.get(f"{name}_source")
        if source:
            line["source"] = source
        lines.append(line)
    return lines, ignored


# ---------------------------------------------------------------------------
# Stage facts
# ---------------------------------------------------------------------------


def _facts_source(result: dict) -> dict:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    return _compact({
        "source": result.get("source") or metadata.get("source_type"),
        "identifier": (result.get("pdb_id") or result.get("uniprot_id")
                       or result.get("source_id") or metadata.get("source_id")),
        "format": result.get("format") or metadata.get("format"),
        "structures": result.get("structure_count") or result.get("candidate_count"),
        "atoms": result.get("num_atoms") or metadata.get("num_atoms"),
        "chains": result.get("chains") or metadata.get("chains"),
        "assembly_ids": result.get("assembly_ids"),
    })


def _summary_source(facts: dict, result: dict) -> str:
    parts = []
    if facts.get("source") or facts.get("identifier"):
        parts.append(" ".join(str(x) for x in (facts.get("source"), facts.get("identifier")) if x))
    if facts.get("structures"):
        parts.append(f"{facts['structures']} structure(s)")
    if facts.get("atoms"):
        parts.append(f"{_n(facts['atoms'])} atoms")
    if facts.get("chains"):
        chains = facts["chains"]
        parts.append("chains " + ("/".join(str(c) for c in chains) if isinstance(chains, list) else str(chains)))
    return "source: " + ", ".join(parts) if parts else "source recorded"


def _caps(protein: dict) -> Optional[str]:
    caps = protein.get("terminal_caps") if isinstance(protein.get("terminal_caps"), dict) else {}
    n_cap, c_cap = caps.get("n_terminal"), caps.get("c_terminal")
    if not n_cap and not c_cap:
        return "charged termini (no caps)"
    return f"N {n_cap or '-'} / C {c_cap or '-'}"


# Residue names that are one hydrogen away from the force field's fixed state.
# CYX (disulfide) and metal-site CYM are structural chemistry, not a
# protonation choice, and are filtered out below.
_NON_FIXED_PROTONATION_NAMES = {"ASH", "GLH", "LYN", "HIP", "CYM"}


def _protonation_fact(result: dict, proteins: list) -> Optional[dict]:
    """Method, pH and the residues off the fixed state, from the prep record.

    Four campaign attempts (2026-09-16) lost a check on a HIP, ASH or GLH that
    propka assigned while the request asked for fixed states; nothing in the
    receipt said so. The method label comes from the protein records
    (``pdb2pqr+propka`` / ``pdb2pqr_no_prediction``), the residues from the
    per-chain ``protonation_states`` (ASH/GLH/LYN/CYM) and ``histidine_states``
    (HIP) records; metal-site ligands declared in ``metal_sites`` are excluded.
    """
    labels = {p.get("protonation_method") for p in proteins if p.get("protonation_method")}
    requested = _get(result, "preparation_summary.protonation_method")
    if not labels and not requested:
        return None
    label = next(iter(labels)) if len(labels) == 1 else (requested or ", ".join(sorted(labels)))
    if label in ("standard", "no-prediction"):
        label = "pdb2pqr_no_prediction"
    method = ("no-prediction" if "no_prediction" in str(label) or "standard" in str(label)
              else "propka" if "propka" in str(label) else str(label))
    ph = None
    if method == "propka":
        for protein in proteins:
            if protein.get("requested_ph") is not None:
                ph = protein.get("requested_ph")
                break
    metal_ligands = set()
    for site in result.get("metal_sites") or []:
        for item in (site.get("ligands") or []) if isinstance(site, dict) else []:
            if isinstance(item, dict):
                metal_ligands.add((str(item.get("chain") or ""), str(item.get("resnum") or "")))
    moved = []
    seen = set()
    for protein in proteins:
        for record in protein.get("protonation_states") or []:
            if not isinstance(record, dict):
                continue
            state = str(record.get("state") or "")
            chain, resnum = str(record.get("chain") or ""), str(record.get("resnum") or "")
            if state not in _NON_FIXED_PROTONATION_NAMES or (chain, resnum) in seen:
                continue
            if state == "CYM" and (chain, resnum) in metal_ligands:
                continue
            seen.add((chain, resnum))
            moved.append(f"{state}{chain}{resnum}" if chain else f"{state}{resnum}")
        his = protein.get("histidine_states") if isinstance(protein.get("histidine_states"), dict) else {}
        for key, state in his.items():
            if state != "HIP":
                continue
            chain, _, resnum = str(key).partition(":")
            if (chain, resnum) in seen:
                continue
            seen.add((chain, resnum))
            moved.append(f"HIP{chain}{resnum}")
    return _compact({
        "method": method,
        "label": label,
        "ph": ph,
        "non_fixed_states": moved or None,
        "non_fixed_state_count": len(moved),
    })


def _facts_prep(result: dict) -> dict:
    proteins = [p for p in result.get("proteins") or [] if isinstance(p, dict)]
    chains = []
    missing = []
    for protein in proteins:
        statistics = protein.get("statistics") if isinstance(protein.get("statistics"), dict) else {}
        chains.append(_compact({
            "chain": protein.get("chain_id"),
            "residues": statistics.get("final_residues"),
            "atoms": statistics.get("final_atoms"),
            "ranges": protein.get("residue_ranges") or None,
            "termini": _caps(protein),
        }))
        detection = protein.get("missing_residue_detection")
        if isinstance(detection, dict) and detection.get("status") == "detected":
            reference = detection.get("reference_sequence_length")
            modeled = detection.get("modeled_residues")
            if reference and modeled and reference > modeled:
                missing.append({"chain": protein.get("chain_id"),
                                "unmodeled_residues": reference - modeled,
                                "reference_length": reference, "modeled": modeled})
    pieces = [
        _compact({"chain": group.get("chain_id"), "ranges": group.get("ranges"),
                  "residues": group.get("residue_count")})
        for group in _get(result, "preparation_summary.residue_range_groups") or []
        if isinstance(group, dict) and group.get("ranges")
    ]
    ligands = [
        _compact({"id": ligand.get("ligand_id") or ligand.get("residue_name"),
                  "instance": ligand.get("ligand_instance_id"),
                  "net_charge": ligand.get("net_charge"),
                  "protonation": ligand.get("protonation_method"),
                  "ph": ligand.get("protonation_ph")})
        for ligand in result.get("ligands") or []
        if isinstance(ligand, dict) and ligand.get("success", True)
    ]
    disulfides = []
    for pair in result.get("disulfide_bonds") or []:
        cys1, cys2 = pair.get("cys1") or {}, pair.get("cys2") or {}
        if cys1.get("resnum") and cys2.get("resnum"):
            label = f"{cys1.get('chain', '')}{cys1['resnum']}-{cys2.get('chain', '')}{cys2['resnum']}"
            if pair.get("geometry") == "overlap":
                # The deposit overlaps the two sulfurs; the bond is still formed.
                label += f" (overlap {pair.get('distance_angstrom')} A)"
            disulfides.append(label)
    repair = result.get("missing_residue_repair") or result.get("complex_missing_residue_repair")
    clearance = _get(result, "complex_missing_residue_repair.nonpolymer_clearance")
    merge = _get(result, "merge_result.statistics") or {}
    facts = _compact({
        "chains": chains,
        "protonation": _protonation_fact(result, proteins),
        "pieces": pieces or None,
        "ligands": ligands,
        "ligands_dropped": result.get("excluded_ligand_ids") or None,
        "disulfides": disulfides,
        "missing_residues": missing or None,
        "rebuilt_residues": repair if isinstance(repair, (dict, list)) and repair else None,
        "rebuilt_loop_clearance": clearance if isinstance(clearance, dict) and clearance else None,
        "gap_policy": ("ranges are separate pieces; no bond is formed across a gap unless "
                       "--join-range-pieces or --join-range-groups was given") if (pieces or missing) else None,
        "nucleic_chains": len(result.get("nucleics") or []) or None,
        "glycans": len(result.get("glycans") or []) or None,
        "excluded_components": _get(result, "component_disposition_summary.excluded_component_count") or None,
        "atoms": merge.get("total_atoms"),
        "residues": merge.get("total_residues"),
        "solvent_type": result.get("solvent_type"),
        "source_structure": result.get("source_structure_id"),
    })
    return facts


def _summary_prep(facts: dict, result: dict) -> str:
    parts = []
    chains = facts.get("chains") or []
    if chains:
        residues = sum(c.get("residues") or 0 for c in chains)
        ids = "/".join(str(c.get("chain")) for c in chains if c.get("chain"))
        parts.append(f"{len(chains)} protein chain(s) {ids} ({_n(residues)} residues)".replace("  ", " "))
    for piece in facts.get("pieces") or []:
        if piece.get("ranges") and len(piece["ranges"]) > 1:
            parts.append(f"chain {piece.get('chain')} as {len(piece['ranges'])} pieces "
                         f"({', '.join(piece['ranges'])}; gaps left open)")
    for entry in facts.get("missing_residues") or []:
        parts.append(f"chain {entry.get('chain')}: {entry.get('unmodeled_residues')} residues unmodeled")
    ligands = facts.get("ligands") or []
    if ligands:
        names = ", ".join(
            f"{ligand.get('id')}"
            + (f" ({ligand['net_charge']:+d})" if isinstance(ligand.get("net_charge"), int) else "")
            for ligand in ligands
        )
        parts.append(f"{len(ligands)} ligand(s): {names}")
    else:
        parts.append("0 ligands")
    if facts.get("disulfides"):
        parts.append(f"{len(facts['disulfides'])} disulfide(s)")
    protonation = facts.get("protonation") or {}
    if protonation.get("method"):
        text = f"protonation {protonation['method']}"
        if protonation.get("ph") is not None:
            text += f" pH {protonation['ph']}"
        moved = protonation.get("non_fixed_states") or []
        if moved:
            shown = ", ".join(moved[:6]) + (", ..." if len(moved) > 6 else "")
            text += f" ({len(moved)} non-fixed: {shown})"
        parts.append(text)
    if facts.get("atoms"):
        parts.append(f"{_n(facts['atoms'])} atoms")
    return "prepared: " + ", ".join(parts)


def _facts_solv(result: dict) -> dict:
    parameters = result.get("parameters") if isinstance(result.get("parameters"), dict) else {}
    statistics = result.get("statistics") if isinstance(result.get("statistics"), dict) else {}
    box = result.get("box_dimensions") if isinstance(result.get("box_dimensions"), dict) else {}
    ions = result.get("ion_counts") if isinstance(result.get("ion_counts"), dict) else {}
    neutralization = statistics.get("neutralization") if isinstance(statistics.get("neutralization"), dict) else {}
    box_edges = [box.get(key) for key in ("box_a", "box_b", "box_c")]
    facts = _compact({
        "water_model": parameters.get("water_model"),
        "buffer_angstrom": parameters.get("dist"),
        "box_angstrom": [round(float(edge), 1) for edge in box_edges] if all(isinstance(e, (int, float)) for e in box_edges) else None,
        "cubic": box.get("is_cubic"),
        "atoms": statistics.get("total_atoms"),
        "protein_atoms": statistics.get("protein_atoms"),
        "lipids": parameters.get("lipids"),
        "lipid_ratio": parameters.get("ratio") if parameters.get("lipids") else None,
        "orientation": _get(result, "orientation.method"),
        "salt_molar": parameters.get("saltcon") if parameters.get("salt") else None,
        "ions": _compact({
            "cation": ions.get("cation_species") or parameters.get("salt_c"),
            "cations": ions.get("cation_count") or neutralization.get("cations_requested"),
            "anion": ions.get("anion_species") or parameters.get("salt_a"),
            "anions": ions.get("anion_count") or neutralization.get("anions_requested"),
        }) or None,
        "solute_net_charge_e": result.get("solute_net_charge_e", neutralization.get("net_charge")),
        "neutralized": neutralization.get("complete") if neutralization else None,
        "water_residues": neutralization.get("water_residues"),
    })
    return facts


def _summary_solv(facts: dict, result: dict) -> str:
    membrane = bool(facts.get("lipids"))
    parts = []
    if membrane:
        parts.append(f"lipids {facts['lipids']}" + (f" ({facts['lipid_ratio']})" if facts.get("lipid_ratio") not in (None, "1") else ""))
    if facts.get("atoms"):
        atoms = f"{_n(facts['atoms'])} atoms"
        if facts.get("protein_atoms"):
            atoms += f" (protein {_n(facts['protein_atoms'])})"
        parts.append(atoms)
    if facts.get("box_angstrom"):
        parts.append("box " + "×".join(str(e) for e in facts["box_angstrom"]) + " Å")
    if facts.get("water_model"):
        parts.append(f"water {facts['water_model']}")
    ions = facts.get("ions") or {}
    if ions.get("cations") is not None or ions.get("anions") is not None:
        ion_text = f"{ions.get('cation', 'cation')} {_n(ions.get('cations', 0))} / {ions.get('anion', 'anion')} {_n(ions.get('anions', 0))}"
        if facts.get("salt_molar"):
            ion_text += f" ({facts['salt_molar']} M)"
        parts.append(ion_text)
    if facts.get("solute_net_charge_e") is not None:
        parts.append(f"solute charge {facts['solute_net_charge_e']:+g} e")
    if facts.get("orientation"):
        parts.append(f"orientation {facts['orientation']}")
    return ("membrane: " if membrane else "solvated: ") + ", ".join(parts)


def _facts_topo(result: dict) -> dict:
    parameters = result.get("parameters") if isinstance(result.get("parameters"), dict) else {}
    statistics = result.get("statistics") if isinstance(result.get("statistics"), dict) else {}
    provenance = result.get("forcefield_provenance") if isinstance(result.get("forcefield_provenance"), dict) else {}
    charge = result.get("system_net_charge_e", provenance.get("system_net_charge_e"))
    ligand_molecules = provenance.get("ligand_molecules") or []
    facts = _compact({
        "forcefield": parameters.get("forcefield"),
        "forcefield_source": parameters.get("forcefield_source"),
        "water_model": parameters.get("water_model"),
        "water_model_source": parameters.get("water_model_source"),
        "force_field_files": provenance.get("openmm_xml"),
        "ligand_forcefield": provenance.get("small_molecule_forcefield") if ligand_molecules else None,
        "ligands_parameterized": len(ligand_molecules) or None,
        "hmr": result.get("hmr", _get(result, "system_signature.hmr", _get(provenance, "method.hmr"))),
        "is_membrane": parameters.get("is_membrane"),
        "atoms": statistics.get("num_atoms"),
        "residues": statistics.get("num_residues"),
        "net_charge_e": round(float(charge), 3) if isinstance(charge, (int, float)) else None,
        "validation": _get(result, "topology_validation.status"),
        "initial_minimization_energy_kj_mol": _get(result, "minimization.energy_final_kj_mol"),
    })
    return facts


def _summary_topo(facts: dict, result: dict) -> str:
    parts = []
    files = facts.get("force_field_files") or []
    names = [str(f).split("/")[-1].replace(".xml", "") for f in files]
    if names:
        parts.append(" + ".join(names))
    elif facts.get("forcefield"):
        parts.append(f"{facts['forcefield']} + {facts.get('water_model', '?')}")
    if facts.get("water_model_source"):
        parts.append(f"water {facts.get('water_model')} ({facts['water_model_source']})")
    if facts.get("hmr") is not None:
        parts.append("HMR on" if facts["hmr"] else "HMR off")
    if facts.get("ligands_parameterized"):
        parts.append(f"{facts['ligands_parameterized']} ligand(s) via {facts.get('ligand_forcefield', 'GAFF')}")
    if facts.get("atoms"):
        parts.append(f"{_n(facts['atoms'])} atoms" + (f" / {_n(facts['residues'])} residues" if facts.get("residues") else ""))
    if facts.get("net_charge_e") is not None:
        parts.append(f"net charge {facts['net_charge_e']:+.2f} e")
    if facts.get("validation"):
        parts.append(f"validation {facts['validation']}")
    return "topology: " + ", ".join(parts)


def _facts_min(result: dict) -> dict:
    minimization = result.get("minimization") if isinstance(result.get("minimization"), dict) else {}
    return _compact({
        "iterations": minimization.get("max_iterations", result.get("max_iterations")),
        "energy_initial_kj_mol": minimization.get("energy_initial_kj_mol"),
        "energy_final_kj_mol": minimization.get("energy_final_kj_mol"),
        "max_force_final_kj_mol_nm": minimization.get("max_force_final_kj_mol_nm"),
        "restraints": _compact({
            "selection": result.get("restraint_atoms"),
            "atoms": result.get("restraint_count"),
            "force_constant": minimization.get("restraint_force_constant"),
            "lipid_headgroups": result.get("lipid_headgroup_restraint_count"),
        }) or None,
        "platform": result.get("platform"),
    })


def _summary_min(facts: dict, result: dict) -> str:
    parts = []
    if facts.get("iterations"):
        parts.append(f"up to {_n(facts['iterations'])} iterations")
    if facts.get("energy_initial_kj_mol") is not None and facts.get("energy_final_kj_mol") is not None:
        parts.append(f"energy {_n(facts['energy_initial_kj_mol'])} → {_n(facts['energy_final_kj_mol'])} kJ/mol")
    if facts.get("max_force_final_kj_mol_nm") is not None:
        parts.append(f"max force {_n(facts['max_force_final_kj_mol_nm'])} kJ/mol/nm")
    restraints = facts.get("restraints") or {}
    if restraints.get("atoms"):
        text = f"restraints {restraints.get('selection', '')} {_n(restraints['atoms'])} atoms".replace("  ", " ")
        if restraints.get("lipid_headgroups"):
            text += f" + {_n(restraints['lipid_headgroups'])} lipid headgroups"
        parts.append(text)
    if facts.get("platform"):
        parts.append(f"platform {facts['platform']}")
    return "minimized: " + ", ".join(parts)


def _facts_eq(result: dict) -> dict:
    signature = result.get("integrator_signature") if isinstance(result.get("integrator_signature"), dict) else {}
    system = result.get("system_signature") if isinstance(result.get("system_signature"), dict) else {}
    warmup = _get(result, "relaxation_protocol.warmup") or {}
    heating = result.get("nvt_heating") if isinstance(result.get("nvt_heating"), dict) else {}
    return _compact({
        "stages": result.get("stages_completed"),
        "nvt_ns": result.get("effective_nvt_time_ns"),
        "npt_ns": result.get("effective_npt_time_ns"),
        "nvt_steps": result.get("nvt_steps"),
        "npt_steps": result.get("npt_steps"),
        "temperature_kelvin": signature.get("temperature_kelvin", result.get("temperature_kelvin")),
        "pressure_bar": system.get("pressure_bar", result.get("pressure_bar")),
        "timestep_fs": result.get("timestep_fs", signature.get("timestep_fs")),
        "timestep_fs_requested": result.get("timestep_fs_requested"),
        "integrator": signature.get("integrator"),
        "hmr": system.get("hmr"),
        "final_ensemble": result.get("final_ensemble") or system.get("ensemble"),
        "restraints": _compact({"selection": result.get("restraint_atoms"),
                                "atoms": result.get("restraint_count"),
                                "lipid_headgroups": result.get("lipid_headgroup_restraint_count")}) or None,
        "restart_from": result.get("restart_from_node_id"),
        "restart_from_type": result.get("restart_from_node_type"),
        "warmup_retried": warmup.get("retried") or None,
        "heating_retried": heating.get("retried") or None,
        "platform": result.get("platform"),
    })


def _summary_eq(facts: dict, result: dict) -> str:
    parts = []
    stage_text = []
    if facts.get("nvt_ns") is not None:
        stage_text.append(f"NVT {facts['nvt_ns']:g} ns")
    if facts.get("npt_ns"):
        stage_text.append(f"NPT {facts['npt_ns']:g} ns")
    if stage_text:
        parts.append(" + ".join(stage_text))
    conditions = []
    if facts.get("temperature_kelvin") is not None:
        conditions.append(f"{facts['temperature_kelvin']:g} K")
    if facts.get("pressure_bar") is not None:
        conditions.append(f"{facts['pressure_bar']:g} bar")
    if conditions:
        parts.append("at " + " / ".join(conditions))
    if facts.get("timestep_fs") is not None:
        text = f"{facts['timestep_fs']:g} fs"
        if facts.get("timestep_fs_requested") is not None:
            text += f" (requested {facts['timestep_fs_requested']:g}; NaN retry)"
        if facts.get("hmr"):
            text += ", HMR"
        parts.append(text)
    restraints = facts.get("restraints") or {}
    if restraints.get("atoms"):
        parts.append(f"restraints {restraints.get('selection', '')} {_n(restraints['atoms'])} atoms".replace("  ", " "))
    if facts.get("restart_from"):
        parts.append(f"from {facts['restart_from']}")
    if facts.get("platform"):
        parts.append(f"platform {facts['platform']}")
    return "equilibrated: " + ", ".join(parts)


def _facts_prod(result: dict) -> dict:
    signature = result.get("integrator_signature") if isinstance(result.get("integrator_signature"), dict) else {}
    return _compact({
        "ensemble": result.get("ensemble"),
        "simulation_time_ns": result.get("simulation_time_ns"),
        "temperature_kelvin": result.get("temperature_kelvin", signature.get("temperature_kelvin")),
        "pressure_bar": result.get("pressure_bar"),
        "timestep_fs": result.get("timestep_fs", signature.get("timestep_fs")),
        "hmr": result.get("hmr"),
        "steps": result.get("steps_completed") or result.get("num_steps"),
        "restarted_from": result.get("restarted_from"),
        "restart_from": result.get("restart_from_node_id"),
        "restart_integrator_changes": result.get("restart_integrator_changes"),
        "trajectory_file": result.get("trajectory_file"),
        "platform": result.get("platform"),
    })


def _summary_prod(facts: dict, result: dict) -> str:
    parts = []
    if facts.get("simulation_time_ns") is not None:
        parts.append(f"{facts['simulation_time_ns']:g} ns" + (f" {facts['ensemble']}" if facts.get("ensemble") else ""))
    conditions = []
    if facts.get("temperature_kelvin") is not None:
        conditions.append(f"{facts['temperature_kelvin']:g} K")
    if facts.get("pressure_bar") is not None:
        conditions.append(f"{facts['pressure_bar']:g} bar")
    if conditions:
        parts.append("at " + " / ".join(conditions))
    if facts.get("timestep_fs") is not None:
        parts.append(f"{facts['timestep_fs']:g} fs" + (", HMR" if facts.get("hmr") else ""))
    if facts.get("restart_from"):
        parts.append(f"from {facts['restart_from']}")
    if facts.get("restart_integrator_changes"):
        parts.append("integrator settings changed from the restart (see warnings)")
    if facts.get("platform"):
        parts.append(f"platform {facts['platform']}")
    return "production: " + ", ".join(parts)


def _facts_fep(result: dict) -> dict:
    windows = result.get("windows") if isinstance(result.get("windows"), list) else []
    rates = [w.get("ns_per_day") for w in windows if isinstance(w, dict) and w.get("ns_per_day")]
    return _compact({
        "lambda_indices": result.get("lambda_indices"),
        "n_protocol_windows": result.get("n_protocol_windows"),
        "sampling_time_ns": windows[0].get("sampling_time_ns") if windows else None,
        "ensemble": result.get("ensemble"),
        "temperature_kelvin": result.get("temperature_kelvin"),
        "pressure_bar": result.get("pressure_bar"),
        "timestep_fs": result.get("timestep_fs"),
        "hmr": result.get("hmr"),
        "ns_per_day": round(sum(rates) / len(rates), 1) if rates else None,
        "platform": result.get("platform"),
    })


def _summary_fep(facts: dict, result: dict) -> str:
    parts = []
    idx = facts.get("lambda_indices") or []
    if idx:
        span = f"{idx[0]}-{idx[-1]}" if len(idx) > 1 else str(idx[0])
        parts.append(f"{len(idx)} of {facts.get('n_protocol_windows', '?')} windows ({span})")
    if facts.get("sampling_time_ns") is not None:
        parts.append(f"{facts['sampling_time_ns']:g} ns each" + (f" {facts['ensemble']}" if facts.get("ensemble") else ""))
    conditions = []
    if facts.get("temperature_kelvin") is not None:
        conditions.append(f"{facts['temperature_kelvin']:g} K")
    if facts.get("pressure_bar") is not None:
        conditions.append(f"{facts['pressure_bar']:g} bar")
    if conditions:
        parts.append("at " + " / ".join(conditions))
    if facts.get("timestep_fs") is not None:
        parts.append(f"{facts['timestep_fs']:g} fs" + (", HMR" if facts.get("hmr") else ""))
    if facts.get("ns_per_day"):
        parts.append(f"{facts['ns_per_day']:g} ns/day")
    if facts.get("platform"):
        parts.append(f"platform {facts['platform']}")
    return "fep windows: " + ", ".join(parts)


def _facts_generic(result: dict) -> dict:
    facts = {}
    for key, value in result.items():
        if key in ("success", "errors", "warnings", "hints", "code", "message") or key.startswith("_"):
            continue
        if isinstance(value, (str, int, float, bool)) and not (isinstance(value, str) and len(value) > 120):
            facts[key] = value
        if len(facts) >= 12:
            break
    return facts


def _summary_generic(facts: dict, result: dict) -> str:
    return "completed"


_FACTS: dict[str, Callable[[dict], dict]] = {
    "source": _facts_source, "prep": _facts_prep, "solv": _facts_solv, "topo": _facts_topo,
    "min": _facts_min, "eq": _facts_eq, "prod": _facts_prod, "fep": _facts_fep,
}
_SUMMARY: dict[str, Callable[[dict, dict], str]] = {
    "source": _summary_source, "prep": _summary_prep, "solv": _summary_solv,
    "topo": _summary_topo, "min": _summary_min, "eq": _summary_eq, "prod": _summary_prod,
    "fep": _summary_fep,
}


def build_receipt(*, tool_name: str, node_type: Optional[str], result: dict,
                  explicit: Optional[dict] = None, node_mode: bool = False) -> dict:
    """The ``applied`` block for a stage tool result."""
    options, ignored = option_lines(result, explicit or {}, node_type=node_type, node_mode=node_mode)
    facts = _FACTS.get(node_type or "", _facts_generic)(result)
    summary = _SUMMARY.get(node_type or "", _summary_generic)(facts, result)
    return {"tool": tool_name, "summary": summary, "options": options,
            "ignored_options": ignored, "facts": facts}
