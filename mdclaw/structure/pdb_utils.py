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
from typing import Optional, Dict, Any, Tuple  # noqa: E402

from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,
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


def _pdb_chain_id_for_index(index: int) -> str:
    """Return a PDB-compatible one-character chain label.

    The label is intentionally not a canonical identity.  PDB only has one
    chain-ID column, so large assemblies must reuse labels and rely on the
    chain identity map for unambiguous component tracking.
    """
    return PDB_CHAIN_ID_POOL[index % len(PDB_CHAIN_ID_POOL)]


def _path_lookup_keys(path: str | Path | None) -> set[str]:
    """Return stable path spellings for joins across preparation steps."""
    if path is None:
        return set()
    p = Path(path)
    keys = {str(path), str(p)}
    try:
        keys.add(str(p.resolve()))
    except OSError:
        pass
    return keys


def preserve_long_resnames_in_pdb_text(pdb_text: str, topology: Any) -> str:
    """Rewrite 4-character residue names so they survive a PDB round-trip.

    ``openmm.app.PDBFile.writeFile`` truncates residue names longer than three
    characters to their first three characters (``POPC`` -> ``POP``), which both
    collapses distinct lipids (``POPC``/``POPE`` -> ``POP``) and prevents any
    downstream reader from recovering the canonical name. PDB readers (including
    OpenMM's own) accept a 4-character residue name written into columns 18-21,
    so we left-justify the full (<=4 char) name into that field. Names of three
    characters or fewer are untouched, so this is a no-op for proteins, water,
    and ions.

    The residue name for each ``ATOM``/``HETATM`` record is taken from
    ``topology.atoms()`` in order, matching the order ``writeFile`` emits.
    """
    names = [atom.residue.name for atom in topology.atoms()]
    out_lines: list[str] = []
    atom_index = 0
    for line in pdb_text.splitlines():
        if line.startswith(("ATOM  ", "HETATM")):
            if atom_index < len(names):
                resname = names[atom_index]
                atom_index += 1
                if resname and len(resname) >= 4:
                    padded = line.ljust(80)
                    line = (padded[:17] + f"{resname[:4]:<4}" + padded[21:]).rstrip()
        out_lines.append(line)
    trailing = "\n" if pdb_text.endswith("\n") else ""
    return "\n".join(out_lines) + trailing


def _residue_key(line: str) -> tuple[str, str, str]:
    """(chainID, resSeq, iCode) from a PDB ATOM/HETATM record."""
    return (line[21], line[22:26].strip(), line[26])


def restore_residue_numbering_from_reference(
    target_pdb: str | Path, reference_pdb: str | Path
) -> Optional[str]:
    """Rewrite each residue's chain/resSeq/iCode in ``target_pdb`` from
    ``reference_pdb``, matched by residue ORDER.

    ``pdb4amber`` renumbers residues (e.g. it makes numbering continuous across
    chains, turning chain B 1-99 into 215-430) while preserving residue order
    and count — it only adds hydrogens *within* residues. Any site-keyed input
    (``protonation_states`` / ``histidine_states`` keyed by ``chain:resnum``, or
    a detected PTM resnum) captured against the original numbering then points
    at the wrong residue. This copies the reference (pre-pdb4amber)
    chain/resSeq/iCode onto the target, by residue order, leaving every atom,
    coordinate and hydrogen untouched.

    Returns ``target_pdb`` on success, or ``None`` when the residue counts
    differ (caller should then leave the file unchanged — fail safe).
    """
    target_path, ref_path = Path(target_pdb), Path(reference_pdb)
    try:
        ref_lines = ref_path.read_text().splitlines()
        tgt_lines = target_path.read_text().splitlines()
    except OSError:
        return None

    ref_keys: list[tuple[str, str, str]] = []
    for line in ref_lines:
        if line.startswith(("ATOM  ", "HETATM")):
            key = _residue_key(line)
            if not ref_keys or ref_keys[-1] != key:
                ref_keys.append(key)

    # Count target residues first; bail out on any mismatch.
    tgt_residue_count = 0
    last: Optional[tuple[str, str, str]] = None
    for line in tgt_lines:
        if line.startswith(("ATOM  ", "HETATM")):
            key = _residue_key(line)
            if key != last:
                tgt_residue_count += 1
                last = key
    if tgt_residue_count != len(ref_keys) or not ref_keys:
        return None

    out: list[str] = []
    res_idx = -1
    last = None
    for line in tgt_lines:
        if line.startswith(("ATOM  ", "HETATM")):
            key = _residue_key(line)
            if key != last:
                res_idx += 1
                last = key
            chain, resseq, icode = ref_keys[res_idx]
            padded = line.ljust(80)
            line = (
                padded[:21] + f"{chain[:1]:1}" + f"{resseq[:4]:>4}"
                + f"{icode[:1]:1}" + padded[27:]
            ).rstrip()
        out.append(line)
    trailing = "\n" if target_path.read_text().endswith("\n") else "\n"
    target_path.write_text("\n".join(out) + trailing)
    return str(target_path)


