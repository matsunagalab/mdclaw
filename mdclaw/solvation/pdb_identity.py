"""PDB residue iteration, nucleic charge deltas, and solute identity restore.

Extracted from ``mdclaw/solvation_server.py``. These helpers parse PDB residue
structure, estimate the packmol-memgen ``--charge_pdb_delta`` needed for
standard nucleic termini, and restore solute identity columns after
packmol-memgen renumbering.
"""

from __future__ import annotations

from pathlib import Path

from mdclaw.forcefield_catalog import (
    DNA_XML,
    LIPID_XML,
    OPENMM_APP_LIPID_XML,
    RNA_XML,
)
from mdclaw.forcefield_templates import (
    load_lipid_template_contract,
    load_nucleic_template_families,
    load_residue_templates,
    nucleic_residue_name_map,
)


def _pdb_atom_lines(path: Path) -> list[str]:
    return [
        line.rstrip("\n")
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.startswith(("ATOM", "HETATM"))
    ]


def _pdb_atom_name(line: str) -> str:
    return line[12:16].strip() if len(line) >= 16 else ""


def _pdb_element(line: str) -> str:
    if len(line) >= 78 and line[76:78].strip():
        return line[76:78].strip().upper()
    atom = _pdb_atom_name(line)
    return "".join(ch for ch in atom if ch.isalpha())[:1].upper()


def _pdb_residue_key(line: str) -> tuple[str, str, str, str]:
    chain_id = line[21:22].strip() if len(line) >= 22 else ""
    resseq = line[22:26].strip() if len(line) >= 26 else ""
    icode = line[26:27].strip() if len(line) >= 27 else ""
    resname = line[17:21].strip() if len(line) >= 21 else ""
    return chain_id, resseq, icode, resname


def _pdb_residue_label(residue: dict) -> str:
    chain_id = residue["chain_id"] or "-"
    insertion = residue["insertion_code"]
    suffix = insertion if insertion else ""
    return f"{chain_id}:{residue['residue_number']}{suffix}:{residue['resname']}"


def _iter_pdb_residues(path: Path) -> list[dict]:
    residues: list[dict] = []
    current_key: tuple[str, str, str, str] | None = None
    current: dict | None = None
    segment_break_before_next = False

    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("TER"):
            if current is not None:
                residues.append(current)
            current_key = None
            current = None
            segment_break_before_next = True
            continue
        if not line.startswith(("ATOM", "HETATM")):
            continue
        key = _pdb_residue_key(line)
        if key != current_key:
            if current is not None:
                residues.append(current)
            chain_id, resseq, icode, resname = key
            current_key = key
            current = {
                "chain_id": chain_id,
                "residue_number": resseq,
                "insertion_code": icode,
                "resname": resname,
                "atom_count": 0,
                "atom_names": [],
                "segment_break_before": segment_break_before_next,
            }
            segment_break_before_next = False
        if current is not None:
            current["atom_count"] += 1
            current["atom_names"].append(_pdb_atom_name(line))

    if current is not None:
        residues.append(current)
    return residues


_PACKMOL_NUCLEIC_ROLE_CHARGES = {
    "DNA": {"internal": -1.0, "five_prime": -0.3079, "three_prime": -0.6921},
    "RNA": {"internal": -1.0, "five_prime": -0.3081, "three_prime": -0.6919},
}


