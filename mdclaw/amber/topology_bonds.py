"""
Amber Server — curated Amber → OpenMM System builder.

Provides tools for:
- ``build_amber_system``: load a prepared PDB through OpenFF Pablo, apply Amber
  protein / nucleic / glycan / lipid / PTM force fields plus topology-time
  ligand templates (``GAFFTemplateGenerator``), and emit a portable
  ``system.xml`` +
  ``topology.pdb`` + ``state.xml`` triple consumed by ``run_minimization`` /
  ``run_equilibration`` / ``run_production``, plus a minimization report for
  benchmark evidence.
- Supporting both implicit (no PBC) and explicit (with PBC, optionally
  membrane) solvent setups.
- Handling protein-ligand complexes by consuming prep-stage
  ``ligand_chemistry`` records; topology parameterizes the small molecules
  with ``GAFFTemplateGenerator``.
- Handling glycoproteins by converting deposited glycan residues to
  Amber/GLYCAM notation at topology time, preserving the generated bond plan,
  and completing only GLYCAM-specific hydrogens before System creation.

The XML triple is the only topology contract on the run side; tleap and
parm7/rst7 are not produced or consumed anywhere. AmberTools
(``pdb4amber`` and ``cpptraj``) remain available for structure-preparation
support; ligand parameterization is not a prep-stage mdclaw artifact.
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

from pathlib import Path  # noqa: E402
from typing import List, Optional, Dict, Any, Tuple  # noqa: E402

from mdclaw._common import (  # noqa: E402
    ensure_directory, BaseToolWrapper,
)

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

# Initialize tool wrappers.
# ``tleap`` is no longer used: the curated build path runs through
# ``openmmforcefields.SystemGenerator`` and emits the modern
# ``system.xml`` + ``topology.pdb`` + ``state.xml`` triple (PR3 of the
# openmmforcefields-unification refactor). ``cpptraj`` is still used for
# the GLYCAM ``prepareforleap`` glycan conversion stage; see
# ``_prepare_glycam_pdb_with_cpptraj`` for context.
cpptraj_wrapper = BaseToolWrapper("cpptraj")


# =============================================================================
# Force Field Mappings (based on Amber Manual 2024 recommendations)
# =============================================================================


def _patch_template_internal_bonds(omm_topology: Any, forcefield: Any) -> int:
    bonds_added = 0
    existing_internal_bonds = {
        tuple(sorted((bond.atom1.index, bond.atom2.index)))
        for bond in omm_topology.bonds()
        if bond.atom1.residue.index == bond.atom2.residue.index
    }
    for residue in list(omm_topology.residues()):
        atom_by_name = {a.name: a for a in residue.atoms()}
        if not atom_by_name:
            continue
        template = forcefield._templates.get(residue.name)
        if template is None:
            continue
        for tb in template.bonds:
            n1 = template.atoms[tb[0]].name
            n2 = template.atoms[tb[1]].name
            a1 = atom_by_name.get(n1)
            a2 = atom_by_name.get(n2)
            if a1 is None or a2 is None:
                continue
            key = tuple(sorted((a1.index, a2.index)))
            if key in existing_internal_bonds:
                continue
            omm_topology.addBond(a1, a2)
            existing_internal_bonds.add(key)
            bonds_added += 1
    return bonds_added


def _topology_has_bond(omm_topology: Any, atom1: Any, atom2: Any) -> bool:
    key = tuple(sorted((atom1.index, atom2.index)))
    return any(
        tuple(sorted((bond.atom1.index, bond.atom2.index))) == key
        for bond in omm_topology.bonds()
    )


def _patch_ligand_molecule_internal_bonds(
    omm_topology: Any,
    ligand_records: list[Dict[str, Any]],
    ligand_molecules: list[Any],
) -> int:
    """Patch ligand bonds from OpenFF Molecule atom order."""
    bonds_added = 0
    existing_internal_bonds = {
        tuple(sorted((bond.atom1.index, bond.atom2.index)))
        for bond in omm_topology.bonds()
        if bond.atom1.residue.index == bond.atom2.residue.index
    }
    residues_by_name: dict[str, list[Any]] = {}
    for residue in omm_topology.residues():
        residues_by_name.setdefault(str(residue.name or "").upper(), []).append(residue)

    used_residue_indices: set[int] = set()
    for ligand_record, molecule in zip(ligand_records or [], ligand_molecules or []):
        residue_name = str(ligand_record.get("residue_name") or "").upper()
        if not residue_name:
            continue
        try:
            molecule_atom_count = int(molecule.n_atoms)
        except Exception:  # noqa: BLE001
            continue
        residue = next(
            (
                candidate for candidate in residues_by_name.get(residue_name, [])
                if candidate.index not in used_residue_indices
                and len(list(candidate.atoms())) == molecule_atom_count
            ),
            None,
        )
        if residue is None:
            continue
        used_residue_indices.add(residue.index)
        residue_atoms = list(residue.atoms())
        for bond in molecule.bonds:
            try:
                a1 = residue_atoms[int(bond.atom1_index)]
                a2 = residue_atoms[int(bond.atom2_index)]
            except (AttributeError, IndexError, TypeError, ValueError):
                continue
            key = tuple(sorted((a1.index, a2.index)))
            if key in existing_internal_bonds:
                continue
            omm_topology.addBond(a1, a2)
            existing_internal_bonds.add(key)
            bonds_added += 1
    return bonds_added


def _plan_disulfide_topology_bonds(
    pdb_path: Path,
    disulfide_pairs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Map disulfide pairs (from prepare_complex) onto unit sequential indices.

    The openmmforcefields build path adds the SG-SG bond directly to the
    OpenMM ``Topology`` after loading the prepared PDB through Pablo, so
    this helper just resolves which residues to wire together. Unit
    sequential indices (1-based, in PDB atom order) — historically the
    same numbering tleap used for ``bond mol.N.SG`` — are still the
    cleanest cross-reference because they survive ``loadpdb`` and Pablo
    alike for solvated PDBs where resSeq wraps.

    Resolution is done **per chain**: for each disulfide pair, every
    chain in the merged PDB that carries both resnums as ``CYX`` yields
    one resolved entry. The pair's ``chain`` field is advisory and
    ignored, because ``merge_structures`` renames chain IDs (A, B, C, …)
    while the pair's chain comes from the original pre-split structure —
    propagating the mapping is not worth the wiring cost when per-chain
    CYX presence is an equally reliable signal.

    Global de-duplication on ``frozenset({idx1, idx2})`` keeps the
    homodimer case (two legitimate disulfide_bonds.json entries listing
    the same pair under different chains) from double-bonding: the
    first entry emits one resolved row per matching chain, and later
    entries that resolve to the same indices are recorded as
    ``emitted_duplicate``.

    Returns a dict with:
        ``resolved``: list[dict] — per-pair provenance (``cys1``, ``cys2``,
            ``source``, ``topology_residues`` as ``[[idx1, idx2], …]`` —
            a list because one pair can match multiple chains —,
            ``status``: ``emitted``, ``emitted_duplicate``,
            ``skipped_cys_protonated``, or ``unresolved``).
        ``warnings``: list[str] — human-readable notes for non-emitted pairs.
    """
    plan: Dict[str, Any] = {"resolved": [], "warnings": []}
    if not disulfide_pairs:
        return plan

    # Walk the PDB once. ``unit_index`` counts every unique residue in PDB
    # order (1-based) — the openmmforcefields build path consumes this
    # index when calling ``Topology.addBond`` so it remains a stable
    # provenance handle even when PDB resSeq collides across chains.
    unit_index = 0
    by_chain: Dict[str, Dict[int, Dict[str, Any]]] = {}
    last_key: Optional[Tuple[str, int, str]] = None
    try:
        with open(pdb_path, "r") as fh:
            for line in fh:
                if not (line.startswith("ATOM") or line.startswith("HETATM")):
                    continue
                if len(line) < 26:
                    continue
                resname = line[17:20].strip()
                chain = line[21].strip()
                try:
                    resnum = int(line[22:26])
                except ValueError:
                    continue
                key = (chain, resnum, resname)
                if key == last_key:
                    continue
                last_key = key
                unit_index += 1
                # Only index CYS/CYX residues. Everything else is irrelevant
                # to disulfide bond resolution, and indexing water/ion
                # residues would silently overwrite protein entries in
                # solvated PDBs where PDB resSeq wraps and waters share
                # chain IDs with the protein (e.g. a water at chain A
                # resnum 22 clobbering the protein's CYX 22).
                if resname not in ("CYX", "CYS"):
                    continue
                by_chain.setdefault(chain, {})[resnum] = {
                    "resname": resname,
                    "unit_index": unit_index,
                }
    except OSError as e:
        plan["warnings"].append(
            f"Could not read PDB for disulfide bond mapping: {e}"
        )
        return plan

    emitted_pairs: set = set()  # frozenset({idx1, idx2}) already emitted

    for pair in disulfide_pairs:
        c1 = pair.get("cys1", {})
        c2 = pair.get("cys2", {})
        r1 = c1.get("resnum")
        r2 = c2.get("resnum")
        record = {
            "cys1": c1,
            "cys2": c2,
            "source": pair.get("source"),
            "topology_residues": None,
            "status": "unresolved",
        }

        matched: List[Tuple[str, int, int]] = []
        saw_cys_protonated = False
        for chain_id, residues in by_chain.items():
            if r1 not in residues or r2 not in residues:
                continue
            rn1 = residues[r1]["resname"]
            rn2 = residues[r2]["resname"]
            if rn1 not in ("CYX", "CYS") or rn2 not in ("CYX", "CYS"):
                continue
            if rn1 == "CYS" or rn2 == "CYS":
                saw_cys_protonated = True
                continue
            matched.append((
                chain_id,
                residues[r1]["unit_index"],
                residues[r2]["unit_index"],
            ))

        if not matched:
            if saw_cys_protonated:
                record["status"] = "skipped_cys_protonated"
                plan["warnings"].append(
                    f"Disulfide pair {r1}-{r2} skipped: one or both residues "
                    f"are CYS (protonated) in {pdb_path.name}; rename to CYX "
                    f"before building the system."
                )
            else:
                plan["warnings"].append(
                    f"Disulfide pair {r1}-{r2} skipped: residues not found "
                    f"as CYX in {pdb_path.name}"
                )
            plan["resolved"].append(record)
            continue

        emitted_indices: List[List[int]] = []
        for _chain_id, idx1, idx2 in matched:
            bond_key = frozenset({idx1, idx2})
            if bond_key in emitted_pairs:
                continue
            emitted_pairs.add(bond_key)
            emitted_indices.append([idx1, idx2])

        if emitted_indices:
            record["status"] = "emitted"
            record["topology_residues"] = emitted_indices
        else:
            # Every chain that matched was already covered by an earlier
            # pair — typical for the second entry of a homodimer listing.
            record["status"] = "emitted_duplicate"
        plan["resolved"].append(record)

    return plan


