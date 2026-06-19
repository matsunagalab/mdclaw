"""
Structure Server - PDB retrieval and structure cleaning tools.

Provides tools for:
- Automatic retrieval of structure files from PDB/AlphaFold/PDB-REDO (prefers mmCIF)
- Chain separation and classification using gemmi
- Structure cleaning, missing residue modeling, water/heterogen removal, and protonation using PDBFixer
- Automatic detection of disulfide bonds and CYS->CYX renaming
- Mutation modeling with HPacker
- Ligand chemistry preparation with SMILES/SDF template matching
- LLM-friendly structure validation and error reporting at each step
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import List, Optional, Dict, Any  # noqa: E402

from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,
    create_unique_subdir,
    ensure_directory,
)

# Default working directory for prepare_complex when output_dir is not specified
WORKING_DIR = Path(".")
PDB_CHAIN_ID_POOL = (
    list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + list("abcdefghijklmnopqrstuvwxyz")
    + list("0123456789")
)
_DEUTERIUM_FALLBACK_ATOM_NAME_RE = re.compile(r"^D[0-9]*$")
DEFAULT_TERMINAL_CAP_FORCEFIELD = "ff19SB"
SUPPORTED_N_TERMINAL_CAPS = {"ACE"}
SUPPORTED_C_TERMINAL_CAPS = {"NME"}
TERMINAL_CAP_RESIDUES = SUPPORTED_N_TERMINAL_CAPS | SUPPORTED_C_TERMINAL_CAPS
SUPPORTED_PREP_SOLVENT_TYPES = {"explicit", "implicit", "vacuum"}

# Initialize tool wrappers
pdb2pqr_wrapper = BaseToolWrapper("pdb2pqr")
pdb4amber_wrapper = BaseToolWrapper("pdb4amber")


_PHOSPHO_TARGETS = {
    "SEP": {"source": "SER", "hydroxyl_h": "HG", "ester_o": "OG", "parent_c": "CB"},
    "TPO": {"source": "THR", "hydroxyl_h": "HG1", "ester_o": "OG1", "parent_c": "CB"},
    "PTR": {"source": "TYR", "hydroxyl_h": "HH", "ester_o": "OH", "parent_c": "CZ"},
}


def _compute_phospho_atom_coords(
    parent_c_xyz: tuple[float, float, float],
    ester_o_xyz: tuple[float, float, float],
    *,
    p_o_ester_bond: float = 1.60,
    p_o_terminal_bond: float = 1.50,
    o_h_bond: float = 0.97,
) -> dict[str, tuple[float, float, float]]:
    """Place the phosphate atoms on a tetrahedral phosphorus.

    Geometry (Amber dianion convention; SEP / TPO / PTR all share the
    same skeleton):

    - ``P`` sits along the parent_C → ester_O direction, extended by
      ``p_o_ester_bond`` past the ester oxygen.
    - The three terminal oxygens (``O1P`` / ``O2P`` / ``O3P``) ring P
      tetrahedrally so each forms a ~109.5° angle with the P-OG / P-OG1
      / P-OH bond. They are evenly spaced 120° around the C-O axis with
      arbitrary phase (downstream eq/min relaxes the orientation).
    - ``HOP2`` / ``HOP3`` are written as protons on ``O2P`` / ``O3P``
      with the H pointing radially outward from P. Pablo's CCD entries
      for SEP / TPO / PTR ship the *protonated* (singly-anion or
      neutral) form and refuse to match unless these hydrogens are
      present; the topology builder strips them again with
      ``Modeller.delete`` after Pablo loads so Amber's dianion phosaa
      templates apply.
    """
    import math

    cx, cy, cz = parent_c_xyz
    ox, oy, oz = ester_o_xyz
    vx, vy, vz = ox - cx, oy - cy, oz - cz
    norm = math.sqrt(vx * vx + vy * vy + vz * vz) or 1.0
    ux, uy, uz = vx / norm, vy / norm, vz / norm

    px = ox + p_o_ester_bond * ux
    py = oy + p_o_ester_bond * uy
    pz = oz + p_o_ester_bond * uz

    if abs(ux) < 0.9:
        rx, ry, rz = 1.0, 0.0, 0.0
    else:
        rx, ry, rz = 0.0, 1.0, 0.0
    dot = rx * ux + ry * uy + rz * uz
    e1x = rx - dot * ux
    e1y = ry - dot * uy
    e1z = rz - dot * uz
    n = math.sqrt(e1x * e1x + e1y * e1y + e1z * e1z) or 1.0
    e1x, e1y, e1z = e1x / n, e1y / n, e1z / n
    e2x = uy * e1z - uz * e1y
    e2y = uz * e1x - ux * e1z
    e2z = ux * e1y - uy * e1x

    cos_t = 1.0 / 3.0
    sin_t = math.sqrt(8.0) / 3.0

    out: dict[str, tuple[float, float, float]] = {}
    op_data: list[tuple[str, tuple[float, float, float], tuple[float, float, float]]] = []
    for label, phi in (("O1P", 0.0), ("O2P", 2 * math.pi / 3), ("O3P", 4 * math.pi / 3)):
        c, s = math.cos(phi), math.sin(phi)
        dx = cos_t * ux + sin_t * (c * e1x + s * e2x)
        dy = cos_t * uy + sin_t * (c * e1y + s * e2y)
        dz = cos_t * uz + sin_t * (c * e1z + s * e2z)
        op_xyz = (
            px + p_o_terminal_bond * dx,
            py + p_o_terminal_bond * dy,
            pz + p_o_terminal_bond * dz,
        )
        out[label] = op_xyz
        op_data.append((label, op_xyz, (dx, dy, dz)))
    out["P"] = (px, py, pz)
    # Protons placed along the P→O direction, extended by ``o_h_bond`` past
    # each terminal oxygen. Direction-only — bond / angle relax in eq/min.
    for label, (ox_p, oy_p, oz_p), (dx, dy, dz) in op_data:
        # ``O2P`` → ``HOP2`` etc. The Pablo CCD entries name the proton
        # ``HOP{n}`` rather than ``HO{n}P``.
        h_label = "HOP" + label[1]
        out[h_label] = (
            ox_p + o_h_bond * dx,
            oy_p + o_h_bond * dy,
            oz_p + o_h_bond * dz,
        )
    return out


def _build_source_to_merged_chain_map(
    chain_file_info: list[dict],
    proteins: list[dict],
    merge_chain_mapping: dict,
) -> dict:
    """Build the ``source_author_chain -> merged_chain`` composite map.

    Three pieces are joined on file path:

    - ``chain_file_info`` (from ``split_molecules``) gives ``chain_id`` (the
      label_asym_id used internally) plus the **full** ``author_chain``
      (auth_asym_id, possibly multi-letter on mmCIF inputs like ``"BBB"``).
    - ``proteins`` (from ``prepare_complex``) maps ``chain_id`` to the
      cleaned ``output_file`` that ``merge_structures`` actually consumed.
    - ``merge_chain_mapping`` (from ``merge_structures``) maps
      ``cleaned_file_path -> {1char_in_split: merged_chain}``. The 1-char
      key is what ``split_molecules`` wrote into the PDB (PDB format only
      has a 1-character chain column, so multi-letter authors get
      truncated); we don't use the key directly because ``split_molecules``
      emits one chain per file so the dict has exactly one entry whose
      value is what we want.

    The result is keyed by the **full** source author chain so that PTMs
    coming out of ``detect_ptm_sites`` (which records the source chain as
    gemmi sees it on the source structure — multi-letter for mmCIF) line
    up directly with the merged chain id without a brittle truncate-and-
    pray step.
    """
    chain_id_to_author: dict[str, str] = {}
    for info in chain_file_info or []:
        cid = info.get("chain_id")
        if cid is not None:
            chain_id_to_author[cid] = info.get("author_chain", cid)

    composite: dict[str, str] = {}
    for p in proteins or []:
        if not p.get("success"):
            continue
        cleaned_file = p.get("output_file")
        cid = p.get("chain_id")
        if not cleaned_file or cid is None:
            continue
        author = chain_id_to_author.get(cid, cid)
        per_file = (merge_chain_mapping or {}).get(cleaned_file) or {}
        if not per_file:
            continue
        # split_molecules emits one chain per cleaned file, so the per-file
        # mapping has exactly one entry. Take its value (the merged id).
        merged_id = next(iter(per_file.values()))
        composite[author] = merged_id
    return composite


def _remap_detected_ptm_chains(
    detected_ptm_residues: list[dict],
    composite_chain_map: dict,
) -> tuple[list[dict], list[dict]]:
    """Apply a pre-built ``source_author_chain -> merged_chain`` map to PTM
    detection results.

    The composite map is built by :func:`_build_source_to_merged_chain_map`
    inside ``prepare_complex`` because the join needs three sources of
    information (split's chain_file_info, prepare_complex's proteins[],
    and merge's chain_mapping). Splitting the helpers keeps this one
    trivially testable in isolation.

    Args:
        detected_ptm_residues: list of ``{"chain","resnum","name"}`` from
            ``detect_ptm_sites`` — ``chain`` is the **source author chain**
            (full, possibly multi-letter on mmCIF inputs).
        composite_chain_map: ``{source_author_chain: merged_chain}``.

    Returns:
        ``(remapped, dropped)``. Each remapped entry carries:
            - ``chain``: the merged.pdb chain id (what
              ``phosphorylate_residues`` actually looks up).
            - ``original_chain``: the source author chain (provenance).
            - ``resnum`` / ``name``: unchanged.
        ``dropped`` collects entries whose source chain has no entry in
        the composite map (typically excluded by ``select_chains``).
    """
    remapped: list[dict] = []
    dropped: list[dict] = []
    for ptm in detected_ptm_residues or []:
        original = ptm["chain"]
        merged_chain = (composite_chain_map or {}).get(original)
        if merged_chain is None:
            dropped.append(dict(ptm))
            continue
        remapped.append({
            "chain": merged_chain,
            "original_chain": original,
            "resnum": ptm["resnum"],
            "name": ptm["name"],
        })
    return remapped, dropped


def _remap_disulfide_chains(
    disulfide_bonds: list[dict], composite_chain_map: dict
) -> list[dict]:
    """Remap ``cys1``/``cys2`` chain ids of each disulfide pair from source to
    merged chain ids (via the same map as PTMs). ``_reconcile_cyx_cys_in_pdb``
    keys on (chain, resnum) against the merged pdb, so without this a chain
    reassignment makes it promote/demote the wrong CYS. Pairs are returned
    mutated in place; an unmapped chain is left as-is."""
    for bond in disulfide_bonds or []:
        for key in ("cys1", "cys2"):
            cys = bond.get(key)
            if isinstance(cys, dict):
                merged = (composite_chain_map or {}).get(cys.get("chain"))
                if merged is not None and merged != cys.get("chain"):
                    cys["original_chain"] = cys.get("chain")
                    cys["chain"] = merged
    return disulfide_bonds


def _remap_protonation_state_chains(
    protonation_states: list[dict], composite_chain_map: dict
) -> list[dict]:
    """Remap the ``chain`` of each ``{chain,resnum,state}`` entry from source to
    merged chain ids, so the reported summary matches merged.pdb."""
    for entry in protonation_states or []:
        if isinstance(entry, dict):
            merged = (composite_chain_map or {}).get(entry.get("chain"))
            if merged is not None and merged != entry.get("chain"):
                entry["original_chain"] = entry.get("chain")
                entry["chain"] = merged
    return protonation_states


def _remap_histidine_state_chains(
    histidine_states: dict, composite_chain_map: dict
) -> dict:
    """Remap ``"chain:resnum"`` keys of a histidine-state dict from source to
    merged chain ids."""
    if not histidine_states:
        return histidine_states
    out: dict = {}
    for key, state in histidine_states.items():
        chain, sep, rest = str(key).partition(":")
        merged = (composite_chain_map or {}).get(chain)
        out[f"{merged}:{rest}" if (merged and sep) else key] = state
    return out


def _parse_sites_str(sites_str: str) -> list[dict]:
    """Parse "A:65:SEP,A:178:TPO" into a list of site dicts."""
    out: list[dict] = []
    for chunk in sites_str.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"Invalid site entry '{chunk}': expected 'CHAIN:RESNUM:TARGET'"
            )
        chain, resnum_s, target = parts
        try:
            resnum = int(resnum_s)
        except ValueError as e:
            raise ValueError(
                f"Invalid resnum in site '{chunk}': '{resnum_s}'"
            ) from e
        out.append({"chain": chain.strip(), "resnum": resnum, "target": target.strip().upper()})
    return out


def _apply_phosphorylation_to_pdb(
    in_path: Path,
    out_path: Path,
    sites: list[dict],
) -> dict:
    """Rename target residues to SEP/TPO/PTR and strip hydroxyl hydrogens.

    Operates on standard PDB format (cols 18-20 = resName, col 22 = chainID,
    cols 23-26 = resSeq, cols 13-16 = atom name). `clean_protein` always
    emits standard PDB so single-character chain IDs are guaranteed.

    Returns a dict with:
        applied: list of fully-applied sites (chain, resnum, target, source)
        mismatch: list of sites whose current residue did not match the
                  expected source residue for the requested target
        not_found: list of sites whose (chain, resnum) was not found in the PDB
    """
    site_map: dict[tuple, str] = {}
    for s in sites:
        site_map[(s["chain"], int(s["resnum"]))] = s["target"]

    # First pass: gather parent_C and ester_O coordinates per target site so
    # we can synthesise phosphate-atom positions before the residue closes.
    site_geometry: dict[tuple, dict[str, tuple[float, float, float]]] = {}
    with in_path.open() as fin:
        for line in fin:
            if not line.startswith(("ATOM  ", "HETATM")):
                continue
            resname = line[17:20].strip()
            chain = line[21:22].strip()
            try:
                resnum = int(line[22:26].strip())
            except ValueError:
                continue
            key = (chain, resnum)
            if key not in site_map:
                continue
            spec = _PHOSPHO_TARGETS.get(site_map[key])
            if spec is None:
                continue
            atom_name = line[12:16].strip()
            if atom_name not in (spec["parent_c"], spec["ester_o"]):
                continue
            try:
                xyz = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
            except ValueError:
                continue
            entry = site_geometry.setdefault(key, {})
            if atom_name == spec["parent_c"]:
                entry["parent_c"] = xyz
            else:
                entry["ester_o"] = xyz

    seen: dict[tuple, str] = {}
    mismatch: list[dict] = []
    last_serial = 0
    pending_phospho_lines: list[str] = []
    current_residue_key: Optional[tuple] = None

    def _emit_phospho_atoms(
        key: tuple, target: str, last_template_line: str
    ) -> list[str]:
        """Build P / O1P / O2P / O3P / HOP2 / HOP3 ATOM records.

        Pablo's CCD ships SEP / TPO / PTR in the protonated form; we
        emit ``HOP2`` and ``HOP3`` (placed by ``_compute_phospho_atom_coords``)
        so Pablo's residue match succeeds. ``build_amber_system`` strips
        these protons after Pablo loads so the dianion phosaa templates
        used by ``protein.ff*.xml`` apply.
        """
        nonlocal last_serial
        geom = site_geometry.get(key, {})
        parent_c = geom.get("parent_c")
        ester_o = geom.get("ester_o")
        if not parent_c or not ester_o:
            return []
        coords = _compute_phospho_atom_coords(parent_c, ester_o)
        chain_field = last_template_line[21:22]
        resnum_field = last_template_line[22:26]
        icode_field = last_template_line[26:27]
        out_lines: list[str] = []
        for atom_name in ("P", "O1P", "O2P", "O3P", "HOP2", "HOP3"):
            x, y, z = coords[atom_name]
            element = "H" if atom_name.startswith("H") else atom_name[0]
            atom_field = f"{atom_name:>4}" if len(atom_name) < 4 else atom_name[:4]
            last_serial += 1
            out_lines.append(
                f"ATOM  {last_serial:>5} {atom_field} {target:>3} {chain_field}"
                f"{resnum_field}{icode_field}   "
                f"{x:>8.3f}{y:>8.3f}{z:>8.3f}  1.00  0.00          {element:>2}\n"
            )
        return out_lines

    with in_path.open() as fin, out_path.open("w") as fout:
        for line in fin:
            if line.startswith(("ATOM  ", "HETATM")):
                resname = line[17:20].strip()
                chain = line[21:22].strip()
                resnum_field = line[22:26].strip()
                try:
                    resnum = int(resnum_field)
                except ValueError:
                    if pending_phospho_lines:
                        for pl in pending_phospho_lines:
                            fout.write(pl)
                        pending_phospho_lines = []
                    fout.write(line)
                    continue
                try:
                    last_serial = max(last_serial, int(line[6:11].strip()))
                except ValueError:
                    pass
                atom_name = line[12:16].strip()
                key = (chain, resnum)

                if current_residue_key is not None and current_residue_key != key:
                    for pl in pending_phospho_lines:
                        fout.write(pl)
                    pending_phospho_lines = []
                current_residue_key = key

                if key in site_map:
                    target = site_map[key]
                    spec = _PHOSPHO_TARGETS.get(target)
                    if spec is None:
                        fout.write(line)
                        continue
                    expected_source = spec["source"]
                    if resname != expected_source:
                        if key not in seen:
                            mismatch.append({
                                "chain": chain,
                                "resnum": resnum,
                                "expected": expected_source,
                                "actual": resname,
                                "target": target,
                            })
                            seen[key] = "mismatch"
                        fout.write(line)
                        continue
                    if seen.get(key) != target:
                        seen[key] = target
                        # Queue phospho atoms to flush right after the
                        # last source atom — keeps the residue contiguous
                        # so PDBFile / Pablo treat them as one residue.
                        pending_phospho_lines = _emit_phospho_atoms(key, target, line)
                    if atom_name == spec["hydroxyl_h"]:
                        # Drop the original hydroxyl hydrogen — Amber's
                        # phosaa XMLs assume the dianion form (no H on the
                        # phosphate oxygens). The phosphate atoms we
                        # synthesised replace it.
                        continue
                    new_line = line[:17] + f"{target:>3}" + line[20:]
                    fout.write(new_line)
                    continue
            else:
                if pending_phospho_lines:
                    for pl in pending_phospho_lines:
                        fout.write(pl)
                    pending_phospho_lines = []
                current_residue_key = None
            fout.write(line)
        if pending_phospho_lines:
            for pl in pending_phospho_lines:
                fout.write(pl)

    applied = [
        {
            "chain": chain,
            "resnum": resnum,
            "target": target,
            "source": _PHOSPHO_TARGETS[target]["source"],
        }
        for (chain, resnum), target in seen.items()
        if target != "mismatch"
    ]
    not_found = [
        {"chain": chain, "resnum": resnum, "target": tgt}
        for (chain, resnum), tgt in site_map.items()
        if (chain, resnum) not in seen
    ]
    return {"applied": applied, "mismatch": mismatch, "not_found": not_found}


def phosphorylate_residues(
    pdb_file: Optional[str] = None,
    sites: Optional[List[Dict[str, Any]]] = None,
    sites_str: Optional[str] = None,
    restore_from_detection: bool = False,
    allow_partial: bool = False,
    name: Optional[str] = None,
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Apply phosphorylation (SER→SEP / THR→TPO / TYR→PTR) to a *cleaned* PDB.

    Phosphorylation is a post-prep transformation that runs on a branched
    ``prep`` node (parallels ``create_mutated_structure``). The DAG shape::

        source_001 → prep_001 (prepare_complex) → prep_002 (this tool)
                                                 → solv_001 → ...

    Ordering with other residue edits: when a residue needs both a PTM and a
    mutation, phosphorylate FIRST, then mutate — the order is
    ``prepare_complex`` (protonation + PTM detection) -> ``phosphorylate_residues``
    (branched from the prepare_complex node) -> ``create_mutated_structure``.
    Phosphorylation requires the residue to still be its standard base (SER/THR/
    TYR); a prior mutation changes it and this tool then refuses with a mismatch
    error. Protonation is owned by ``prepare_complex`` and applies to the final
    residue identities, so it precedes both.

    Three input modes (mutually exclusive):

    - ``restore_from_detection=True`` — reads
      ``metadata.detected_ptm_residues`` from the nearest prep ancestor
      (recorded by ``prepare_complex``) and re-introduces the same set of
      sites. Use when the source PDB carried PTMs and you want them back
      after PDBFixer's standard nonstandard-residue replacement.
    - ``sites=[{"chain":"A","resnum":65,"target":"SEP"}, ...]`` — explicit list.
    - ``sites_str="A:65:SEP,A:178:TPO"`` — CLI sugar for the same.

    Each site's *current* residue (in ``merged_pdb``) must be the standard
    counterpart of the requested target (``SEP`` requires ``SER`` etc.).
    The tool renames the residue and strips the hydroxyl hydrogen
    (``HG`` / ``HG1`` / ``HH``); ``OG`` / ``OG1`` / ``OH`` is kept as the
    phosphate linkage atom. ``build_amber_system`` then routes the matching
    openmmforcefields phosaa XML — ``amber/phosaa19SB.xml`` (ff19SB),
    ``amber/phosaa14SB.xml`` (ff14SB), ``amber/phosaa10.xml`` (ff03 /
    ff99SB legacy), ``amber/phosfb18.xml`` (fb15) — into the
    ``SystemGenerator`` ForceField bundle so the phosphate atoms get
    rebuilt by the OpenMM ForceField residue template (no tleap
    source step is involved).

    Args:
        pdb_file: Cleaned PDB (output of ``prepare_complex``). Required
                  unless running in node mode with a resolvable prep ancestor.
        sites: Explicit site list. See docstring head.
        sites_str: CLI sugar. See docstring head.
        restore_from_detection: Use sites recorded by ``prepare_complex``.
        allow_partial: When ``False`` (the default), any requested site that
                  is not located in the input PDB makes the call fail. This
                  catches typos in ``--sites-str`` and chain-remap drift in
                  ``--restore-from-detection``. Set ``True`` only if you
                  knowingly want to apply whichever subset is present.
        name: Optional name prefix for output files (e.g. "p_a65_a178").
        output_dir: Output directory (ignored in node mode).
        job_dir: DAG job directory (node mode).
        node_id: Node ID; expected ``node_type=prep`` with a prep parent.

    Returns:
        Dict with success / output_path / applied_sites / errors / warnings.
    """
    result = {
        "success": False,
        "output_dir": None,
        "output_path": None,
        "applied_sites": [],
        "errors": [],
        "warnings": [],
    }

    # Mutual exclusivity check
    explicit_modes = sum(
        1 for v in (sites, sites_str, restore_from_detection) if v
    )
    if explicit_modes != 1:
        result["errors"].append(
            "Provide exactly one of: --sites (JSON list), --sites-str "
            "('CHAIN:RESNUM:TARGET,...'), or --restore-from-detection."
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                result,
                default_error="phosphorylate_residues site mode invalid",
            )
        return result

    if job_dir and node_id:
        from mdclaw._node import validate_node_execution_context
        _ctx = validate_node_execution_context(
            job_dir,
            node_id,
            "prep",
            actual_conditions={
                "restore_from_detection": restore_from_detection,
                "explicit_sites": bool(sites or sites_str),
                "name": name,
            },
        )
        if not _ctx["success"]:
            blocked = {"success": False, "error_type": "ValidationError", **_ctx}
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="phosphorylate_residues node execution context invalid",
            )

    # Resolve site list
    resolved_sites: list[dict] = []
    if restore_from_detection:
        if not (job_dir and node_id):
            result["errors"].append(
                "--restore-from-detection requires --job-dir and --node-id."
            )
            return result
        from mdclaw._node import find_ancestor_metadata
        detected = find_ancestor_metadata(
            job_dir, node_id, "prep", "detected_ptm_residues"
        )
        if not detected:
            result["errors"].append(
                "No detected_ptm_residues metadata on any prep ancestor. "
                "Was prepare_complex run on a structure with SEP/TPO/PTR?"
            )
            return result
        for d in detected:
            resolved_sites.append({
                "chain": d["chain"],
                "resnum": int(d["resnum"]),
                "target": d["name"],
            })
    elif sites_str:
        try:
            resolved_sites = _parse_sites_str(sites_str)
        except ValueError as e:
            result["errors"].append(str(e))
            return result
    else:
        for s in sites or []:
            try:
                resolved_sites.append({
                    "chain": s["chain"],
                    "resnum": int(s["resnum"]),
                    "target": s.get("target", s.get("name", "")).upper(),
                })
            except (KeyError, TypeError, ValueError) as e:
                result["errors"].append(
                    f"Invalid site entry {s!r}: {type(e).__name__}: {e}"
                )
                return result

    if not resolved_sites:
        result["errors"].append("Resolved site list is empty.")
        return result

    invalid = [s for s in resolved_sites if s["target"] not in _PHOSPHO_TARGETS]
    if invalid:
        result["errors"].append(
            f"Unsupported target residue(s): {invalid}. "
            f"Supported: {sorted(_PHOSPHO_TARGETS)}."
        )
        return result

    # Auto-resolve input from nearest prep ancestor
    if job_dir and node_id and not pdb_file:
        from mdclaw._node import find_ancestor_artifact
        v = find_ancestor_artifact(job_dir, node_id, "prep", "merged_pdb")
        if v:
            pdb_file = v

    if not pdb_file:
        result["errors"].append(
            "pdb_file is required (or pass --job-dir/--node-id with a prep "
            "ancestor that provides a merged_pdb artifact)."
        )
        return result

    pdb_path = Path(pdb_file).resolve()
    if not pdb_path.is_file():
        result["errors"].append(f"Input PDB file not found: {pdb_file}")
        return result

    _node_mode = bool(job_dir and node_id)
    if _node_mode:
        from mdclaw._node import begin_node
        base_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        base_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    elif output_dir:
        base_dir = Path(output_dir)
    else:
        base_dir = create_unique_subdir(WORKING_DIR, "phospho")
    ensure_directory(base_dir)

    pref = f"{name}_" if name else ""
    output_path = (base_dir / f"{pref}phosphorylated.pdb").resolve()

    edit_result = _apply_phosphorylation_to_pdb(
        pdb_path, output_path, resolved_sites
    )

    if edit_result["mismatch"]:
        result["errors"].append(
            "Residue/target mismatch — refusing to write a partial result. "
            f"Details: {edit_result['mismatch']}"
        )
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass
        if _node_mode:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    if edit_result["not_found"]:
        message = (
            "The following sites were not located in the input PDB: "
            f"{edit_result['not_found']}. Common causes: a typo in "
            "--sites-str, or a chain-id mismatch between the cleaned merged "
            "PDB and the detection list (re-run prepare_complex if its "
            "chain remapping was missing)."
        )
        if allow_partial:
            result["warnings"].append(message + " Proceeding because allow_partial=True.")
        else:
            result["errors"].append(
                message + " Pass --allow-partial to apply the rest anyway."
            )
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass
            if _node_mode:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=result["errors"])
            return result

    if not edit_result["applied"]:
        result["errors"].append(
            "No sites were applied (input PDB did not contain any of the "
            "requested residues)."
        )
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass
        if _node_mode:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    result["success"] = True
    result["output_dir"] = str(base_dir)
    result["output_path"] = str(output_path)
    result["applied_sites"] = edit_result["applied"]
    logger.info(
        "Phosphorylation applied: %s sites -> %s",
        len(edit_result["applied"]),
        output_path,
    )

    if _node_mode:
        from mdclaw._node import complete_node
        rel_out = f"artifacts/{output_path.name}"
        ptm_residues_meta = [
            {
                "chain": s["chain"],
                "resnum": s["resnum"],
                "name": s["target"],
                "source": "detected" if restore_from_detection else "introduced",
            }
            for s in edit_result["applied"]
        ]
        complete_node(
            job_dir, node_id,
            artifacts={
                "merged_pdb": rel_out,
                "phosphorylated_pdb": rel_out,
            },
            metadata={
                "name": name,
                "phosphorylation_source_pdb": str(pdb_path),
                "ptm_residues": ptm_residues_meta,
                "restore_from_detection": restore_from_detection,
            },
            warnings=result.get("warnings", []),
        )

    return result