def _flush_nucleic_segment(
    segment: list[dict],
    *,
    kind: str,
    forcefield_xml: str,
    segments: list[dict],
) -> tuple[int, list[str]]:
    if not segment:
        return 0, []

    resnames = [residue["resname"] for residue in segment]
    name_map = nucleic_residue_name_map(forcefield_xml)
    templates = load_residue_templates(forcefield_xml)
    families = [name_map[name] for name in resnames]
    if len(segment) == 1:
        roles = ["single"]
        explicit_names = [families[0].single]
    else:
        roles = ["five_prime"] + ["internal"] * (len(segment) - 2) + ["three_prime"]
        explicit_names = [families[0].five_prime]
        explicit_names.extend(family.internal for family in families[1:-1])
        explicit_names.append(families[-1].three_prime)

    internal_names = [family.internal for family in families]
    if resnames == internal_names:
        naming_mode = "internal_names"
    elif resnames == explicit_names:
        naming_mode = "explicit_terminal_names"
    else:
        error = (
            f"{kind} segment {_pdb_residue_label(segment[0])} to "
            f"{_pdb_residue_label(segment[-1])} mixes or misplaces terminal "
            "residue names. Use either ordinary base names throughout or a "
            "complete 5'/3'/single terminal naming scheme."
        )
        segments.append({
            "kind": kind,
            "chain_id": segment[0]["chain_id"],
            "start": _pdb_residue_label(segment[0]),
            "end": _pdb_residue_label(segment[-1]),
            "length": len(segment),
            "residue_names": resnames,
            "forcefield_xml": forcefield_xml,
            "charge_pdb_delta": 0,
            "code": "forcefield_template_contract_mismatch",
            "error": error,
        })
        return 0, [error]

    selected_template_names: list[str] = []
    forcefield_charge = 0.0
    packmol_charge = 0.0
    for family, role in zip(families, roles):
        template_name = getattr(family, role)
        selected_template_names.append(template_name)
        forcefield_charge += templates[template_name].net_charge
        if naming_mode == "internal_names":
            packmol_charge += _PACKMOL_NUCLEIC_ROLE_CHARGES[kind]["internal"]
        elif role != "single":
            packmol_charge += _PACKMOL_NUCLEIC_ROLE_CHARGES[kind][role]

    raw_correction = forcefield_charge - packmol_charge
    correction = int(round(raw_correction))
    errors: list[str] = []
    if abs(raw_correction - correction) > 1.0e-3:
        errors.append(
            f"{kind} terminal charge correction from {forcefield_xml} is not "
            f"an integer ({raw_correction:+.6f}); packmol-memgen accepts only "
            "integer charge_pdb_delta values."
        )

    entry = {
        "kind": kind,
        "chain_id": segment[0]["chain_id"],
        "start": _pdb_residue_label(segment[0]),
        "end": _pdb_residue_label(segment[-1]),
        "length": len(segment),
        "residue_names": resnames,
        "selected_template_names": selected_template_names,
        "naming_mode": naming_mode,
        "forcefield_xml": forcefield_xml,
        "forcefield_charge": forcefield_charge,
        "packmol_estimated_charge": packmol_charge,
        "charge_pdb_delta": correction,
    }
    if errors:
        entry["code"] = "forcefield_template_contract_mismatch"
        entry["error"] = errors[0]
    segments.append(entry)
    return correction, errors


def _auto_nucleic_packmol_charge_pdb_delta(
    pdb_path: Path,
    *,
    dna_xml: str = DNA_XML["OL15"],
    rna_xml: str = RNA_XML["OL3"],
) -> dict:
    """Return packmol-memgen charge delta needed for standard nucleic termini.

    packmol-memgen estimates standard DA/DC/DG/DT/A/C/G/U residues as -1 each.
    Amber terminal templates make each ordinary linear nucleic-acid segment one
    charge unit less negative overall. Pass that difference through
    ``--charge_pdb_delta`` instead of rewriting final PDB residue names.
    """
    xml_by_kind = {"DNA": dna_xml, "RNA": rna_xml}
    kind_by_resname = {
        **{name: "DNA" for name in nucleic_residue_name_map(dna_xml)},
        **{name: "RNA" for name in nucleic_residue_name_map(rna_xml)},
    }
    residues = _iter_pdb_residues(pdb_path)
    segments: list[dict] = []
    current_segment: list[dict] = []
    current_kind: str | None = None
    current_chain: str | None = None
    charge_delta = 0
    errors: list[str] = []

    for residue in residues:
        kind = kind_by_resname.get(residue["resname"])
        if (
            kind is None
            or current_kind is None
            or kind != current_kind
            or residue["chain_id"] != current_chain
            or residue.get("segment_break_before")
        ):
            if current_segment and current_kind is not None:
                segment_delta, segment_errors = _flush_nucleic_segment(
                    current_segment,
                    kind=current_kind,
                    forcefield_xml=xml_by_kind[current_kind],
                    segments=segments,
                )
                charge_delta += segment_delta
                errors.extend(segment_errors)
            current_segment = []
            current_kind = None
            current_chain = None

        if kind is None:
            continue

        current_segment.append(residue)
        current_kind = kind
        current_chain = residue["chain_id"]

    if current_segment and current_kind is not None:
        segment_delta, segment_errors = _flush_nucleic_segment(
            current_segment,
            kind=current_kind,
            forcefield_xml=xml_by_kind[current_kind],
            segments=segments,
        )
        charge_delta += segment_delta
        errors.extend(segment_errors)

    applied_segments = [
        segment for segment in segments if int(segment.get("charge_pdb_delta", 0)) != 0
    ]
    return {
        "success": not errors,
        "code": "forcefield_template_contract_mismatch" if errors else None,
        "errors": errors,
        "charge_pdb_delta": int(charge_delta),
        "segments": segments,
        "forcefield_xml": xml_by_kind,
        "applied_segment_count": len(applied_segments),
        "reason": (
            f"force-field nucleic termini: {len(applied_segments)} corrected segment(s)"
            if applied_segments
            else "no standard nucleic terminal correction needed"
        ),
    }


