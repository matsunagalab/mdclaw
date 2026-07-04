"""PDB residue iteration, nucleic charge deltas, and solute identity restore.

Extracted from ``mdclaw/solvation_server.py``. These helpers parse PDB residue
structure, estimate the packmol-memgen ``--charge_pdb_delta`` needed for
standard nucleic termini, and restore solute identity columns after
packmol-memgen renumbering.
"""

from __future__ import annotations

from pathlib import Path

from mdclaw.chemistry_constants import (
    STANDARD_DNA_RESNAMES,
    STANDARD_RNA_RESNAMES,
)
from mdclaw.solvation.constants import (
    NUCLEIC_RESNAME_KIND,
    TERMINAL_DNA_RESNAMES,
    TERMINAL_RNA_RESNAMES,
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


def _pdb_residue_key(line: str) -> tuple[str, str, str]:
    chain_id = line[21:22].strip() if len(line) >= 22 else ""
    resseq = line[22:26].strip() if len(line) >= 26 else ""
    icode = line[26:27].strip() if len(line) >= 27 else ""
    return chain_id, resseq, icode


def _pdb_residue_label(residue: dict) -> str:
    chain_id = residue["chain_id"] or "-"
    insertion = residue["insertion_code"]
    suffix = insertion if insertion else ""
    return f"{chain_id}:{residue['residue_number']}{suffix}:{residue['resname']}"


def _iter_pdb_residues(path: Path) -> list[dict]:
    residues: list[dict] = []
    current_key: tuple[str, str, str] | None = None
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
            chain_id, resseq, icode = key
            current_key = key
            current = {
                "chain_id": chain_id,
                "residue_number": resseq,
                "insertion_code": icode,
                "resname": line[17:20].strip() if len(line) >= 20 else "",
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


def _flush_nucleic_segment(
    segment: list[dict],
    *,
    kind: str,
    segments: list[dict],
) -> int:
    if not segment:
        return 0

    resnames = [residue["resname"] for residue in segment]
    terminal_resnames = (
        TERMINAL_DNA_RESNAMES if kind == "DNA" else TERMINAL_RNA_RESNAMES
    )
    standard_resnames = STANDARD_DNA_RESNAMES if kind == "DNA" else STANDARD_RNA_RESNAMES
    terminal_names_present = sorted({name for name in resnames if name in terminal_resnames})

    correction = 0
    skipped_reason: str | None = None
    if len(segment) < 2:
        skipped_reason = "short_nucleic_segment"
    elif terminal_names_present:
        skipped_reason = "terminal_residue_names_present"
    elif all(name in standard_resnames for name in resnames):
        correction = 1
    else:
        skipped_reason = "nonstandard_nucleic_residue_names"

    entry = {
        "kind": kind,
        "chain_id": segment[0]["chain_id"],
        "start": _pdb_residue_label(segment[0]),
        "end": _pdb_residue_label(segment[-1]),
        "length": len(segment),
        "residue_names": resnames,
        "charge_pdb_delta": correction,
    }
    if terminal_names_present:
        entry["terminal_residue_names_present"] = terminal_names_present
    if skipped_reason:
        entry["skipped_reason"] = skipped_reason
    segments.append(entry)
    return correction


def _auto_nucleic_packmol_charge_pdb_delta(pdb_path: Path) -> dict:
    """Return packmol-memgen charge delta needed for standard nucleic termini.

    packmol-memgen estimates standard DA/DC/DG/DT/A/C/G/U residues as -1 each.
    Amber terminal templates make each ordinary linear nucleic-acid segment one
    charge unit less negative overall. Pass that difference through
    ``--charge_pdb_delta`` instead of rewriting final PDB residue names.
    """
    residues = _iter_pdb_residues(pdb_path)
    segments: list[dict] = []
    current_segment: list[dict] = []
    current_kind: str | None = None
    current_chain: str | None = None
    charge_delta = 0

    for residue in residues:
        kind = NUCLEIC_RESNAME_KIND.get(residue["resname"])
        if (
            kind is None
            or current_kind is None
            or kind != current_kind
            or residue["chain_id"] != current_chain
            or residue.get("segment_break_before")
        ):
            if current_segment and current_kind is not None:
                charge_delta += _flush_nucleic_segment(
                    current_segment,
                    kind=current_kind,
                    segments=segments,
                )
            current_segment = []
            current_kind = None
            current_chain = None

        if kind is None:
            continue

        current_segment.append(residue)
        current_kind = kind
        current_chain = residue["chain_id"]

    if current_segment and current_kind is not None:
        charge_delta += _flush_nucleic_segment(
            current_segment,
            kind=current_kind,
            segments=segments,
        )

    applied_segments = [
        segment for segment in segments if int(segment.get("charge_pdb_delta", 0)) != 0
    ]
    return {
        "charge_pdb_delta": int(charge_delta),
        "segments": segments,
        "applied_segment_count": len(applied_segments),
        "reason": (
            f"standard nucleic termini: {len(applied_segments)} segment(s) x +1"
            if applied_segments
            else "no standard nucleic terminal correction needed"
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
# system is left one charge too positive per CYM. Anionic-lipid head groups
# (Lipid21 POPG head residue "PGR", POPS "PGR"/"PSER") are included so this
# helper also corrects charged-lipid solutes when it runs on membrane inputs.
# Values are (true_formal_charge - packmol_estimated_charge).
_PACKMOL_UNRECOGNIZED_RESIDUE_DELTAS: dict[str, int] = {
    "CYM": -1,
    "PGR": -1,
    "PSER": -1,
}

_PACKMOL_CHARGE_TRACKING_RESNAMES = (
    set(_PACKMOL_RECOGNIZED_ION_CHARGES)
    | set(_PACKMOL_RECOGNIZED_RESIDUE_CHARGES)
    | set(STANDARD_DNA_RESNAMES)
    | set(STANDARD_RNA_RESNAMES)
    | set(TERMINAL_DNA_RESNAMES)
    | set(TERMINAL_RNA_RESNAMES)
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
    that covers standard amino acids, nucleotides, and a few ions (MG, CA,
    Na+, Cl-). Charged MD components it does not recognize therefore leave a
    residual net charge after neutralization:

    - Transition-metal / non-MG/CA ions kept as components (ZN, MN, FE, CO,
      NI, CU, ...) are counted as neutral, so a catalytic Zn2+ leaves +2.
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
    from mdclaw.chemistry_constants import METAL_CHARGES

    residues = _iter_pdb_residues(pdb_path)
    ions: list[dict] = []
    charge_delta = 0
    packmol_charge_track: str | None = None
    for residue in residues:
        resname = (residue.get("resname") or "").strip().upper()
        atom_count = residue.get("atom_count", 0)
        track_resseq = _packmol_charge_tracking_resseq(residue)
        packmol_counts_this_residue = (
            resname in _PACKMOL_CHARGE_TRACKING_RESNAMES
            and packmol_charge_track != track_resseq
        )
        if packmol_counts_this_residue:
            packmol_charge_track = track_resseq

        contribution: int | None = None
        formal_charge: int | None = None
        already_counted = 0
        kind = None
        atom_names = _atom_names(residue)
        # Monoatomic residues are treated as bare metal ions; this avoids
        # misreading a metal atom that belongs to a larger cofactor residue.
        if atom_count == 1 and resname in METAL_CHARGES:
            formal_charge = int(METAL_CHARGES[resname])
            if packmol_counts_this_residue:
                already_counted = int(_PACKMOL_RECOGNIZED_ION_CHARGES.get(resname, 0))
            contribution = formal_charge - already_counted
            kind = "metal_ion"
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
            "resname": resname,
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