def restore_resnames_from_source_pdb(
    pdb_text: str, source_pdb: str | Path
) -> Optional[str]:
    """Overlay residue names from a source PDB onto an exported PDB, by index.

    ``openmm.app.PDBFile`` normalizes Amber protonation-state and water residue
    names when it *loads* a structure (``GLH``->``GLU``, ``ASH``->``ASP``,
    ``HID``/``HIE``/``HIP``->``HIS``, ``LYN``->``LYS``, ``CYX``/``CYM``->``CYS``,
    ``WAT``->``HOH``, ...). A structure written back out from that loaded
    topology therefore loses the protonation-state name even though the protons
    (``HE2`` etc.) are still present and the chemistry is unchanged. The source
    ``topology.pdb`` emitted by the topo build carries the canonical names; this
    rewrites the residue-name column (cols 18-21) of each ``ATOM``/``HETATM``
    record from that source, matched by record order (identical atom order,
    since the export was written from a topology loaded out of the same file).

    This is a pure text relabel of the exported artifact — it does not touch
    coordinates, the OpenMM ``System``, the restart ``state.xml``, or anything
    the run side consumes, so it cannot affect the MD result.

    Returns the rewritten text, or ``None`` when the source cannot be read or
    its ``ATOM``/``HETATM`` count does not match (caller should then fall back).
    """
    try:
        source_names = [
            line[17:21].strip()
            for line in Path(source_pdb).read_text().splitlines()
            if line.startswith(("ATOM  ", "HETATM"))
        ]
    except OSError:
        return None
    lines = pdb_text.splitlines()
    n_records = sum(1 for ln in lines if ln.startswith(("ATOM  ", "HETATM")))
    if n_records != len(source_names) or n_records == 0:
        return None
    out_lines: list[str] = []
    idx = 0
    for line in lines:
        if line.startswith(("ATOM  ", "HETATM")):
            name = source_names[idx]
            idx += 1
            if name:
                padded = line.ljust(80)
                line = (padded[:17] + f"{name[:4]:<4}" + padded[21:]).rstrip()
        out_lines.append(line)
    trailing = "\n" if pdb_text.endswith("\n") else ""
    return "\n".join(out_lines) + trailing


def render_simulation_pdb_preserving_resnames(
    topology: Any, positions: Any, source_topology_pdb: Optional[str | Path]
) -> str:
    """Serialize an OpenMM topology+positions to PDB text, preserving the
    Amber/PTM/water residue names that OpenMM's ``PDBFile`` loader normalized
    away when the run stage loaded ``topology.pdb``.

    This is the single export path shared by min / eq / prod: write to a buffer,
    overlay the canonical names from the topo node's ``topology.pdb`` (the
    authoritative, name-correct topology contract), and fall back to the
    long-resname patch when that source is missing or its atom count does not
    match. Pure text relabel — never touches coordinates, ``system.xml``, or
    ``state.xml``, so the MD result is unaffected.
    """
    import io

    from openmm.app import PDBFile

    buffer = io.StringIO()
    PDBFile.writeFile(topology, positions, buffer)
    text = None
    if source_topology_pdb:
        text = restore_resnames_from_source_pdb(
            buffer.getvalue(), source_topology_pdb
        )
    if text is None:
        text = preserve_long_resnames_in_pdb_text(buffer.getvalue(), topology)
    return text