def _lipid_residue_group_key(residue: dict) -> tuple[str, str, str]:
    return (
        str(residue.get("chain_id") or ""),
        str(residue.get("residue_number") or ""),
        str(residue.get("insertion_code") or ""),
    )


def _auto_lipid_packmol_charge_pdb_delta(
    pdb_path: Path,
    *,
    modular_xml: str = LIPID_XML["lipid21"],
    full_xml: str = OPENMM_APP_LIPID_XML["lipid21_full"],
) -> dict:
    """Return retained-lipid charges omitted by packmol-memgen."""
    contract = load_lipid_template_contract(modular_xml, full_xml)
    modular_templates = load_residue_templates(modular_xml)
    full_templates = load_residue_templates(full_xml)
    residues = _iter_pdb_residues(pdb_path)
    full_residues = [
        residue for residue in residues if residue["resname"] in contract.full_names
    ]
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for residue in residues:
        if residue["resname"] in contract.modular_names:
            grouped.setdefault(_lipid_residue_group_key(residue), []).append(residue)
    lipid_groups = [
        group
        for group in grouped.values()
        if any(
            residue["resname"] in contract.head_names
            or residue["resname"] == "CHL"
            for residue in group
        )
    ]
    errors: list[str] = []
    lipids: list[dict] = []

    if lipid_groups and full_residues:
        errors.append(
            "The input mixes modular and whole-residue Lipid21 representations; "
            "one OpenMM Lipid21 XML cannot parameterize both representations."
        )

    for residue in full_residues:
        charge = full_templates[residue["resname"]].net_charge
        rounded = int(round(charge))
        if abs(charge - rounded) > 1.0e-3:
            errors.append(
                f"Lipid template {residue['resname']} in {full_xml} has "
                f"non-integral net charge {charge:+.6f}."
            )
        lipids.append({
            "residue": _pdb_residue_label(residue),
            "residue_names": [residue["resname"]],
            "representation": "full",
            "forcefield_xml": full_xml,
            "forcefield_charge": charge,
            "packmol_estimated_charge": 0.0,
            "charge_pdb_delta": rounded,
        })

    for group in lipid_groups:
        names = [residue["resname"] for residue in group]
        heads = [name for name in names if name in contract.head_names]
        tails = [name for name in names if name in contract.tail_names]
        zero_external = [
            name
            for name in names
            if name not in contract.head_names and name not in contract.tail_names
        ]
        complete_sterol = len(names) == 1 and names[0] == "CHL"
        complete_fragmented = (
            len(heads) == 1 and len(tails) == 2 and not zero_external
        )
        if not (complete_sterol or complete_fragmented):
            errors.append(
                f"Lipid21 fragments at {_pdb_residue_label(group[0])} do not "
                f"form one complete lipid: {names}."
            )
        charge = sum(modular_templates[name].net_charge for name in names)
        rounded = int(round(charge))
        if abs(charge - rounded) > 1.0e-3:
            errors.append(
                f"Lipid21 fragments {names} in {modular_xml} have non-integral "
                f"net charge {charge:+.6f}."
            )
        lipids.append({
            "residue": _pdb_residue_label(group[0]),
            "residue_names": names,
            "representation": "modular",
            "forcefield_xml": modular_xml,
            "forcefield_charge": charge,
            "packmol_estimated_charge": 0.0,
            "charge_pdb_delta": rounded,
        })

    charge_delta = sum(int(lipid["charge_pdb_delta"]) for lipid in lipids)
    applied = [lipid for lipid in lipids if lipid["charge_pdb_delta"]]
    return {
        "success": not errors,
        "code": "forcefield_template_contract_mismatch" if errors else None,
        "errors": errors,
        "charge_pdb_delta": charge_delta,
        "lipids": lipids,
        "applied_lipid_count": len(applied),
        "forcefield_xml": {"modular": modular_xml, "full": full_xml},
        "reason": (
            f"force-field lipid charges: {len(applied)} corrected lipid(s)"
            if applied
            else "no retained lipid charge correction needed"
        ),
    }


