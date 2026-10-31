"""A standard amino acid deposited on its own, not as a residue of a chain.

Free L-arginine in 6MJ7 is its own entity: the split reads ``ARG`` as a
one-residue protein component, so ``--include-ligand-resnames ARG`` finds no
ligand, and a one-residue protein chain has no Amber template (it would need
NH3+ and COO- on the same residue). It fails late, in ``build_amber_system``,
with OpenMM's "No template found for residue ... (ARG)". The designed route
is a ``--ligand-components`` declaration, which needs a residue name that is
not an amino-acid name and an isomeric SMILES. Every model of campaign
test-v6-cli spent turns guessing both (105_ligand_6mj7: 26 of 30 attempts hit
the topology failure, four qwen attempts ran out of time on the guesses and
one submitted a neutral-amine/neutral-acid cation the request did not mean).
These helpers give the refusal the declaration to copy.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

# L-amino acid zwitterions in their standard pH-7 states (Asp/Glu -1, Lys/Arg
# +1, His neutral with the N-epsilon proton, Cys/Tyr neutral). The template is
# ``[NH3+][C@@H](<side chain>)C(=O)[O-]`` -- the L configuration for every
# residue, including cysteine (R by CIP).
_SIDE_CHAINS: dict[str, str] = {
    "ALA": "C",
    "ARG": "CCCNC(N)=[NH2+]",
    "ASN": "CC(N)=O",
    "ASP": "CC(=O)[O-]",
    "CYS": "CS",
    "GLN": "CCC(N)=O",
    "GLU": "CCC(=O)[O-]",
    "HIS": "Cc1c[nH]cn1",
    "ILE": "[C@@H](C)CC",
    "LEU": "CC(C)C",
    "LYS": "CCCC[NH3+]",
    "MET": "CCSC",
    "PHE": "Cc1ccccc1",
    "SER": "CO",
    "THR": "[C@@H](C)O",
    "TRP": "Cc1c[nH]c2ccccc12",
    "TYR": "Cc1ccc(O)cc1",
    "VAL": "C(C)C",
}

FREE_AMINO_ACID_SMILES: dict[str, str] = {
    name: f"[NH3+][C@@H]({side})C(=O)[O-]" for name, side in _SIDE_CHAINS.items()
}
FREE_AMINO_ACID_SMILES["GLY"] = "[NH3+]CC(=O)[O-]"
FREE_AMINO_ACID_SMILES["PRO"] = "[O-]C(=O)[C@@H]1CCC[NH2+]1"

FREE_AMINO_ACID_CHARGE: dict[str, int] = {
    name: (1 if name in ("ARG", "LYS") else -1 if name in ("ASP", "GLU") else 0)
    for name in FREE_AMINO_ACID_SMILES
}


_ONE_LETTER = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q", "GLU": "E",
    "GLY": "G", "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F",
    "PRO": "P", "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


def ligand_residue_name(resname: str) -> str:
    """A residue name for the ligand that no protein/water/ion template owns,
    distinct per amino acid (ARG -> ARL, ASN -> ANL, ASP -> ADL)."""
    from mdclaw.chemistry_constants import AMINO_ACIDS, WATER_NAMES, is_standard_bare_ion_resname

    candidate = f"{resname[:1]}{_ONE_LETTER.get(resname, resname[1:2])}L"
    if (candidate in AMINO_ACIDS or candidate in WATER_NAMES
            or is_standard_bare_ion_resname(candidate)):
        return "LIG"
    return candidate


def _first_residue(chain: dict[str, Any]) -> str | None:
    """``"204 ARG"`` from a split record (``delivered_range.first`` / ``first``)."""
    delivered = chain.get("delivered_range")
    first = delivered.get("first") if isinstance(delivered, dict) else chain.get("first")
    return first if isinstance(first, str) and " " in first.strip() else None


def _chain_residue_name(chain: dict[str, Any]) -> str | None:
    names = chain.get("residue_names")
    if isinstance(names, dict):
        names = names.get("unique_residues")
    if isinstance(names, (list, tuple)) and len(names) == 1:
        return str(names[0]).upper()
    first = _first_residue(chain)
    return first.split()[-1].upper() if first else None


def free_amino_acid_chains(chains: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One-residue protein components that are a standard amino acid.

    Accepts either inspection chain records (``num_residues``,
    ``residue_names``, ``resnum``) or split ``chain_file_info`` records
    (``residue_count``, ``delivered_range.first`` = "204 ARG").
    """
    chains = list(chains or [])
    # The selection names a chain the way route_ligand_components resolves
    # it: the author chain when no other protein component shares it (a PDB
    # input, whose label ids are Gemmi's synthetic "Bx1"), else the label
    # chain (6MJ7's free ARG is author A like the protein, label E).
    authors: dict = {}
    for chain in chains:
        if chain.get("chain_type") == "protein" and chain.get("author_chain"):
            authors.setdefault(chain["author_chain"], set()).add(chain.get("chain_id"))
    found = []
    for chain in chains:
        if chain.get("chain_type") != "protein":
            continue
        count = next((chain[key] for key in ("num_residues", "residue_count", "count")
                      if chain.get(key) is not None), None)
        if count != 1:
            continue
        resname = _chain_residue_name(chain)
        if resname not in FREE_AMINO_ACID_SMILES:
            continue
        resnum = chain.get("resnum")
        first = _first_residue(chain)
        if resnum is None and first:
            resnum = first.split()[0]
        try:
            resnum = int(str(resnum))
        except (TypeError, ValueError):
            continue
        author = chain.get("author_chain")
        site = {
            "chain_id": chain.get("chain_id"),
            "author_chain": author,
            "resnum": resnum,
            "resname": resname,
            "selection_chain": (author if author and len(authors.get(author, ())) == 1
                                else chain.get("chain_id")),
        }
        if site not in found:
            found.append(site)
    return found


