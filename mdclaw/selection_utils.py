"""Selection helpers shared by structure inspection and splitting."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


ASSOCIATED_LIGAND_ANCHOR_TYPES = {"protein", "nucleic", "glycan"}

# Curated PDB ligand (HETATM) codes that are crystallization additives,
# cryoprotectants, buffer components, or unknown/placeholder residues rather
# than biologically meaningful cofactors or substrates. When one of these is
# swept into the ``ligand`` selection it almost always fails GAFF / template
# parameterization -- either in ``prepare_complex`` (ligand read/chemistry) or
# much later at ``build_amber_system`` with ``No template found for residue
# <RESNAME>``. They are surfaced so agents can drop them (omit ``ligand`` from
# ``--include-types``) unless the task explicitly names one as the target.
# Real cofactors/substrates (ATP, NAD, HEM, FAD, retinal, heme, ...) are
# deliberately absent from this map.
LIKELY_ADDITIVE_RESNAMES: dict[str, str] = {
    # polyols / cryoprotectants
    "GOL": "cryoprotectant", "EDO": "cryoprotectant", "PEG": "cryoprotectant",
    "PG4": "cryoprotectant", "1PE": "cryoprotectant", "2PE": "cryoprotectant",
    "PGE": "cryoprotectant", "P6G": "cryoprotectant", "PE4": "cryoprotectant",
    "XPE": "cryoprotectant", "7PE": "cryoprotectant", "12P": "cryoprotectant",
    "15P": "cryoprotectant", "MPD": "cryoprotectant", "MRD": "cryoprotectant",
    "BU3": "cryoprotectant", "PDO": "cryoprotectant", "DIO": "cryoprotectant",
    # organic solvents
    "EOH": "solvent", "MOH": "solvent", "IPA": "solvent", "DMS": "solvent",
    "ACN": "solvent", "ACT": "solvent", "ACY": "solvent", "FMT": "solvent",
    "TFA": "solvent", "EEE": "solvent", "BME": "solvent", "DTT": "solvent",
    # buffers / small counter-species
    "MES": "buffer", "EPE": "buffer", "TRS": "buffer", "BTB": "buffer",
    "IMD": "buffer", "BIS": "buffer", "CIT": "buffer", "FLC": "buffer",
    "TLA": "buffer", "MLA": "buffer", "MLI": "buffer", "POP": "buffer",
    "SO4": "buffer", "PO4": "buffer", "2HP": "buffer", "PI": "buffer",
    "NO3": "buffer", "AZI": "buffer", "NH4": "buffer", "GAI": "buffer",
    "CO3": "buffer", "BCT": "buffer",
    # unknown / placeholder residues (never parameterizable)
    "UNX": "unknown", "UNL": "unknown", "UNK": "unknown",
}

# Placeholder residues that carry no defined chemistry and therefore can never
# be parameterized; selecting them should block rather than warn.
UNPARAMETRIZABLE_PLACEHOLDER_RESNAMES = frozenset({"UNX", "UNL", "UNK"})


def _residue_names(chain: dict[str, Any]) -> list[str]:
    names = chain.get("residue_names")
    if isinstance(names, dict):
        values = names.get("unique_residues") or []
        return [str(value) for value in values]
    if isinstance(names, list):
        return [str(value) for value in names]
    return []


def associated_ligand_candidates(
    chains: Iterable[dict[str, Any]],
    *,
    anchor_chain_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return ligand candidates associated by source author-chain identity.

    PDB/mmCIF entries often store a ligand as a separate label chain while the
    ligand still shares the polymer's author chain. This helper exposes that
    relationship as structured data so agents do not have to infer it from the
    raw chain tables.
    """
    anchor_types = anchor_chain_types or ASSOCIATED_LIGAND_ANCHOR_TYPES
    chain_list = list(chains)
    anchors_by_author: dict[str, list[dict[str, str]]] = {}
    for chain in chain_list:
        chain_type = str(chain.get("chain_type") or "")
        author_chain = str(chain.get("author_chain") or "").strip()
        chain_id = str(chain.get("chain_id") or "").strip()
        if not (author_chain and chain_id and chain_type in anchor_types):
            continue
        anchors_by_author.setdefault(author_chain, []).append(
            {"chain_id": chain_id, "chain_type": chain_type}
        )

    candidates: list[dict[str, Any]] = []
    for chain in chain_list:
        if str(chain.get("chain_type") or "") != "ligand":
            continue
        author_chain = str(chain.get("author_chain") or "").strip()
        unique_id = str(chain.get("unique_id") or "").strip()
        ligand_chain_id = str(chain.get("chain_id") or "").strip()
        anchors = anchors_by_author.get(author_chain) or []
        if not (author_chain and unique_id and ligand_chain_id and anchors):
            continue
        residues = _residue_names(chain)
        candidates.append(
            {
                "author_chain": author_chain,
                "ligand_chain_id": ligand_chain_id,
                "unique_id": unique_id,
                "residue_names": residues,
                "resname": residues[0] if len(residues) == 1 else None,
                "resnum": chain.get("resnum"),
                "num_atoms": chain.get("num_atoms"),
                "num_residues": chain.get("num_residues"),
                "associated_chain_ids": [item["chain_id"] for item in anchors],
                "associated_chain_types": sorted(
                    {item["chain_type"] for item in anchors}
                ),
                "recommended_select_chains_add": [ligand_chain_id],
                "recommended_include_ligand_ids": [unique_id],
            }
        )

    return sorted(
        candidates,
        key=lambda item: (
            str(item.get("author_chain") or ""),
            str(item.get("ligand_chain_id") or ""),
            str(item.get("unique_id") or ""),
        ),
    )