def _plan_glycan_topology_bonds(
    pdb_path: Path,
    glycan_linkages: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Map prepared protein-glycan linkages onto unit sequential indices.

    The openmmforcefields build path consumes ``topology_residues`` from
    the resolved entries to call ``Topology.addBond`` on the OpenMM
    topology after Pablo loading.
    """
    plan: Dict[str, Any] = {"resolved": [], "warnings": []}
    if not glycan_linkages:
        return plan

    unit_index = 0
    residues: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    last_key: Optional[Tuple[str, str, str, str]] = None
    try:
        with open(pdb_path, "r") as fh:
            for line in fh:
                if not line.startswith(("ATOM", "HETATM")) or len(line) < 27:
                    continue
                resname = line[17:20].strip()
                chain = line[21].strip() or "A"
                resnum = line[22:26].strip()
                icode = line[26].strip()
                key = (chain, resnum, icode, resname)
                if key != last_key:
                    unit_index += 1
                    residues[key] = {
                        "unit_index": unit_index,
                        "atoms": set(),
                    }
                    last_key = key
                residues[key]["atoms"].add(line[12:16].strip())
    except OSError as e:
        plan["warnings"].append(f"Could not read PDB for glycan linkage mapping: {e}")
        return plan

    emitted_pairs: set[frozenset[int]] = set()
    for linkage in glycan_linkages:
        protein = linkage.get("protein") or {}
        glycan = linkage.get("glycan") or {}
        protein_key = (
            str(protein.get("merged_chain") or protein.get("chain") or ""),
            str(protein.get("merged_resnum") or protein.get("resnum") or ""),
            str(protein.get("merged_icode") or protein.get("icode") or ""),
            str(protein.get("resname") or ""),
        )
        glycan_key = (
            str(glycan.get("merged_chain") or glycan.get("chain") or ""),
            str(glycan.get("merged_resnum") or glycan.get("resnum") or ""),
            str(glycan.get("merged_icode") or glycan.get("icode") or ""),
            str(glycan.get("resname") or ""),
        )
        record = {
            "protein": protein,
            "glycan": glycan,
            "source": linkage.get("source"),
            "topology_residues": None,
            "status": "unresolved",
        }
        protein_residue = residues.get(protein_key)
        glycan_residue = residues.get(glycan_key)
        protein_atom = str(protein.get("atom") or "")
        glycan_atom = str(glycan.get("atom") or "")
        if protein_residue is None or glycan_residue is None:
            plan["warnings"].append(
                f"Glycan linkage skipped: residue not found in {pdb_path.name}: "
                f"{protein_key} - {glycan_key}"
            )
            plan["resolved"].append(record)
            continue
        if protein_atom not in protein_residue["atoms"] or glycan_atom not in glycan_residue["atoms"]:
            plan["warnings"].append(
                f"Glycan linkage skipped: atom not found in {pdb_path.name}: "
                f"{protein_key}.{protein_atom} - {glycan_key}.{glycan_atom}"
            )
            plan["resolved"].append(record)
            continue
        idx1 = protein_residue["unit_index"]
        idx2 = glycan_residue["unit_index"]
        bond_key = frozenset({idx1, idx2})
        if bond_key in emitted_pairs:
            record["status"] = "emitted_duplicate"
            plan["resolved"].append(record)
            continue
        emitted_pairs.add(bond_key)
        record["status"] = "emitted"
        record["topology_residues"] = [[idx1, idx2]]
        plan["resolved"].append(record)

    return plan


def _format_pdb_link_line(
    atom1: str,
    resname1: str,
    chain1: str,
    resnum1: Any,
    icode1: str,
    atom2: str,
    resname2: str,
    chain2: str,
    resnum2: Any,
    icode2: str,
) -> str:
    """Format a minimal PDB LINK record for cpptraj prepareforleap."""
    return (
        f"LINK        {atom1[:4]:>4} {resname1[:3]:>3} {chain1[:1] or ' ':1}{str(resnum1)[:4]:>4}{(icode1 or ' ')[:1]:1}"
        f"               {atom2[:4]:>4} {resname2[:3]:>3} {chain2[:1] or ' ':1}{str(resnum2)[:4]:>4}{(icode2 or ' ')[:1]:1}"
        "     1555   1555        "
    )


def _write_pdb_with_glycan_link_records(
    pdb_path: Path,
    output_path: Path,
    glycan_linkages: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Reinject remapped glycoprotein connectivity before prepareforleap."""
    result: Dict[str, Any] = {
        "path": str(output_path),
        "success": True,
        "expected_linkage_count": len(glycan_linkages or []),
        "emitted_link_count": 0,
        "emitted_conect_pair_count": 0,
        "missing_link_count": 0,
        "link_records": [],
        "conect_records": [],
        "errors": [],
        "warnings": [],
    }
    atoms: dict[tuple[str, str, str, str, str], int] = {}
    contents = pdb_path.read_text(encoding="utf-8").splitlines()
    for line in contents:
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 27:
            continue
        serial = line[6:11].strip()
        if not serial.isdigit():
            continue
        key = (
            line[21].strip() or "A",
            line[22:26].strip(),
            line[26].strip(),
            line[17:20].strip().upper(),
            line[12:16].strip(),
        )
        atoms[key] = int(serial)

    link_lines: list[str] = []
    conect_lines: list[str] = []
    for linkage in glycan_linkages or []:
        protein = linkage.get("protein") or {}
        glycan = linkage.get("glycan") or {}
        protein_chain = str(protein.get("merged_chain") or protein.get("chain") or "")
        protein_resnum = protein.get("merged_resnum") or protein.get("resnum")
        protein_icode = str(protein.get("merged_icode") or protein.get("icode") or "")
        glycan_chain = str(glycan.get("merged_chain") or glycan.get("chain") or "")
        glycan_resnum = glycan.get("merged_resnum") or glycan.get("resnum")
        glycan_icode = str(glycan.get("merged_icode") or glycan.get("icode") or "")
        if not all([protein.get("atom"), protein.get("resname"), protein_chain, protein_resnum,
                    glycan.get("atom"), glycan.get("resname"), glycan_chain, glycan_resnum]):
            result["missing_link_count"] += 1
            result["errors"].append(f"Incomplete glycan LINK record: {linkage}")
            continue
        protein_atom_key = (
            protein_chain,
            str(protein_resnum),
            protein_icode,
            str(protein["resname"]).upper(),
            str(protein["atom"]),
        )
        glycan_atom_key = (
            glycan_chain,
            str(glycan_resnum),
            glycan_icode,
            str(glycan["resname"]).upper(),
            str(glycan["atom"]),
        )
        protein_serial = atoms.get(protein_atom_key)
        glycan_serial = atoms.get(glycan_atom_key)
        if protein_serial is None or glycan_serial is None:
            result["missing_link_count"] += 1
            result["errors"].append(
                f"Could not resolve glycan LINK endpoint atoms: {protein_atom_key} - {glycan_atom_key}"
            )
            continue
        line = _format_pdb_link_line(
            atom1=str(protein["atom"]),
            resname1=str(protein["resname"]),
            chain1=protein_chain,
            resnum1=protein_resnum,
            icode1=protein_icode,
            atom2=str(glycan["atom"]),
            resname2=str(glycan["resname"]),
            chain2=glycan_chain,
            resnum2=glycan_resnum,
            icode2=glycan_icode,
        )
        link_lines.append(line)
        conect_lines.append(f"CONECT{protein_serial:5d}{glycan_serial:5d}")
        conect_lines.append(f"CONECT{glycan_serial:5d}{protein_serial:5d}")

    insert_at = next(
        (i for i, line in enumerate(contents) if line.startswith(("ATOM", "HETATM", "MODEL"))),
        len(contents),
    )
    conect_at = next(
        (i for i, line in enumerate(contents) if line.startswith("END")),
        len(contents),
    )
    output_lines = contents[:insert_at] + link_lines + contents[insert_at:conect_at] + conect_lines + contents[conect_at:]
    output_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    result["link_records"] = link_lines
    result["conect_records"] = conect_lines
    result["emitted_link_count"] = len(link_lines)
    result["emitted_conect_pair_count"] = len(conect_lines) // 2
    if result["expected_linkage_count"] and (
        result["missing_link_count"]
        or result["emitted_link_count"] != result["expected_linkage_count"]
        or result["emitted_conect_pair_count"] != result["expected_linkage_count"]
    ):
        result["success"] = False
    return result