def deposited_as_free_molecule(pdb_file: str) -> bool:
    """Whether a one-residue component carries its own carboxylate oxygen
    (OXT): the deposit modelled a complete free amino acid (6MJ7's ARG 204),
    not a one-residue stub or fragment of a chain."""
    try:
        with open(pdb_file) as handle:
            return any(line.startswith(("ATOM", "HETATM")) and line[12:16].strip() == "OXT"
                       for line in handle)
    except OSError:
        return False


def ligand_component_declaration(site: dict[str, Any]) -> dict[str, str]:
    """The ``--ligand-components`` entry that turns ``site`` into a ligand."""
    chain = site.get("selection_chain") or site["chain_id"]
    return {
        "selection": f"{chain}:{site['resnum']}-{site['resnum']}",
        "residue_name": ligand_residue_name(site["resname"]),
        "smiles": FREE_AMINO_ACID_SMILES[site["resname"]],
    }


def free_amino_acid_sites_for_declarations(declarations, chains) -> list[dict[str, Any]]:
    """Free amino acids among ``chains`` that a ``--ligand-components`` list selects."""
    from .residue_range import parse_residue_ranges

    sites = free_amino_acid_chains(chains)
    picked = []
    for spec in declarations if isinstance(declarations, list) else []:
        if not isinstance(spec, dict):
            continue
        named = str(spec.get("residue_name") or "").strip().upper()
        for site in sites:
            if named == site["resname"] and site not in picked:
                picked.append(site)
        selection = spec.get("selection")
        try:
            spans = parse_residue_ranges([selection]) if isinstance(selection, str) else []
        except (TypeError, ValueError):
            spans = []
        for span in spans:
            for site in sites:
                if (span.chain in {site["chain_id"], site["author_chain"]}
                        and span.contains(site["resnum"], "") and site not in picked):
                    picked.append(site)
    return picked


def free_amino_acid_next_action(sites: list[dict[str, Any]], *, reason: str, pending: bool = True) -> str:
    """Spell the complete change: chain selection, declaration and charges."""
    declarations = [ligand_component_declaration(site) for site in sites]
    charges = {d["residue_name"]: FREE_AMINO_ACID_CHARGE[s["resname"]]
               for d, s in zip(declarations, sites)}
    named = ", ".join(
        f"{s['resname']} {s['author_chain'] or s['chain_id']}:{s['resnum']}"
        + (f" (label chain {s['chain_id']})"
           if s.get("selection_chain") == s.get("chain_id") and s.get("chain_id") != s.get("author_chain")
           else "")
        for s in sites)
    chains = " ".join(sorted({str(s.get("selection_chain") or s["chain_id"]) for s in sites
                              if s.get("selection_chain") or s.get("chain_id")}))
    return (
        f"{named} {reason}: it is a free amino acid, not a residue of a chain, and a one-residue "
        "protein chain has no Amber template. Simulate it as a ligand: add its chain "
        f"({chains}) to --select-chains, keep protein in --include-types, drop it from "
        "--include-ligand-resnames / --include-ligand-ids, and pass "
        f"--ligand-components '{json.dumps(declarations)}' "
        f"--ligand-net-charges '{json.dumps(charges)}' "
        "(the SMILES is the L zwitterion in its pH-7 state; change it only if the request asks "
        "for another protonation state), then "
        + ("run the same (still pending) node again." if pending else
           "run it on a new prep node branched from the same source (this one is sealed).")
    )