def classify_additive_resnames(resnames: Iterable[str]) -> dict[str, Any]:
    """Classify a set of ligand residue names into additive / placeholder buckets.

    Returns a dict with ``likely_additive`` resnames (mapped to a coarse reason)
    and the subset that are unparameterizable placeholders (``UNX``/``UNL``/
    ``UNK``). A ligand chain whose residue names fall entirely in these buckets
    is not part of the biological system.
    """
    names = [str(name).strip().upper() for name in resnames if str(name).strip()]
    additives = {
        name: LIKELY_ADDITIVE_RESNAMES[name]
        for name in names
        if name in LIKELY_ADDITIVE_RESNAMES
    }
    placeholders = sorted(
        {name for name in names if name in UNPARAMETRIZABLE_PLACEHOLDER_RESNAMES}
    )
    return {
        "likely_additive": additives,
        "unparametrizable_placeholders": placeholders,
    }


def likely_additive_ligands(
    chains: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return ligand-type chains whose residues are all additives/placeholders.

    Each entry carries enough identity (``chain_id``, ``author_chain``,
    ``unique_id``, ``resname``) plus a coarse ``reason`` and an
    ``unparametrizable`` flag so callers can either drop the component or, for
    placeholder residues, block before topology.
    """
    flagged: list[dict[str, Any]] = []
    for chain in chains:
        if str(chain.get("chain_type") or "") != "ligand":
            continue
        residues = _residue_names(chain)
        if not residues:
            continue
        classified = classify_additive_resnames(residues)
        additive = classified["likely_additive"]
        placeholders = classified["unparametrizable_placeholders"]
        # Only flag chains that are *entirely* additive/placeholder; a real
        # cofactor sharing a chain with an additive should not be dropped.
        residue_set = {str(name).strip().upper() for name in residues}
        if not residue_set <= set(additive):
            continue
        reasons = sorted(set(additive.values()))
        flagged.append(
            {
                "chain_id": str(chain.get("chain_id") or "").strip() or None,
                "author_chain": str(chain.get("author_chain") or "").strip()
                or None,
                "unique_id": str(chain.get("unique_id") or "").strip() or None,
                "residue_names": sorted(residue_set),
                "resname": sorted(residue_set)[0] if len(residue_set) == 1 else None,
                "reason": reasons[0] if len(reasons) == 1 else "additive",
                "reasons": reasons,
                "unparametrizable": bool(placeholders),
            }
        )
    return sorted(flagged, key=lambda item: str(item.get("unique_id") or ""))


def associated_ligands_by_author_chain(
    candidates: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        author_chain = str(candidate.get("author_chain") or "").strip()
        if not author_chain:
            continue
        grouped.setdefault(author_chain, []).append(dict(candidate))
    return grouped


def selected_associated_ligand_candidates(
    chains: Iterable[dict[str, Any]],
    selected_chain_ids: set[str],
    *,
    exclude_ligand_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Return associated ligand candidates omitted by a current chain selection."""
    selected = {str(chain_id) for chain_id in selected_chain_ids}
    excluded = {
        str(item).strip()
        for item in (exclude_ligand_ids or [])
        if str(item).strip()
    }
    chain_list = list(chains)
    selected_anchor_authors = {
        str(chain.get("author_chain") or "").strip()
        for chain in chain_list
        if str(chain.get("chain_id") or "").strip() in selected
        and str(chain.get("chain_type") or "") in ASSOCIATED_LIGAND_ANCHOR_TYPES
    }
    candidates = []
    for candidate in associated_ligand_candidates(chain_list):
        if candidate.get("author_chain") not in selected_anchor_authors:
            continue
        if candidate.get("ligand_chain_id") in selected:
            continue
        if candidate.get("unique_id") in excluded:
            continue
        candidates.append(candidate)
    return candidates