# Monoatomic ion residue names whose formal charge packmol-memgen can account
# for during neutralization (see packmol_memgen/lib/utils.py `charged`). Its
# parser only tracks the previous charged residue number, not chain/icode, so
# same-resseq recognized ions can still be missed and need a delta.
_PACKMOL_RECOGNIZED_ION_CHARGES: dict[str, int] = {
    "MG": 2,
    "CA": 2,
}

_PACKMOL_RECOGNIZED_RESIDUE_CHARGES: dict[str, int] = {
    "ASP": -1,
    "GLU": -1,
    "LYS": 1,
    "ARG": 1,
    "HIP": 1,
}

_CANONICAL_PROTONATED_ACID_HYDROGENS: dict[str, set[str]] = {
    "ASP": {"HD", "HD1", "HD2"},
    "GLU": {"HE", "HE1", "HE2"},
}

_NEUTRAL_HISTIDINE_RESNAMES = {"HIS", "HID", "HIE", "HSD", "HSE"}

# Non-monoatomic residues whose true formal charge packmol-memgen does not
# recognize (its `charged` table only covers ASP/GLU/LYS/ARG/HIP and the
# phospho variants). Deprotonated metal-coordinating cysteine (CYM) is the
# common case: each CYM is truly -1 but packmol counts it as neutral, so the
# system is left one charge too positive per CYM. Lipids are evaluated from the
# selected Lipid21 XML by ``_auto_lipid_packmol_charge_pdb_delta``.
# Values are (true_formal_charge - packmol_estimated_charge).
_PACKMOL_UNRECOGNIZED_RESIDUE_DELTAS: dict[str, int] = {
    "CYM": -1,
}

def _packmol_charge_tracking_resnames() -> set[str]:
    nucleic_names = {
        name
        for xml in (DNA_XML["OL15"], RNA_XML["OL3"])
        for family in load_nucleic_template_families(xml).values()
        for name in (family.internal, family.five_prime, family.three_prime)
    }
    return (
        set(_PACKMOL_RECOGNIZED_ION_CHARGES)
        | set(_PACKMOL_RECOGNIZED_RESIDUE_CHARGES)
        | nucleic_names
        | {"OHE"}
        | {
        "PTR", "SEP", "TPO", "Y1P", "S1P", "T1P",
        "H1D", "H2D", "H1E", "H2E", "NME", "ACE",
        }
    )


def _atom_names(residue: dict) -> set[str]:
    return {str(name).strip().upper() for name in residue.get("atom_names", [])}


def _packmol_charge_tracking_resseq(residue: dict) -> str:
    """Return the residue-number key packmol-memgen uses for charge tracking."""
    return str(residue.get("residue_number") or "").strip()