def _pdb_atom_descriptor(line: str) -> dict[str, Any]:
    """Return a compact, serializable descriptor for a PDB atom record."""
    chain = line[21].strip() if len(line) > 21 else ""
    return {
        "serial": line[6:11].strip(),
        "atom_name": line[12:16].strip(),
        "resname": line[17:20].strip(),
        "chain": chain,
        "resnum": line[22:26].strip(),
        "icode": line[26].strip() if len(line) > 26 else "",
        "element": line[76:78].strip() if len(line) >= 78 else "",
    }


def _is_deuterium_atom_record(line: str) -> bool:
    """Return True for experimental deuterium atom records in PDB text."""
    if not line.startswith(("ATOM", "HETATM")):
        return False
    atom_name = line[12:16].strip().upper()
    element = line[76:78].strip().upper() if len(line) >= 78 else ""
    if element == "D":
        return True
    if element:
        return False
    return bool(_DEUTERIUM_FALLBACK_ATOM_NAME_RE.fullmatch(atom_name))


def _component_disposition_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the v1 component disposition artifact payload."""
    excluded_entries = [entry for entry in entries if entry.get("action_taken") == "excluded"]
    isotope_atoms = sum(
        int(entry.get("atom_count", 0))
        for entry in excluded_entries
        if entry.get("classification") == "experimental_isotope"
    )
    excluded_atoms = sum(int(entry.get("atom_count", 0)) for entry in excluded_entries)
    return {
        "schema_version": "mdclaw.component_disposition.v1",
        "summary": {
            "experimental_isotope_atoms_excluded": isotope_atoms,
            "excluded_atom_count": excluded_atoms,
            "excluded_component_count": len(excluded_entries),
        },
        "entries": entries,
    }


def _exclude_deuterium_atoms_from_pdb(input_path: Path, output_path: Path) -> dict[str, Any]:
    """Write *output_path* with experimental deuterium atom records removed."""
    lines = input_path.read_text().splitlines()
    kept: list[str] = []
    excluded_atoms: list[dict[str, Any]] = []
    for line in lines:
        if _is_deuterium_atom_record(line):
            excluded_atoms.append(_pdb_atom_descriptor(line))
            continue
        kept.append(line)

    if excluded_atoms:
        output_path.write_text("\n".join(kept) + "\n")
        entries = [
            {
                "component_id": "experimental_isotope_deuterium",
                "classification": "experimental_isotope",
                "default_action": "exclude",
                "action_taken": "excluded",
                "atom_count": len(excluded_atoms),
                "reason": (
                    "Experimental deuterium atoms are excluded from the default "
                    "classical MD preparation path; standard hydrogens are rebuilt downstream."
                ),
                "sample_atoms": excluded_atoms[:20],
            }
        ]
    else:
        entries = []
    return _component_disposition_payload(entries)


def _normalize_prepare_solvent_type(solvent_type: Optional[str]) -> Optional[str]:
    """Normalize prep-stage solvent intent; prep defaults to explicit solvent."""
    if solvent_type is None:
        return "explicit"
    normalized = str(solvent_type).strip().lower().replace("-", "_")
    aliases = {
        "explicit_water": "explicit",
        "explicit_solvent": "explicit",
        "implicit_solvent": "implicit",
        "no_solvent": "vacuum",
        "none": "vacuum",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized == "":
        return "explicit"
    return normalized


def _pdb_atom_record_count(path: Path) -> int:
    """Count atom records in a PDB fragment for disposition summaries."""
    try:
        return sum(
            1 for line in path.read_text().splitlines()
            if line.startswith(("ATOM", "HETATM"))
        )
    except OSError:
        return 0


def _split_file_list_key(component_type: str) -> str:
    return {
        "protein": "protein_files",
        "nucleic": "nucleic_files",
        "glycan": "glycan_files",
        "ligand": "ligand_files",
        "ion": "ion_files",
        "water": "water_files",
    }.get(component_type, "ligand_files")


def _replace_split_result_file(
    split_result: dict,
    *,
    component_type: str,
    old_path: str,
    new_path: str,
) -> None:
    """Update split_molecules path bookkeeping after component normalization."""
    key = _split_file_list_key(component_type)
    split_result[key] = [
        new_path if path == old_path else path
        for path in split_result.get(key, [])
    ]
    for info in split_result.get("chain_file_info", []):
        if info.get("file") == old_path:
            info.setdefault("source_file", old_path)
            info["file"] = new_path
            info["normalized_file"] = new_path


def _component_disposition_metadata(chain_info: dict, component_type: str) -> dict[str, Any]:
    """Return shared component identity fields for disposition entries."""
    metadata: dict[str, Any] = {
        "component_type": component_type,
        "chain_id": chain_info.get("chain_id"),
        "author_chain": chain_info.get("author_chain"),
        "resnum": chain_info.get("resnum"),
        "unique_id": chain_info.get("unique_id"),
        "nucleic_subtype": chain_info.get("nucleic_subtype"),
    }
    residue_names = chain_info.get("residue_names")
    if isinstance(residue_names, dict):
        names = residue_names.get("unique_residues", [])
    elif isinstance(residue_names, list):
        names = residue_names
    else:
        names = []
    if names:
        metadata["residue_names"] = sorted(set(names))
        metadata["residue_name"] = metadata["residue_names"][0]
    return {key: value for key, value in metadata.items() if value not in (None, [], {})}


def _apply_component_disposition_to_split_result(
    split_result: dict,
    *,
    solvent_type: Optional[str] = "explicit",
) -> dict[str, Any]:
    """Apply component-common prep disposition to split PDB fragments.

    The split result is mutated in place so all downstream preparation steps
    consume normalized component files.
    """
    entries: list[dict[str, Any]] = []
    retained_ion_files: list[str] = []
    excluded_ion_files: list[str] = []
    all_chain_lookup = {
        chain.get("chain_id"): chain
        for chain in split_result.get("all_chains", []) or []
    }

    for info in split_result.get("chain_file_info", []) or []:
        component_type = info.get("chain_type", "ligand")
        current_file = info.get("file")
        if not current_file:
            continue
        info = {
            **all_chain_lookup.get(info.get("chain_id"), {}),
            **info,
        }
        current_path = Path(current_file)
        metadata = _component_disposition_metadata(info, component_type)

        deuterium_stripped_file = current_path.parent / (
            f"{current_path.stem}.deuterium_stripped{current_path.suffix}"
        )
        deuterium_payload = _exclude_deuterium_atoms_from_pdb(
            current_path,
            deuterium_stripped_file,
        )
        if deuterium_payload.get("entries"):
            new_file = str(deuterium_stripped_file)
            _replace_split_result_file(
                split_result,
                component_type=component_type,
                old_path=current_file,
                new_path=new_file,
            )
            info = {
                **info,
                "source_file": current_file,
                "file": new_file,
                "normalized_file": new_file,
            }
            current_file = new_file
            current_path = deuterium_stripped_file
            for entry in deuterium_payload.get("entries", []):
                entries.append({
                    **entry,
                    **metadata,
                    "source_file": str(info.get("source_file")),
                    "normalized_file": new_file,
                })

        if component_type == "ion":
            if solvent_type == "implicit":
                atom_count = _pdb_atom_record_count(current_path)
                excluded_ion_files.append(current_file)
                entries.append({
                    "component_id": (
                        f"explicit_ion:{metadata.get('unique_id')}"
                        if metadata.get("unique_id")
                        else f"explicit_ion:{current_path.stem}"
                    ),
                    "classification": "explicit_ion",
                    "default_action": "retain",
                    "action_taken": "excluded",
                    "atom_count": atom_count,
                    "reason": (
                        "Explicit ion particles are excluded from the prep "
                        "output for implicit solvent; continuum solvent "
                        "topology should not retain discrete ions."
                    ),
                    **metadata,
                    "source_file": current_file,
                })
            else:
                retained_ion_files.append(current_file)

    return {
        "component_disposition": _component_disposition_payload(entries),
        "retained_ion_files": retained_ion_files,
        "excluded_ion_files": excluded_ion_files,
    }


def _pdb_atom_count(pdb_file: str | Path) -> int:
    """Count atom records in a PDB file."""
    return sum(
        1
        for line in Path(pdb_file).read_text().splitlines()
        if line.startswith(("ATOM  ", "HETATM"))
    )


def _pdb_hydrogen_count(pdb_file: str | Path) -> int:
    """Count hydrogen-like atom records in a PDB file."""
    count = 0
    for line in Path(pdb_file).read_text().splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        element = line[76:78].strip().upper() if len(line) >= 78 else ""
        atom_name = line[12:16].strip().upper()
        if element in {"H", "D"} or atom_name.startswith(("H", "D")):
            count += 1
    return count


def _pdb_residue_names(pdb_file: str | Path) -> set[str]:
    """Return residue names present in a PDB file."""
    names: set[str] = set()
    for line in Path(pdb_file).read_text().splitlines():
        if line.startswith(("ATOM", "HETATM")) and len(line) >= 20:
            names.add(line[17:20].strip().upper())
    return names


def _pdb_hydrogen_counts_by_resname(
    pdb_file: str | Path,
    residue_names: set[str],
) -> dict[str, int]:
    """Count hydrogen-like atom records grouped by residue name."""
    counts = {name: 0 for name in residue_names}
    for line in Path(pdb_file).read_text().splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        resname = line[17:20].strip().upper()
        if resname not in counts:
            continue
        element = line[76:78].strip().upper() if len(line) >= 78 else ""
        atom_name = line[12:16].strip().upper()
        if element in {"H", "D"} or atom_name.startswith(("H", "D")):
            counts[resname] += 1
    return counts


def _pdb_noncap_protein_hydrogen_signature(
    pdb_file: str | Path,
) -> dict[str, tuple[str, ...]]:
    """Return non-cap protein H atom-name sets keyed by residue identity."""
    hydrogens: dict[str, list[str]] = {}
    for line in Path(pdb_file).read_text().splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        resname = line[17:20].strip().upper()
        if resname in TERMINAL_CAP_RESIDUES:
            continue
        element = line[76:78].strip().upper() if len(line) >= 78 else ""
        atom_name = line[12:16].strip().upper()
        if element not in {"H", "D"} and not atom_name.startswith(("H", "D")):
            continue
        chain = line[21:22].strip()
        resseq = line[22:26].strip()
        icode = line[26:27].strip()
        key = f"{chain}:{resseq}:{icode}:{resname}"
        hydrogens.setdefault(key, []).append(atom_name)
    return {
        key: tuple(sorted(names))
        for key, names in hydrogens.items()
    }


def _fix_amino_acid_hetatm_records(pdb_file: Path) -> None:
    """Convert HETATM to ATOM for residues with amino acid backbone.

    gemmi doesn't recognize Amber residue naming (HIE, NALA, etc.) and
    writes them as HETATM. Detect amino acids by backbone atoms (N, CA, C)
    instead of maintaining a residue name list.

    Also removes HET header records for amino acid residues, which confuse
    external tools like MEMEMBED (used by packmol-memgen for membrane embedding).
    """
    import gemmi

    # Read structure to identify amino acid residues
    st = gemmi.read_pdb(str(pdb_file))
    amino_acid_residues = set()  # (chain_id, resnum, resname)
    amino_acid_resnames = set()  # Just resnames for HET record filtering

    for model in st:
        for chain in model:
            for res in chain:
                atom_names = {a.name for a in res}
                # Check for backbone atoms (N, CA, C)
                if {"N", "CA", "C"}.issubset(atom_names):
                    amino_acid_residues.add((chain.name, res.seqid.num, res.name))
                    amino_acid_resnames.add(res.name)

    if not amino_acid_residues:
        return  # No amino acids to fix

    # Fix HETATM records and remove HET header records for amino acids
    with open(pdb_file) as f:
        lines = f.readlines()

    fixed_hetatm_count = 0
    removed_het_count = 0
    fixed_lines = []
    for line in lines:
        # Remove HET header records for amino acid residues
        # Format: HET    resname chain resnum  natoms
        if line.startswith("HET ") or line.startswith("HET\t"):
            try:
                parts = line.split()
                if len(parts) >= 2:
                    het_resname = parts[1].strip()
                    if het_resname in amino_acid_resnames:
                        removed_het_count += 1
                        continue  # Skip this HET record
            except (IndexError, ValueError):
                pass
        # Convert HETATM to ATOM for amino acid residues
        elif line.startswith("HETATM"):
            chain_id = line[21].strip() or line[21]
            try:
                resnum = int(line[22:26])
                resname = line[17:20].strip()
                if (chain_id, resnum, resname) in amino_acid_residues:
                    line = "ATOM  " + line[6:]
                    fixed_hetatm_count += 1
            except ValueError:
                pass
        fixed_lines.append(line)

    with open(pdb_file, 'w') as f:
        f.writelines(fixed_lines)

    if fixed_hetatm_count > 0:
        logger.info(f"Fixed {fixed_hetatm_count} HETATM records to ATOM for amino acid residues")
    if removed_het_count > 0:
        logger.info(f"Removed {removed_het_count} HET header records for amino acid residues")


def _iter_unique_conect_bonds(conect_map: dict) -> list[tuple[int, int, int]]:
    """Return unique PDB CONECT bonds as ``(serial1, serial2, order)``.

    Gemmi stores CONECT as a low-level serial-number map.  Some writers emit
    both directions, and bond order is represented by repeating the partner
    serial.  Collapse those records into one unordered bond while preserving
    the maximum directional repeat count as the order.
    """
    directional_counts: Dict[Tuple[int, int], int] = {}
    for serial1, partners in (conect_map or {}).items():
        try:
            s1 = int(serial1)
        except (TypeError, ValueError):
            continue
        for partner in partners or []:
            try:
                s2 = int(partner)
            except (TypeError, ValueError):
                continue
            if s1 <= 0 or s2 <= 0 or s1 == s2:
                continue
            directional_counts[(s1, s2)] = directional_counts.get((s1, s2), 0) + 1

    pair_orders: Dict[Tuple[int, int], int] = {}
    for (s1, s2), count in directional_counts.items():
        key = (s1, s2) if s1 < s2 else (s2, s1)
        pair_orders[key] = max(pair_orders.get(key, 0), count)

    return [
        (s1, s2, order)
        for (s1, s2), order in sorted(pair_orders.items())
    ]


def _read_pdb_unique_residues(pdb_file: str | Path) -> list[dict]:
    """Read unique residue records from a PDB file without changing order."""
    residues = []
    seen = set()
    for line in Path(pdb_file).read_text().splitlines():
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 27:
            continue
        chain = line[21].strip() or "A"
        resnum = line[22:26].strip()
        icode = line[26].strip()
        resname = line[17:20].strip()
        key = (chain, resnum, icode, resname)
        if key in seen:
            continue
        seen.add(key)
        residues.append({
            "chain": chain,
            "resnum": int(resnum) if resnum.lstrip("-").isdigit() else resnum,
            "resnum_str": resnum,
            "icode": icode,
            "resname": resname,
        })
    return residues


def _rename_pdb_residues(
    input_pdb: Path,
    output_pdb: Path,
    rename_map: dict[tuple[str, str, str], str],
) -> dict:
    atom_count = 0
    residue_keys = set()
    renamed = 0
    out_lines = []
    for line in input_pdb.read_text().splitlines():
        if line.startswith(("ATOM", "HETATM")) and len(line) >= 27:
            atom_count += 1
            chain = line[21].strip() or "A"
            resnum = line[22:26].strip()
            icode = line[26].strip()
            residue_keys.add((chain, resnum, icode))
            new_name = rename_map.get((chain, resnum, icode))
            if new_name:
                line = line[:17] + new_name.rjust(3)[:3] + line[20:]
                renamed += 1
        out_lines.append(line)
    output_pdb.write_text("\n".join(out_lines) + "\n")
    return {
        "atom_count": atom_count,
        "residue_count": len(residue_keys),
        "renamed_atom_count": renamed,
    }
