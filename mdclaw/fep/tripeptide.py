"""``extract_tripeptide`` — the unfolded-state model as a ``prep`` node.

Folding-stability ddG needs two alchemical legs of the *same* mutation: the
folded protein and an unfolded-state model. Following pmx, the unfolded
model is the capped peptide ACE-X(i-flank)..X(i)..X(i+flank)-NME cut from the
prepared protein. This tool is a ``prep`` stage tool whose parent is the
protein's ``prep`` node (``prep -> prep``, like ``create_mutated_structure``)::

    source_001 -> prep_001 (prepare_complex) -> solv -> topo(hybrid) -> ... folded leg
                        └-> prep_002 (this tool)   -> solv -> topo(hybrid) -> ... unfolded leg

Both legs therefore share one source, one preparation (protonation states,
disulfides, numbering) and one ``--mutation`` string, and the ddG node
(``estimate_ddg``) can consume both legs' analyses inside one job.

The fragment keeps the parent's chain id, residue numbers and protonation
variants (``preserve_input_protonation``); ``clean_protein`` adds the ACE/NME
caps and completes their hydrogens. The node completes with the artifacts the
``solv`` / ``topo`` resolvers read from a prep node (``merged_pdb``,
``chain_identity_map``, ``disulfide_bonds``) and marks itself
``leg_role = "unfolded"`` so ``estimate_ddg`` and the envelope can tell the
legs apart without user input.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from mdclaw._common import create_unique_subdir
from mdclaw._tool_meta import node_tool
from mdclaw.fep.mutant import MutantBuildError, MutationSpec, parse_single_mutation
from mdclaw.sidechain_packer import _is_standard_protein_atom

WORKING_DIR = Path("outputs").resolve()
LEG_ROLE_UNFOLDED = "unfolded"
_DISULFIDE_RESNAMES = {"CYX", "CYM"}


class TripeptideError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _residue_key(line: str) -> tuple[str, int, str]:
    return line[21], int(line[22:26]), line[26].strip()


def _resname_of(lines: list[str], key: tuple[str, int, str]) -> str:
    for ln in lines:
        if _residue_key(ln) == key:
            return ln[17:20].strip()
    return "UNK"


def cut_fragment(pdb_lines: list[str], spec: MutationSpec, flank: int) -> tuple[list[str], list[tuple[str, int, str]], list[str]]:
    """ATOM lines of residues ``i-flank .. i+flank`` around the mutation site.

    Returns ``(atom_lines, residue_keys, warnings)``; serials are renumbered
    from 1, everything else in each line is kept byte-for-byte.
    """
    protein = [ln for ln in pdb_lines if ln.startswith(("ATOM  ", "HETATM")) and _is_standard_protein_atom(ln)]
    chain = spec.chain_id or " "  # blank chain ids are stored as a space in PDB column 22
    chain_lines = [ln for ln in protein if ln[21] == chain]
    order: list[tuple[str, int, str]] = []
    for ln in chain_lines:
        key = _residue_key(ln)
        if not order or order[-1] != key:
            if key in order:
                raise TripeptideError(
                    code="fep_tripeptide_extraction_failed",
                    message=f"chain {spec.chain_id} lists residue {key[1]}{key[2]} in two places; "
                    "extract from a cleaned protein PDB")
            order.append(key)
    target = (chain, spec.resseq, spec.icode or "")
    if target not in order:
        raise TripeptideError(code="fep_mutation_residue_not_found",
                              message=f"{spec.label}: residue not found in chain {spec.chain_id}")
    idx = order.index(target)
    lo, hi = max(0, idx - flank), min(len(order) - 1, idx + flank)
    keep = order[lo:hi + 1]
    warnings: list[str] = []
    if lo > idx - flank or hi < idx + flank:
        warnings.append(
            f"{spec.label} sits {idx} residue(s) from the chain start / {len(order) - 1 - idx} from its end; "
            f"only {len(keep)} residues could be kept.")
    keep_set = set(keep)
    kept = [ln for ln in chain_lines if _residue_key(ln) in keep_set]
    # Gaps: consecutive residue numbers are not guaranteed after cleaning, so
    # check the peptide bond geometry instead of numbering.
    break_warning = _check_backbone_continuity(kept, keep)
    if break_warning:
        warnings.append(break_warning)
    resnames = {_resname_of(kept, k) for k in keep}
    if resnames & _DISULFIDE_RESNAMES:
        warnings.append(
            f"the fragment contains {sorted(resnames & _DISULFIDE_RESNAMES)} whose bonding partner lies outside it; "
            "the cleaned peptide carries the residue without that bond")
    atom_lines = [f"ATOM  {serial:5d}{ln[11:]}" for serial, ln in enumerate(kept, start=1)]
    return atom_lines, keep, warnings


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


def _chain_identity_map(pdb_path: Path, source_file: Path) -> dict:
    """The one-component ``mdclaw.chain_identity_map.v1`` record ``merge_structures``
    would write, with the fragment's own chain id kept (the merge tool relabels
    chains from A, which would break the shared ``--mutation`` spec)."""
    atoms = 0
    residues: list[tuple] = []
    chain_id = " "
    for ln in pdb_path.read_text().splitlines():
        if not ln.startswith(("ATOM  ", "HETATM")):
            continue
        atoms += 1
        chain_id = ln[21]
        key = _residue_key(ln)
        if not residues or residues[-1] != key:
            residues.append(key)
    chain = chain_id.strip() or "A"
    return {
        "schema_version": "mdclaw.chain_identity_map.v1",
        "identity_contract": "PDB chain IDs are MD compatibility labels and may be reused; component_id plus "
                             "topology_chain_index and atom/residue ranges are the canonical identities.",
        "pdb_chain_id_policy": "inherited_from_parent_prep",
        "pdb_chain_ids_may_repeat": False,
        "components": [{
            "component_id": "component_000001",
            "source_file": str(source_file),
            "source_chain_id": chain,
            "source_chain_index": 0,
            "topology_chain_index": 0,
            "md_chain_id": chain,
            "pdb_chain_id": chain,
            "atom_index_start": 0,
            "atom_index_end_exclusive": atoms,
            "atom_count": atoms,
            "residue_index_start": 0,
            "residue_index_end_exclusive": len(residues),
            "residue_count": len(residues),
        }],
    }


def _clean_fragment(fragment_pdb: Path, *, cap_termini: bool, terminal_cap_forcefield: Optional[str],
                    protonation_method: str, ph: float) -> dict:
    """``clean_protein`` on the fragment: caps + cap hydrogens, protonation
    variants of the parent preparation preserved."""
    from mdclaw.structure.clean_protein import clean_protein

    cleaned = clean_protein(
        pdb_file=str(fragment_pdb),
        cap_termini=cap_termini,
        terminal_cap_forcefield=terminal_cap_forcefield,
        protonation_method=protonation_method,
        preserve_input_protonation=True,
        ignore_terminal_missing_residues=True,
        remove_heterogens=True,
        ph=ph,
    )
    if not cleaned.get("success") or not cleaned.get("output_file"):
        raise TripeptideError(
            code="fep_tripeptide_cap_failed",
            message="clean_protein could not cap the fragment"
            + (f" ({cleaned.get('code')})" if cleaned.get("code") else "")
            + ": " + "; ".join(cleaned.get("errors") or [])[:400],
        )
    return cleaned


@node_tool(node_type="prep")
def extract_tripeptide(
    mutation: str,
    pdb_file: Optional[str] = None,
    flank: int = 1,
    cap_termini: bool = True,
    terminal_cap_forcefield: Optional[str] = None,
    protonation_method: str = "no-prediction",
    ph: float = 7.4,
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Prepare the unfolded-state model (capped peptide) as a ``prep`` node.

    Node mode: a ``prep`` node whose parent is the protein's completed
    ``prep`` node. The fragment ``i-flank .. i+flank`` around the mutation is
    cut from that parent's ``merged_pdb`` (chain id, residue numbers and
    protonation variants kept), capped with ACE/NME by ``clean_protein``, and
    registered as this node's ``merged_pdb`` so ``solvate_structure`` and
    ``build_hybrid_system`` follow exactly as on the folded leg with the same
    ``--mutation``. The node is marked ``leg_role = "unfolded"``.

    Args:
        mutation: ``A:L99A`` / ``L99A`` — the residue to centre on (the same
            string the folded leg uses).
        pdb_file: Prepared protein PDB (direct mode; resolved from the
            parent prep node's ``merged_pdb`` in node mode).
        flank: Residues kept on each side (1 gives the classic tripeptide).
        cap_termini: Add ACE / NME caps (default on; off leaves charged termini).
        terminal_cap_forcefield: Protein force field for cap-hydrogen
            completion (default ff19SB; pass the planned topology force field).
        protonation_method: ``"no-prediction"`` (default) keeps the parent
            preparation's protonation states so both legs share one chemistry;
            ``"propka"`` re-predicts them on the isolated peptide at ``ph``.
        ph: pH for ``propka`` (ignored by ``no-prediction``).
        output_dir / job_dir / node_id: standard mdclaw knobs.

    Returns:
        ``merged_pdb`` (capped peptide, the prep artifact), ``fragment_pdb``
        (uncapped cut), the residues kept, caps, ``leg_role`` and, on failure,
        ``code`` (``fep_fragment_prep_required``, ``fep_mutation_*``,
        ``fep_tripeptide_extraction_failed``, ``fep_tripeptide_cap_failed``).
    """
    from mdclaw._node import fail_tool

    result: dict = {"success": False, "tool": "extract_tripeptide", "mutation": mutation,
                    "errors": [], "warnings": []}
    node_mode = bool(job_dir and node_id)

    def _fail(code: str, message: str) -> dict:
        return fail_tool(result, code, message, job_dir=job_dir, node_id=node_id)

    # --- argument checks; the node stays pending on failure -------------------
    if not isinstance(flank, int) or flank < 0:
        return _fail(code="invalid_parameter_value", message=f"flank must be an integer >= 0, got {flank!r}")
    parent_prep_id = None
    if node_mode:
        from mdclaw._node import _find_ancestor_node_id, find_ancestor_artifact, validate_node_execution_context

        ctx = validate_node_execution_context(
            job_dir, node_id, "prep",
            actual_conditions={"mutation": mutation, "flank": flank, "cap_termini": cap_termini,
                               "terminal_cap_forcefield": terminal_cap_forcefield,
                               "protonation_method": protonation_method, "ph": ph},
        )
        if not ctx["success"]:
            from mdclaw._node import fail_node_from_result

            return fail_node_from_result(job_dir, node_id, {"success": False, "error_type": "ValidationError", **ctx},
                                         default_error="extract_tripeptide node execution context invalid")
        parent_prep_id = _find_ancestor_node_id(job_dir, node_id, "prep")
        resolved = find_ancestor_artifact(job_dir, node_id, "prep", "merged_pdb") if parent_prep_id else None
        if not resolved:
            return _fail(code="fep_fragment_prep_required",
                         message="extract_tripeptide needs a completed prep parent (prepare_complex) whose merged_pdb is "
                                 "the prepared protein; create this prep node with --parent-node-ids <that prep node>")
        if pdb_file and Path(pdb_file).resolve() != Path(resolved).resolve():
            return _fail(code="fep_fragment_prep_required",
                         message=f"pdb_file {pdb_file} differs from the parent prep's merged_pdb {resolved}; "
                                 "in node mode the fragment is cut from the DAG's prepared structure")
        pdb_file = resolved
    if not pdb_file:
        return _fail(code="missing_pdb_file", message="pdb_file is required (or --job-dir/--node-id under a completed prep node)")
    src = Path(pdb_file).resolve()
    if not src.is_file():
        return _fail(code="file_not_found", message=f"{pdb_file} not found")
    try:
        spec = parse_single_mutation(mutation, src)
    except MutantBuildError as exc:
        return _fail(exc.code, str(exc))
    try:
        atom_lines, keep, warnings = cut_fragment(src.read_text().splitlines(), spec, flank)
    except TripeptideError as exc:
        return _fail(exc.code, str(exc))
    result["warnings"].extend(warnings)
    result["mutation"] = spec.label

    # --- the node starts here -------------------------------------------------
    if node_mode:
        from mdclaw._node import begin_node

        out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        out_dir = create_unique_subdir(Path(output_dir) if output_dir else WORKING_DIR, "tripeptide").resolve()
    result["output_dir"] = str(out_dir)

    fragment_dir = out_dir / "fragment"
    fragment_dir.mkdir(exist_ok=True)
    fragment_pdb = fragment_dir / f"{spec.label.replace(':', '_')}_flank{flank}.pdb"
    fragment_pdb.write_text("\n".join([
        f"REMARK   1 MDCLAW extract_tripeptide from {src.name} around {spec.label} (flank={flank})",
        *atom_lines, "TER", "END", "",
    ]))
    residues = [f"{k[0].strip()}:{_resname_of(atom_lines, k)}{k[1]}{k[2]}" for k in keep]

    try:
        cleaned = _clean_fragment(fragment_pdb, cap_termini=cap_termini, terminal_cap_forcefield=terminal_cap_forcefield,
                                  protonation_method=protonation_method, ph=ph)
    except TripeptideError as exc:
        return _fail(exc.code, str(exc))
    # A cut fragment has no SEQRES by construction; that notice is noise here.
    result["warnings"].extend(w for w in (cleaned.get("warnings") or []) if "SEQRES" not in w)
    caps = {"n_terminal": cleaned.get("n_terminal_cap"), "c_terminal": cleaned.get("c_terminal_cap")}
    disulfide_bonds = list(cleaned.get("disulfide_bonds") or [])
    merge_dir = out_dir / "merge"
    merge_dir.mkdir(exist_ok=True)
    merged_pdb = merge_dir / "merged.pdb"
    shutil.copyfile(cleaned["output_file"], merged_pdb)

    chain_map = _chain_identity_map(merged_pdb, fragment_pdb)
    (out_dir / "chain_identity_map.json").write_text(json.dumps(chain_map, indent=2))
    (out_dir / "disulfide_bonds.json").write_text(json.dumps(disulfide_bonds, indent=2))
    n_atoms = sum(1 for ln in merged_pdb.read_text().splitlines() if ln.startswith(("ATOM  ", "HETATM")))

    unfolded_model = {
        "kind": "capped_peptide" if cap_termini else "peptide",
        "flank": flank,
        "residues": residues,
        "n_residues": len(keep),
        "caps": caps,
        "protonation_method": protonation_method,
        "preserve_input_protonation": True,
    }
    result.update({
        "success": True,
        "merged_pdb": str(merged_pdb),
        "tripeptide_pdb": str(merged_pdb),
        "fragment_pdb": str(fragment_pdb),
        "chain_identity_map": str(out_dir / "chain_identity_map.json"),
        "residues": residues,
        "n_residues": len(keep),
        "n_atoms": n_atoms,
        "caps": caps,
        "leg_role": LEG_ROLE_UNFOLDED,
        "unfolded_model": unfolded_model,
    })
    if node_mode:
        from mdclaw._node import complete_node

        complete_node(
            job_dir, node_id,
            artifacts={
                "merged_pdb": "artifacts/merge/merged.pdb",
                "fragment_pdb": f"artifacts/fragment/{fragment_pdb.name}",
                "chain_identity_map": "artifacts/chain_identity_map.json",
                "disulfide_bonds": "artifacts/disulfide_bonds.json",
            },
            metadata={
                "tool": "extract_tripeptide",
                "leg_role": LEG_ROLE_UNFOLDED,
                "mutation": spec.label,
                "derived_from_prep_node_id": parent_prep_id,
                "unfolded_model": unfolded_model,
                "n_terminal_cap": caps["n_terminal"],
                "c_terminal_cap": caps["c_terminal"],
                "terminal_cap_forcefield": cleaned.get("terminal_cap_forcefield"),
                "protonation_method": protonation_method,
                "preserve_input_protonation": True,
                "statistics": {"num_atoms": n_atoms, "num_residues": len(keep) + sum(1 for v in caps.values() if v)},
            },
            warnings=result["warnings"],
        )
    else:
        result["next"] = [
            "Inside a job: create a prep node under the protein's prep node and run this tool there:",
            "  mdclaw create_node --job-dir <job> --node-type prep --parent-node-ids <prep_001>",
            f"  mdclaw --job-dir <job> --node-id <prep_002> extract_tripeptide --mutation {spec.label}",
            "then solvate_structure -> build_hybrid_system (same --mutation and options as the folded leg) -> min -> eq -> run_fep -> analyze_fep.",
        ]
    return result


__all__ = ["LEG_ROLE_UNFOLDED", "TripeptideError", "cut_fragment", "extract_tripeptide"]
