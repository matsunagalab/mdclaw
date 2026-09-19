"""``extract_tripeptide`` — the unfolded-state model for folding-stability ddG.

Cuts residues ``i-flank .. i+flank`` of the mutated chain out of a protein PDB
and writes them as a stand-alone peptide, keeping the original chain ID and
residue numbers so the very same ``--mutation A:L99A`` spec applies to both
legs. The fragment is *not* capped here: register it as the source of a second
job and let the ordinary ``md-prepare`` chain add ACE/NME
(``prepare_complex --cap-termini``), solvate, and build the hybrid topology.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from mdclaw._common import create_validation_error
from mdclaw.fep.mutant import MutantBuildError, parse_single_mutation
from mdclaw.sidechain_packer import _is_standard_protein_atom

_HYDROGEN_ELEMENTS = {"H", "D"}


def _residue_key(line: str) -> tuple[str, int, str]:
    return line[21], int(line[22:26]), line[26].strip()


def _is_hydrogen(line: str) -> bool:
    element = line[76:78].strip().upper()
    if element:
        return element in _HYDROGEN_ELEMENTS
    name = line[12:16].strip()
    return name[:1] in _HYDROGEN_ELEMENTS or (name[:1].isdigit() and name[1:2] == "H")


def extract_tripeptide(
    pdb_file: str,
    mutation: str,
    flank: int = 1,
    output_file: Optional[str] = None,
    strip_hydrogens: bool = False,
) -> dict:
    """Write the ``2*flank+1``-residue peptide around a mutation site.

    Args:
        pdb_file: Protein PDB the folded leg was prepared from (the prep
            node's cleaned protein keeps the protonation states in sync).
        mutation: ``A:L99A`` / ``L99A`` — the residue to centre on.
        flank: Residues kept on each side (1 gives the classic tripeptide).
        output_file: Output PDB (default: ``<stem>_<mutation>_tripeptide.pdb``
            beside the input).
        strip_hydrogens: Drop hydrogens so the prep chain re-protonates from
            scratch (default keeps them).

    Returns:
        ``tripeptide_pdb``, the residues kept, and the follow-up commands
        (register the file as a new job's source, then
        ``prepare_complex --cap-termini``).
    """
    result: dict = {"success": False, "tool": "extract_tripeptide", "errors": [], "warnings": []}
    src = Path(pdb_file)
    if not src.is_file():
        return {**result, **create_validation_error("pdb_file", f"{pdb_file} not found", code="file_not_found")}
    if flank < 0:
        return {**result, **create_validation_error("flank", "flank must be >= 0", code="invalid_parameter_value")}
    try:
        spec = parse_single_mutation(mutation, src)
    except MutantBuildError as exc:
        return {**result, **create_validation_error("mutation", str(exc), code=exc.code)}

    lines = src.read_text().splitlines()
    protein = [ln for ln in lines if ln.startswith(("ATOM  ", "HETATM")) and _is_standard_protein_atom(ln)]
    chain = spec.chain_id or " "  # blank chain ids are stored as a space in PDB column 22
    chain_lines = [ln for ln in protein if ln[21] == chain]
    order: list[tuple[str, int, str]] = []
    for ln in chain_lines:
        key = _residue_key(ln)
        if not order or order[-1] != key:
            if key in order:
                return {**result, **create_validation_error(
                    "pdb_file", f"chain {spec.chain_id} lists residue {key[1]}{key[2]} in two places; "
                    "extract from a cleaned protein PDB", code="fep_tripeptide_extraction_failed")}
            order.append(key)
    target = (chain, spec.resseq, spec.icode or "")
    if target not in order:
        return {**result, **create_validation_error(
            "mutation", f"{spec.label}: residue not found in chain {spec.chain_id}", code="fep_mutation_residue_not_found")}
    idx = order.index(target)
    lo, hi = max(0, idx - flank), min(len(order) - 1, idx + flank)
    keep = order[lo:hi + 1]
    if lo > idx - flank or hi < idx + flank:
        result["warnings"].append(
            f"{spec.label} sits {idx} residue(s) from the chain start / {len(order) - 1 - idx} from its end; "
            f"only {len(keep)} residues could be kept.")
    # Gaps: consecutive residue numbers are not guaranteed after cleaning, so
    # check the peptide bond geometry instead of numbering.
    keep_set = set(keep)
    kept_lines = []
    for ln in chain_lines:
        if _residue_key(ln) in keep_set and not (strip_hydrogens and _is_hydrogen(ln)):
            kept_lines.append(ln)
    break_warning = _check_backbone_continuity(kept_lines, keep)
    if break_warning:
        result["warnings"].append(break_warning)

    out = Path(output_file) if output_file else src.with_name(f"{src.stem}_{spec.label.replace(':', '_')}_tripeptide.pdb")
    out.parent.mkdir(parents=True, exist_ok=True)
    body = []
    for serial, ln in enumerate(kept_lines, start=1):
        body.append(f"ATOM  {serial:5d}{ln[11:]}")
    residues = [f"{k[0].strip()}:{_resname_of(kept_lines, k)}{k[1]}{k[2]}" for k in keep]
    out.write_text("\n".join([
        f"REMARK   1 MDCLAW extract_tripeptide from {src.name} around {spec.label} (flank={flank})",
        "REMARK   1 uncapped fragment: run prepare_complex --cap-termini before solvation",
        *body, "TER", "END", "",
    ]))
    result.update({
        "success": True,
        "tripeptide_pdb": str(out.resolve()),
        "mutation": spec.label,
        "residues": residues,
        "n_residues": len(keep),
        "n_atoms": len(kept_lines),
        "next": [
            "mdclaw bootstrap_md_workflow --study-dir <study> --job-id unfolded --question <same question>",
            f"mdclaw --job-dir <study>/jobs/unfolded --node-id <source_id> fetch_structure --source local --file-path {out}",
            "mdclaw --job-dir <study>/jobs/unfolded --node-id <prep_id> prepare_complex --cap-termini true "
            "<same protonation options as the folded leg>",
            f"... solvate_structure, then build_hybrid_system --mutation {spec.label} with the folded leg's options",
        ],
    })
    return result


def _resname_of(lines: list[str], key: tuple[str, int, str]) -> str:
    for ln in lines:
        if _residue_key(ln) == key:
            return ln[17:20].strip()
    return "UNK"


def _check_backbone_continuity(lines: list[str], keep: list[tuple[str, int, str]]) -> Optional[str]:
    """Warn when consecutive kept residues are not peptide-bonded (C–N > 2 Å)."""
    import math

    coords: dict[tuple, dict[str, tuple[float, float, float]]] = {}
    for ln in lines:
        name = ln[12:16].strip()
        if name in ("C", "N"):
            coords.setdefault(_residue_key(ln), {})[name] = (float(ln[30:38]), float(ln[38:46]), float(ln[46:54]))
    broken = []
    for a, b in zip(keep, keep[1:]):
        c = coords.get(a, {}).get("C")
        n = coords.get(b, {}).get("N")
        if c is None or n is None:
            continue
        if math.dist(c, n) > 2.0:
            broken.append(f"{a[1]}{a[2]}-{b[1]}{b[2]}")
    if broken:
        return f"chain break inside the fragment between residues {', '.join(broken)}; the unfolded model is not a single peptide"
    return None


__all__ = ["extract_tripeptide"]