def _auto_metal_ion_packmol_charge_pdb_delta(pdb_path: Path) -> dict:
    """Return the packmol-memgen charge delta for packmol-unrecognized charges.

    packmol-memgen estimates the solute charge from a fixed residue-name table
    that covers standard amino acids, nucleotides, and a few ions (MG, CA).
    Charged MD components it does not recognize therefore leave a
    residual net charge after neutralization:

    - Bare ions not recognized by packmol-memgen (including retained halides
      and transition metals) are counted as neutral, so their full formal
      charge must be added to the estimate.
    - MG/CA are normally counted, but packmol-memgen tracks charged residues
      by residue number only; same-resseq ion collisions can drop later ions.
    - Protonated ASP/GLU residues that have been restored to canonical residue
      names are still counted as -1 by packmol-memgen, while OpenMM/Amber uses
      the explicit side-chain proton and treats them as neutral.
    - Doubly protonated canonical histidine names are counted as neutral, while
      OpenMM/Amber treats them like HIP (+1).
    - Deprotonated metal-coordinating cysteine (CYM, truly -1) is counted as
      neutral, so each CYM leaves +1.
    - Anionic-lipid head groups (PGR/PSER, truly -1) are counted as neutral.

    This sums ``true_formal_charge - packmol_estimate`` over every such
    residue and returns it as a ``--charge_pdb_delta`` so neutralization adds
    the missing counter-ions.
    """
    from mdclaw.chemistry_constants import BARE_ION_CHARGES

    residues = _iter_pdb_residues(pdb_path)
    charge_tracking_resnames = _packmol_charge_tracking_resnames()
    ions: list[dict] = []
    charge_delta = 0
    packmol_charge_track: str | None = None
    for residue in residues:
        exact_resname = (residue.get("resname") or "").strip()
        resname = exact_resname.upper()
        atom_count = residue.get("atom_count", 0)
        track_resseq = _packmol_charge_tracking_resseq(residue)
        packmol_counts_this_residue = (
            resname in charge_tracking_resnames
            and packmol_charge_track != track_resseq
        )
        if packmol_counts_this_residue:
            packmol_charge_track = track_resseq

        contribution: int | None = None
        formal_charge: int | None = None
        already_counted = 0
        kind = None
        atom_names = _atom_names(residue)
        # Only monoatomic residues enter the bare-ion path. This avoids
        # misreading an ion-named atom that belongs to a larger cofactor.
        if atom_count == 1 and exact_resname in BARE_ION_CHARGES:
            formal_charge = int(BARE_ION_CHARGES[exact_resname])
            if packmol_counts_this_residue:
                already_counted = int(_PACKMOL_RECOGNIZED_ION_CHARGES.get(resname, 0))
            contribution = formal_charge - already_counted
            kind = "metal_ion" if formal_charge > 0 else "bare_ion"
        elif resname in _CANONICAL_PROTONATED_ACID_HYDROGENS and (
            atom_names & _CANONICAL_PROTONATED_ACID_HYDROGENS[resname]
        ):
            formal_charge = 0
            if packmol_counts_this_residue:
                already_counted = int(_PACKMOL_RECOGNIZED_RESIDUE_CHARGES[resname])
            contribution = formal_charge - already_counted
            kind = "neutral_protonated_acid"
        elif resname in _NEUTRAL_HISTIDINE_RESNAMES and {"HD1", "HE2"} <= atom_names:
            formal_charge = 1
            if packmol_counts_this_residue:
                already_counted = int(_PACKMOL_RECOGNIZED_RESIDUE_CHARGES.get(resname, 0))
            contribution = formal_charge - already_counted
            kind = "protonated_histidine"
        elif resname in _PACKMOL_UNRECOGNIZED_RESIDUE_DELTAS:
            contribution = int(_PACKMOL_UNRECOGNIZED_RESIDUE_DELTAS[resname])
            formal_charge = contribution
            kind = "charged_residue"

        if contribution is None:
            continue

        entry = {
            "residue": _pdb_residue_label(residue),
            "resname": (
                exact_resname
                if atom_count == 1 and exact_resname in BARE_ION_CHARGES
                else resname
            ),
            "kind": kind,
            "formal_charge": formal_charge,
            "packmol_recognized_charge": already_counted,
            "charge_pdb_delta": contribution,
            "packmol_charge_tracking_resseq": track_resseq,
            "packmol_charge_counted": bool(packmol_counts_this_residue),
        }
        if kind == "neutral_protonated_acid":
            entry["sidechain_proton_atoms"] = sorted(
                atom_names & _CANONICAL_PROTONATED_ACID_HYDROGENS[resname]
            )
        elif kind == "protonated_histidine":
            entry["sidechain_proton_atoms"] = ["HD1", "HE2"]
        ions.append(entry)
        charge_delta += contribution

    applied_ions = [ion for ion in ions if int(ion.get("charge_pdb_delta", 0)) != 0]
    return {
        "charge_pdb_delta": int(charge_delta),
        "ions": ions,
        "applied_ion_count": len(applied_ions),
        "reason": (
            "charges not counted by packmol-memgen: "
            + ", ".join(f"{ion['resname']}({ion['charge_pdb_delta']:+d})"
                        for ion in applied_ions)
            if applied_ions
            else "no uncounted metal/charged-residue correction needed"
        ),
    }


