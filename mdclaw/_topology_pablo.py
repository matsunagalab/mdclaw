"""OpenFF Pablo bridge for the openmmforcefields-unification refactor.

Pablo (`openff-pablo`) is the OpenFF Initiative's PDB → OpenFF Topology loader
that uses the PDB Chemical Component Dictionary instead of bond-from-distance
guessing. mdclaw uses Pablo as the topology source for both
``build_amber_system`` and ``build_openmm_system``; this module wraps the
Pablo entrypoint with project-specific concerns:

- Auto-download of CCD residue definitions (so PDB glycan residue names such
  as NAG / BMA / MAN load without manual library curation).
- ``additional_definitions`` builder for modified amino acids and GAFF-backed
  ligands supplied as SMILES strings via the user-facing ``extra_smiles``
  argument. The residue-name half of each pair is diagnostic; Pablo receives
  anonymous SMILES-derived definitions and matches by graph / atom composition.
- Convertor to OpenMM topology + positions, ready to feed
  ``openmmforcefields.SystemGenerator``.
- Soft fallback to ``openmm.app.PDBFile`` when Pablo fails to identify a
  residue; the caller receives a warning code instead of a hard failure so
  Pablo's pre-1.0 churn does not break otherwise-fine inputs.

The module deliberately avoids importing ``openff.pablo`` at module load
time — Pablo is a pre-1.0 dependency and we want ``import mdclaw`` to keep
working even when the user has not yet run ``conda env update``. Imports
happen lazily inside the public functions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)


@dataclass
class PabloLoadResult:
    """Outcome of a Pablo-or-fallback topology load.

    Attributes:
        topology: An ``openmm.app.Topology`` ready for ``SystemGenerator``.
        positions: Atom positions as an ``openmm.unit.Quantity`` array.
        used_pablo: True when Pablo handled the load; False on PDBFile fallback.
        warnings: Human-readable warning messages (Pablo errors when fallback
            kicked in, missing-residue notices, etc.).
        guardrail_codes: Stable code identifiers the caller can branch on
            (``pablo_topology_fallback``, ``pablo_unknown_residue`` etc.).
        auto_download: Whether Pablo CCD auto-download was enabled for this
            load attempt. This is per-call provenance; the helper restores
            Pablo's process-global cache setting before returning.
    """

    topology: Any
    positions: Any
    used_pablo: bool
    warnings: list[str] = field(default_factory=list)
    guardrail_codes: list[str] = field(default_factory=list)
    auto_download: bool = True
    solvent_atoms_via_pdbfile: int = 0


def build_modaa_residue_definitions(
    extra_smiles: Sequence[tuple[str, str]],
) -> list[Any]:
    """Wrap ``(residue_name, smiles)`` pairs as Pablo ``ResidueDefinition``s.

    Pablo's standard library covers canonical amino acids and the CCD-fetchable
    glycans / nucleotides; modified amino acids and GAFF-backed ligands are not
    guaranteed to be present by residue name. Callers pass their SMILES via
    ``extra_smiles``; this helper turns each tuple into an anonymous
    ``ResidueDefinition.anon_from_smiles`` so Pablo can match by graph / atom
    composition. The tuple's residue name is retained only for diagnostics.

    Returns an empty list if Pablo is not installed (the caller will fall back
    to PDBFile which performs no chemistry checks).
    """
    if not extra_smiles:
        return []
    try:
        from openff.pablo import ResidueDefinition  # noqa: WPS433
    except ImportError:
        logger.warning(
            "openff-pablo is not installed; modAA residue definitions ignored. "
            "Install via `conda env update -f environment.yml`."
        )
        return []

    definitions: list[Any] = []
    for residue_name, smiles in extra_smiles:
        try:
            definitions.append(ResidueDefinition.anon_from_smiles(smiles))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not build ResidueDefinition for %s from SMILES %r: %s",
                residue_name, smiles, exc,
            )
    return definitions


def residue_names_in_pdb(pdb_path: Path) -> list[str]:
    """Distinct residue names of a PDB file, in order of first appearance."""
    names: dict[str, None] = {}
    try:
        with Path(pdb_path).open() as handle:
            for line in handle:
                if line.startswith(("ATOM  ", "HETATM")):
                    name = line[17:20].strip()
                    if name:
                        names.setdefault(name.upper(), None)
    except OSError:
        return []
    return list(names)


def seed_absent_ccd_names(cache: Any, residue_names: Sequence[str]) -> dict:
    """Look each unknown residue name up in the CCD once, and remember misses.

    Pablo asks the CCD for a name every time a residue with that name fails to
    match, and a name the CCD does not have (lipid21's ``PA`` / ``OL`` / ``PC``
    fragments) is asked again for every such residue: one HTTP request per
    lipid, about 700 per membrane build, before Pablo gives up and the
    ``PDBFile`` fallback runs. Under six concurrent agents that took 395 s on
    2026-09-10 (22 s for a smaller membrane, 27 s in isolation). Registering
    an empty definition list for a missed name makes Pablo's lookup answer
    "no definitions" immediately, so each absent name costs one request.
    """
    definitions = getattr(cache, "_definitions", None)
    outcome = {"looked_up": [], "absent": [], "unreachable": []}
    if not isinstance(definitions, dict):
        return outcome
    for name in residue_names:
        key = str(name).upper()
        if key in definitions or key in ("UNK", "UNL"):
            continue
        outcome["looked_up"].append(key)
        try:
            cache[key]
        except KeyError as exc:
            reason = " ".join(str(part) for part in exc.args[1:])
            if "could not be accessed" in reason:
                outcome["unreachable"].append(key)
                continue
            definitions.setdefault(key, [])
            outcome["absent"].append(key)
        except Exception:  # noqa: BLE001 - a probe failure must not change the load
            outcome["unreachable"].append(key)
    return outcome


# A trailing block of water and ions at least this large is read by
# ``openmm.app.PDBFile`` instead of Pablo. Pablo identifies every residue
# against the CCD, right for the solute and wasted on 100k identical waters:
# the campaign's builds above 250k atoms spent a median 127 s (max 276 s) in
# the load, where PDBFile parses the same block in a few seconds.
SOLVENT_FAST_PATH_MIN_ATOMS = 30_000


def _solvent_block_names() -> frozenset[str]:
    from mdclaw.chemistry_constants import STANDARD_BARE_ION_RESNAME_KEYS, WATER_NAMES

    return frozenset(WATER_NAMES) | frozenset(STANDARD_BARE_ION_RESNAME_KEYS)


def split_trailing_solvent(
    pdb_path: Path, *, min_atoms: int | None = None,
) -> tuple[list[str], list[str]] | None:
    """``(solute lines, solvent lines)`` when the file ends in enough water and ions.

    The solvent block is the run of water / bare-ion residues at the end of the
    atom records; the solute keeps every other line (header, CRYST1, CONECT,
    END) in its order. ``None`` when there is no such block, when the whole
    file is solvent, or when the block is under ``min_atoms``
    (``SOLVENT_FAST_PATH_MIN_ATOMS`` by default).
    """
    names = _solvent_block_names()
    lines = Path(pdb_path).read_text(errors="ignore").splitlines(keepends=True)
    atom_indices = [i for i, line in enumerate(lines) if line.startswith(("ATOM  ", "HETATM"))]
    start = len(atom_indices)
    for k in range(len(atom_indices) - 1, -1, -1):
        if lines[atom_indices[k]][17:20].strip().upper() not in names:
            break
        start = k
    solvent_atoms = len(atom_indices) - start
    floor = SOLVENT_FAST_PATH_MIN_ATOMS if min_atoms is None else min_atoms
    if solvent_atoms == 0 or start == 0 or solvent_atoms < floor:
        return None
    boundary = atom_indices[start]
    records = ("ATOM  ", "HETATM", "TER")
    solvent = [line for line in lines[boundary:] if line.startswith(records)]
    solute = lines[:boundary] + [line for line in lines[boundary:] if not line.startswith(records)]
    return solute, solvent


def _append_solvent_block(topology: Any, positions: Any, solvent_lines: list[str]) -> tuple[Any, Any, int]:
    """Read the solvent block with PDBFile and append it the way Pablo would have.

    PDBFile canonicalises names (WAT -> HOH) and groups residues into chains
    by chain id; Pablo keeps the file's names, numbers residues with ints and
    gives every water and ion a chain of its own. The appended block follows
    Pablo so the two paths produce one topology.
    """
    import io

    import numpy as np
    from openmm import unit
    from openmm.app import PDBFile

    solvent = PDBFile(io.StringIO("".join(solvent_lines) + "END\n"))
    atom_lines = [line for line in solvent_lines if line.startswith(("ATOM  ", "HETATM"))]
    atoms = list(solvent.topology.atoms())
    if len(atoms) != len(atom_lines):
        raise ValueError(
            f"PDBFile read {len(atoms)} solvent atoms from {len(atom_lines)} records")
    new_atoms: dict[int, Any] = {}
    for residue in solvent.topology.residues():
        first = atom_lines[next(iter(residue.atoms())).index]
        chain = topology.addChain(id=residue.chain.id)
        residue_id = first[22:26].strip()
        # Water keeps PDBFile's canonical name: OpenMM's createSystem keys
        # rigidWater and the hydrogenMass exemption on ``res.name == 'HOH'``,
        # so a WAT-named block would come out flexible with 4 amu hydrogens.
        residue_name = "HOH" if residue.name == "HOH" else first[17:20].strip()
        new_residue = topology.addResidue(
            residue_name, chain,
            id=int(residue_id) if residue_id.lstrip("-").isdigit() else residue_id,
            insertionCode=residue.insertionCode)
        for atom in residue.atoms():
            line = atom_lines[atom.index]
            new_atoms[atom.index] = topology.addAtom(line[12:16].strip(), atom.element, new_residue, id=atom.id)
    for atom_a, atom_b in solvent.topology.bonds():
        topology.addBond(new_atoms[atom_a.index], new_atoms[atom_b.index])
    merged = np.vstack([
        np.asarray(positions.value_in_unit(unit.nanometer), dtype=float).reshape(-1, 3),
        np.asarray(solvent.positions.value_in_unit(unit.nanometer), dtype=float).reshape(-1, 3),
    ])
    return topology, unit.Quantity(merged, unit.nanometer), len(atoms)


def _canonicalise_water_names(topology: Any) -> int:
    """Rename water residues to ``HOH`` as ``openmm.app.PDBFile`` does.

    OpenMM's ``ForceField.createSystem`` recognises water only by
    ``res.name == 'HOH'`` (rigid-water constraints and the hydrogenMass
    exemption). Pablo keeps the file's residue names, so an Amber-style
    ``WAT`` block would otherwise be parameterised as flexible water and
    receive repartitioned 4 amu hydrogens. Returns the number renamed.
    """
    from mdclaw.chemistry_constants import WATER_NAMES

    names = {str(n).upper() for n in WATER_NAMES}
    renamed = 0
    for residue in topology.residues():
        if residue.name != "HOH" and residue.name.upper() in names:
            residue.name = "HOH"
            renamed += 1
    return renamed


def load_topology(
    pdb_path: Path,
    *,
    extra_smiles: Sequence[tuple[str, str]] = (),
    auto_download: bool = True,
) -> PabloLoadResult:
    """Load a PDB into an OpenMM topology + positions, preferring Pablo.

    Pablo's ``topology_from_pdb`` is tried first; if it fails to identify a
    residue (the common pre-1.0 failure mode), falls back to
    ``openmm.app.PDBFile`` and surfaces a ``pablo_topology_fallback`` warning
    code. Either way the return value is shaped identically so callers don't
    need to branch.

    The caller is responsible for ensuring ``pdb_path`` already has hydrogens
    and complete chemistry. Topology loading does not repair the structure;
    MDClaw prep owns that responsibility.
    """
    from openmm.app import PDBFile  # local import keeps openmm optional

    pdb_path = Path(pdb_path)
    warnings: list[str] = []
    codes: list[str] = []

    try:
        from openff.pablo import (  # noqa: WPS433
            STD_CCD_CACHE,
            topology_from_pdb,
        )
    except ImportError:
        warnings.append(
            "openff-pablo not installed; falling back to openmm.app.PDBFile."
        )
        codes.append("pablo_topology_fallback")
        omm_pdb = PDBFile(str(pdb_path))
        return PabloLoadResult(
            topology=omm_pdb.topology,
            positions=omm_pdb.positions,
            used_pablo=False,
            warnings=warnings,
            guardrail_codes=codes,
            auto_download=bool(auto_download),
        )

    additional_definitions = build_modaa_residue_definitions(extra_smiles)
    previous_auto_download = getattr(STD_CCD_CACHE, "auto_download", None)

    try:
        if previous_auto_download is not None:
            STD_CCD_CACHE.auto_download = bool(auto_download)
        if auto_download:
            ccd_probe = seed_absent_ccd_names(STD_CCD_CACHE, residue_names_in_pdb(pdb_path))
            if ccd_probe["absent"]:
                warnings.append(
                    "Residue names absent from the CCD (one lookup each, then skipped): "
                    + ", ".join(ccd_probe["absent"])
                )
        split = split_trailing_solvent(pdb_path)
        try:
            if split is None:
                off_topology = topology_from_pdb(
                    str(pdb_path),
                    additional_definitions=tuple(additional_definitions),
                )
            else:
                import tempfile

                solute_lines, solvent_lines = split
                with tempfile.TemporaryDirectory(prefix="pablo_solute_") as tmpdir:
                    solute_path = Path(tmpdir) / pdb_path.name
                    solute_path.write_text("".join(solute_lines))
                    off_topology = topology_from_pdb(
                        str(solute_path),
                        additional_definitions=tuple(additional_definitions),
                    )
        finally:
            if previous_auto_download is not None:
                STD_CCD_CACHE.auto_download = previous_auto_download
    except Exception as exc:  # noqa: BLE001
        # Pablo's failure modes (PdbResidueMatchError, etc.) all become
        # warnings; we keep the run alive on the openmm.app.PDBFile path.
        warnings.append(
            f"Pablo could not parse {pdb_path.name}: {type(exc).__name__}: {exc}"
        )
        codes.append("pablo_topology_fallback")
        omm_pdb = PDBFile(str(pdb_path))
        return PabloLoadResult(
            topology=omm_pdb.topology,
            positions=omm_pdb.positions,
            used_pablo=False,
            warnings=warnings,
            guardrail_codes=codes,
            auto_download=bool(auto_download),
        )

    topology = off_topology.to_openmm()
    positions = off_topology.get_positions().to_openmm()
    solvent_atoms = 0
    if split is not None:
        topology, positions, solvent_atoms = _append_solvent_block(topology, positions, split[1])
    _canonicalise_water_names(topology)
    return PabloLoadResult(
        topology=topology,
        positions=positions,
        used_pablo=True,
        warnings=warnings,
        guardrail_codes=codes,
        auto_download=bool(auto_download),
        solvent_atoms_via_pdbfile=solvent_atoms,
    )


def add_disulfide_bonds(
    topology: Any,
    disulfide_pairs: Sequence[dict[str, Any]],
) -> int:
    """Validate the complete request before adding any resolved SG-SG bonds."""
    from mdclaw.amber.disulfide_contract import apply_resolved_disulfides, resolve_disulfides

    pairs = resolve_disulfides(topology, disulfide_pairs)
    records = apply_resolved_disulfides(topology, pairs)
    return sum(record["status"] == "emitted" for record in records)


__all__ = [
    "PabloLoadResult",
    "build_modaa_residue_definitions",
    "load_topology",
    "split_trailing_solvent",
    "add_disulfide_bonds",
]
