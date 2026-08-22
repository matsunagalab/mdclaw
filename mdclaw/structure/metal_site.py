"""Metal sites, and the protonation they force on the side chains around them.

Protonation here came from pdb2pqr/propka alone, which does not know a metal is
present.  Measured on RCSB 6W9C / 6WRH / 4OW0 (2026-08-22), that leaves a
four-cysteine structural zinc with a single thiolate: propka happens to depress
one cysteine's pKa far enough and the other three stay neutral.  A neutral thiol
has no reason to stay on a +2 ion, and it does not -- across 1 ns of production
the three unbound sulfurs sit 5 to 12 A from the zinc while the one thiolate
holds at 2.03 +/- 0.06 A.

The reference simulations make the same mistake in a milder form: MDDB's own
``topology.prmtop`` for all three projects deprotonates two of the four and
holds the other two at 2.05 A while the rest leave.

What this module does is deliberately narrow.  A cysteine ligating a metal is a
thiolate -- that is not a judgement call for Zn, Fe, Cu, Cd or Hg -- so those
are assigned.  A histidine ligand is reported and never assigned, because which
nitrogen coordinates decides the tautomer and that cannot be read off a
distance.  Anything else is reported and never assigned.

Assigning the thiolates is necessary and not sufficient.  A nonbonded 12-6 ion
holds as many ligands as its charge attracts and cannot impose tetrahedral
geometry; keeping a Cys4 site intact needs a bonded model (MCPB.py, or published
ZAFF parameters).  This module does not attempt one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

# Metals whose coordination sphere decides the protonation around it.
METAL_ELEMENTS = frozenset({
    "ZN", "FE", "CU", "NI", "CO", "MN", "CD", "HG", "PT", "AU", "AG", "MO", "W",
})

# Longest metal-ligand separation that still counts as coordination.  Zn-S is
# 2.3 A, but deposited sites are not that tidy: in 6W9C the same Cys4 zinc
# measures 2.19/2.53/3.21 A in chain A and 2.48/2.85/2.57 A in chain C, so a
# 3.0 A limit splits one site into coordinated and not.
METAL_LIGAND_ANGSTROM = 3.5

# A site has to be established before the loose limit is allowed to extend it.
# 3.5 A on its own would call any metal with one cysteine 3.4 A away a site and
# deprotonate that cysteine; requiring one contact at coordination distance and
# two ligands in total keeps the loose limit for what it is for -- the distorted
# outer ligand of a real site, 3.21 A in 6W9C chain A.
METAL_CORE_ANGSTROM = 2.9
MINIMUM_SITE_LIGANDS = 2

# Side-chain donors, by residue.  Backbone nitrogen is excluded on purpose: it
# is never a metal ligand in a protein, and counting it turns one zinc into a
# dozen "ligands" made of the amide nitrogens of the surrounding loop.
SIDECHAIN_DONORS = {
    "CYS": ("SG",), "CYM": ("SG",), "CYX": ("SG",),
    "HIS": ("ND1", "NE2"), "HID": ("ND1", "NE2"), "HIE": ("ND1", "NE2"),
    "HIP": ("ND1", "NE2"),
    "ASP": ("OD1", "OD2"), "GLU": ("OE1", "OE2"),
    "MET": ("SD",), "SER": ("OG",), "THR": ("OG1",), "TYR": ("OH",),
    "ASN": ("OD1",), "GLN": ("OE1",), "LYS": ("NZ",),
}

# The only assignment this module makes.  A metal-bound cysteine is a thiolate.
THIOLATE_STATE = "CYM"


def detect_metal_sites(structure_path: Path,
                       cutoff: float = METAL_LIGAND_ANGSTROM,
                       select_chains=None) -> list[dict]:
    """Every metal ion and the side chains that ligate it.

    Read-only.  Each site carries its ligands, the protonation states this
    module is willing to assign, and the ligands it is deliberately leaving
    alone so a caller can surface them for confirmation.

    ``select_chains`` drops sites and ligands outside the selection, because a
    deposit's other copies are not in the system being built: 6W9C carries three
    of them plus a second zinc shared between all three at Cys270, and without
    the filter every copy's cysteines would be assigned.
    """
    selected = set(select_chains) if select_chains else None
    try:
        import gemmi
    except ImportError:
        logger.warning(
            "gemmi is unavailable, so metal sites cannot be detected; metal-bound "
            "cysteines will keep whatever state pdb2pqr assigns them")
        return []

    sites: list[dict] = []
    try:
        suffix = Path(structure_path).suffix.lower()
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            structure = gemmi.make_structure_from_block(doc[0])
        else:
            structure = gemmi.read_pdb(str(structure_path))
        model = structure[0]

        donors = []
        for chain in model:
            for residue in chain:
                if selected is not None and chain.name not in selected:
                    continue
                for name in SIDECHAIN_DONORS.get(residue.name.strip().upper(), ()):
                    atom = residue.find_atom(name, "*")
                    if atom is not None:
                        donors.append((chain.name, residue.seqid.num,
                                       residue.name.strip().upper(), name, atom.pos))

        for chain in model:
            for residue in chain:
                if selected is not None and chain.name not in selected:
                    continue
                for atom in residue:
                    element = (atom.element.name or "").strip().upper()
                    if element not in METAL_ELEMENTS:
                        continue
                    ligands = []
                    for donor_chain, resnum, resname, name, pos in donors:
                        distance = atom.pos.dist(pos)
                        if distance <= cutoff:
                            ligands.append({
                                "chain": donor_chain, "resnum": resnum,
                                "resname": resname, "atom": name,
                                "distance_angstrom": round(distance, 2),
                            })
                    if not ligands:
                        continue
                    ligands.sort(key=lambda item: item["distance_angstrom"])
                    sites.append(_classify(
                        element, chain.name, residue.seqid.num, ligands,
                        resname=residue.name.strip().upper(),
                        icode=str(residue.seqid.icode or "").strip(),
                    ))
    except Exception as exc:                                        # noqa: BLE001
        logger.warning(
            f"Metal site detection failed on {structure_path}: {type(exc).__name__}: "
            f"{exc}; metal-bound cysteines will keep whatever state pdb2pqr assigns")
        return []
    return sites


def _classify(element: str, chain: str, resnum: int, ligands: list[dict],
              resname: str = "", icode: str = "") -> dict:
    """Split a site's ligands into the ones we assign and the ones we report."""
    established = (len(ligands) >= MINIMUM_SITE_LIGANDS
                   and ligands[0]["distance_angstrom"] <= METAL_CORE_ANGSTROM)
    assigned, deferred, seen = [], [], set()
    for ligand in ligands:
        key = (ligand["chain"], ligand["resnum"])
        if key in seen:                      # His offers two nitrogens; one site
            continue
        seen.add(key)
        # CYX is already spoken for: it holds a disulfide, and turning it into a
        # thiolate would silently break that bond.
        if established and ligand["resname"] in ("CYS", "CYM"):
            assigned.append({
                "chain": ligand["chain"], "resnum": str(ligand["resnum"]),
                "state": THIOLATE_STATE,
                "reason": (f"ligates {element}{resnum} at "
                           f"{ligand['distance_angstrom']} A"),
            })
        else:
            deferred.append(ligand)

    cysteines = len(assigned)
    others = len(deferred)
    if others == 0 and cysteines >= 2:
        motif = f"{element}-Cys{cysteines}"
    elif cysteines >= 1:
        motif = f"{element}-Cys{cysteines}+{others} other"
    else:
        motif = f"{element}-{others} non-cysteine ligand(s)"

    return {
        "established": established,
        "element": element,
        # The residue name, not the element: a site written as FE2 or ZN2 has an
        # element of FE or ZN, and the split writes the residue name.
        "resname": resname or element,
        "icode": icode,
        "chain": chain,
        "resnum": resnum,
        "label": f"{element} {chain}{resnum}",
        "motif": motif,
        "ligands": ligands,
        "protonation_states": assigned,
        "deferred_ligands": deferred,
    }


def protonation_states_for_metal_sites(sites: list[dict]) -> list[dict]:
    """Flatten the assignments, one per residue, deduplicated across sites."""
    out, seen = [], set()
    for site in sites:
        for state in site["protonation_states"]:
            key = (state["chain"], state["resnum"])
            if key in seen:
                continue
            seen.add(key)
            out.append(state)
    return out


def describe_sites(sites: list[dict]) -> list[str]:
    """One human-readable line per site, for logs and confirmation reports."""
    lines = []
    for site in sites:
        ligands = ", ".join(
            f"{item['chain']}:{item['resname']}{item['resnum']}:{item['atom']} "
            f"{item['distance_angstrom']} A" for item in site["ligands"])
        line = f"{site['label']} ({site['motif']}): {ligands}"
        if site["deferred_ligands"]:
            names = ", ".join(f"{item['chain']}:{item['resname']}{item['resnum']}"
                              for item in site["deferred_ligands"])
            line += (f" -- protonation left to pdb2pqr for {names}: which donor "
                     "coordinates decides the state and a distance does not say")
        lines.append(line)
    return lines
