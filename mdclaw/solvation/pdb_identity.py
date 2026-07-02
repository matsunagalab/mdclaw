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
                "segment_break_before": segment_break_before_next,
            }
            segment_break_before_next = False
        if current is not None:
            current["atom_count"] += 1

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
