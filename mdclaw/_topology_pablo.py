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
    """

    topology: Any
    positions: Any
    used_pablo: bool
    warnings: list[str] = field(default_factory=list)
    guardrail_codes: list[str] = field(default_factory=list)


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
        )

    if auto_download:
        STD_CCD_CACHE.auto_download = True

    additional_definitions = build_modaa_residue_definitions(extra_smiles)

    try:
        off_topology = topology_from_pdb(
            str(pdb_path),
            additional_definitions=tuple(additional_definitions),
        )
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
        )

    return PabloLoadResult(
        topology=off_topology.to_openmm(),
        positions=off_topology.get_positions().to_openmm(),
        used_pablo=True,
        warnings=warnings,
        guardrail_codes=codes,
    )


def add_disulfide_bonds(
    topology: Any,
    disulfide_pairs: Sequence[dict[str, Any]],
) -> int:
    """Add SG-SG covalent bonds to an OpenMM topology.

    Pablo identifies cysteine residues as such (CYS) but does not infer
    disulfide bridges from proximity. The mdclaw prep pipeline emits a list of
    explicit pairs as ``disulfide_bonds.json``. Current prep emits
    ``{"cys1": {"chain": "A", "resnum": 11, ...}, "cys2": {...}}``;
    older artifacts used ``{"residue_a": {"chain_id": "A",
    "residue_number": 11, ...}, "residue_b": {...}}``. This function accepts
    both shapes and adds the SG-SG bond for each pair so
    ``SystemGenerator.create_system`` produces the crosslink in the resulting
    System.

    Returns the number of bonds actually added (silently skips pairs where one
    side cannot be resolved — the caller can warn on a non-zero discrepancy).
    """
    if not disulfide_pairs:
        return 0

    # Build a lookup keyed by (chain_id, residue_number) → SG atom.
    sg_index: dict[tuple[str, int], Any] = {}
    for residue in topology.residues():
        if (residue.name or "").upper() not in {"CYS", "CYX"}:
            continue
        chain_id = getattr(residue.chain, "id", None) or ""
        try:
            resnum = int(residue.id)
        except (TypeError, ValueError):
            continue
        for atom in residue.atoms():
            if (atom.name or "").upper() == "SG":
                sg_index[(chain_id, resnum)] = atom
                break

    existing_bonds = {
        frozenset({getattr(atom1, "index", id(atom1)), getattr(atom2, "index", id(atom2))})
        for atom1, atom2 in topology.bonds()
    }

    def _pair_endpoint(pair: dict[str, Any], current_key: str, legacy_key: str) -> tuple[str, int] | None:
        current = pair.get(current_key)
        if isinstance(current, dict):
            chain = current.get("chain")
            resnum = current.get("resnum")
        else:
            current = pair.get(legacy_key)
            if not isinstance(current, dict):
                return None
            chain = current.get("chain_id")
            resnum = current.get("residue_number")
        try:
            return (chain or "", int(resnum))
        except (TypeError, ValueError):
            return None

    added = 0
    for pair in disulfide_pairs:
        key_a = _pair_endpoint(pair, "cys1", "residue_a")
        key_b = _pair_endpoint(pair, "cys2", "residue_b")
        if key_a is None or key_b is None:
            continue
        sg_a = sg_index.get(key_a)
        sg_b = sg_index.get(key_b)
        if sg_a is None or sg_b is None:
            continue
        bond_key = frozenset({
            getattr(sg_a, "index", id(sg_a)),
            getattr(sg_b, "index", id(sg_b)),
        })
        if bond_key in existing_bonds:
            continue
        topology.addBond(sg_a, sg_b)
        existing_bonds.add(bond_key)
        added += 1
    return added


__all__ = [
    "PabloLoadResult",
    "build_modaa_residue_definitions",
    "load_topology",
    "add_disulfide_bonds",
]