def _ligand_chemistry_packmol_charge_pdb_delta(ligand_chemistry: list[dict] | None) -> dict:
    """Return charge delta for prepared ligands absent from packmol-memgen's table.

    ``prepare_complex`` records ligand formal charge in ``ligand_chemistry``.
    packmol-memgen does not read that artifact and estimates input PDB charge
    from a fixed residue-name table, so arbitrary GAFF/OpenFF ligands are
    counted as neutral unless their residue name happens to be a built-in
    charged residue.  The topology step later uses the chemistry graph and
    therefore sees the true formal charge.  This bridges that prep -> solv
    contract by adding the ligand formal charge to ``--charge_pdb_delta``.
    """
    entries: list[dict] = []
    charge_delta = 0
    for index, ligand in enumerate(ligand_chemistry or []):
        if not isinstance(ligand, dict):
            entries.append({
                "index": index,
                "skipped_reason": "non_dict_ligand_chemistry_record",
            })
            continue

        raw_charge = ligand.get("net_charge")
        if raw_charge is None:
            raw_charge = ligand.get("mol_formal_charge")
        if raw_charge is None:
            entries.append({
                "index": index,
                "residue_name": ligand.get("residue_name") or ligand.get("ligand_id"),
                "ligand_instance_id": ligand.get("ligand_instance_id"),
                "skipped_reason": "missing_formal_charge",
            })
            continue

        try:
            charge_float = float(raw_charge)
        except (TypeError, ValueError):
            entries.append({
                "index": index,
                "residue_name": ligand.get("residue_name") or ligand.get("ligand_id"),
                "ligand_instance_id": ligand.get("ligand_instance_id"),
                "raw_charge": raw_charge,
                "skipped_reason": "non_numeric_formal_charge",
            })
            continue

        formal_charge = int(round(charge_float))
        entry = {
            "index": index,
            "residue_name": ligand.get("residue_name") or ligand.get("ligand_id"),
            "ligand_instance_id": ligand.get("ligand_instance_id"),
            "formal_charge": formal_charge,
            "packmol_recognized_charge": 0,
            "charge_pdb_delta": formal_charge,
        }
        if abs(charge_float - formal_charge) > 1.0e-6:
            entry["charge_rounding_warning"] = charge_float
        entries.append(entry)
        charge_delta += formal_charge

    applied = [entry for entry in entries if int(entry.get("charge_pdb_delta", 0)) != 0]
    return {
        "charge_pdb_delta": int(charge_delta),
        "ligands": entries,
        "applied_ligand_count": len(applied),
        "reason": (
            "ligand charges not counted by packmol-memgen: "
            + ", ".join(
                f"{entry.get('residue_name') or '?'}({entry['charge_pdb_delta']:+d})"
                for entry in applied
            )
            if applied
            else "no ligand charge correction needed"
        ),
    }


def _restore_packmol_solute_identity(input_pdb: Path, output_pdb: Path) -> dict:
    """Restore solute PDB identity columns after packmol-memgen renumbering."""
    report = {
        "solute_identity_restored": False,
        "solute_identity_restored_atom_count": 0,
        "solute_identity_restore_warnings": [],
    }
    try:
        input_atoms = _pdb_atom_lines(input_pdb)
        output_lines = output_pdb.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as exc:
        report["solute_identity_restore_warnings"].append(f"Could not read PDB for solute identity restore: {exc}")
        return report

    if not input_atoms:
        report["solute_identity_restore_warnings"].append("Input PDB has no ATOM/HETATM records")
        return report

    output_atom_indices = [
        idx for idx, line in enumerate(output_lines)
        if line.startswith(("ATOM", "HETATM"))
    ]
    if len(output_atom_indices) < len(input_atoms):
        report["solute_identity_restore_warnings"].append(
            f"Packmol output has fewer atom records ({len(output_atom_indices)}) than input solute ({len(input_atoms)})"
        )
        return report

    mismatches: list[str] = []
    for atom_i, (src, out_idx) in enumerate(zip(input_atoms, output_atom_indices), start=1):
        dst = output_lines[out_idx]
        src_name = _pdb_atom_name(src)
        dst_name = _pdb_atom_name(dst)
        src_element = _pdb_element(src)
        dst_element = _pdb_element(dst)
        if src_name != dst_name or (src_element and dst_element and src_element != dst_element):
            mismatches.append(
                f"atom {atom_i}: {src_name}/{src_element} != {dst_name}/{dst_element}"
            )
            if len(mismatches) >= 3:
                break
    if mismatches:
        report["solute_identity_restore_warnings"].append(
            "Skipped solute identity restore because packmol output prefix did not match input solute: "
            + "; ".join(mismatches)
        )
        return report

    restored_lines = list(output_lines)
    for src, out_idx in zip(input_atoms, output_atom_indices):
        dst = restored_lines[out_idx].ljust(80)
        src_padded = src.ljust(80)
        restored_lines[out_idx] = (
            src_padded[:6]
            + dst[6:12]
            + src_padded[12:27]
            + dst[27:76]
            + src_padded[76:78]
            + dst[78:]
        ).rstrip()

    output_pdb.write_text("\n".join(restored_lines) + "\n", encoding="utf-8")
    report["solute_identity_restored"] = True
    report["solute_identity_restored_atom_count"] = len(input_atoms)
    return report
