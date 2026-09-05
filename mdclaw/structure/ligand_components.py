"""Explicit, source-checked conversion of a complete peptide component to LIG."""

from pathlib import Path
import re

from .residue_range import parse_residue_ranges


def route_ligand_components(source_file, split, declarations):
    """Route selected complete source subchains to the existing ligand cleaner.

    This is a representation change, never a fragmentation/capping operation.
    Source resolution and selection have already run; no replacement input or
    source metadata is discarded to make a declaration fit.
    """
    import gemmi
    from mdclaw.chemistry_constants import AMINO_ACIDS, WATER_NAMES, is_standard_bare_ion_resname

    if not isinstance(declarations, list):
        raise ValueError("ligand_components must be a list of declarations")
    source = gemmi.read_structure(str(source_file))
    source.setup_entities()
    used = set()
    for spec in declarations:
        if not isinstance(spec, dict) or set(spec) != {"selection", "residue_name", "smiles"}:
            raise ValueError("each ligand component needs selection, residue_name and smiles")
        spans = parse_residue_ranges([spec["selection"]])
        name = spec["residue_name"]
        if len(spans) != 1 or not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{0,2}", name):
            raise ValueError("use one CHAIN:START-END and a 1--3 character residue name")
        if name in AMINO_ACIDS | WATER_NAMES or is_standard_bare_ion_resname(name):
            raise ValueError("ligand residue name conflicts with a protein, water or ion name")
        if not isinstance(spec["smiles"], str) or not spec["smiles"].strip():
            raise ValueError("a ligand component requires an explicit SMILES")
        span = spans[0]
        files = [info for info in split.get("chain_file_info", [])
                 if span.chain in {info["chain_id"], info.get("author_chain")}
                 and info["chain_type"] == "protein"]
        if len(files) != 1 or files[0]["file"] in used:
            raise ValueError("ligand selection must resolve to one selected, non-overlapping protein component")
        info = files[0]
        source_spans = [(chain.name, part) for chain in source[0] for part in chain.subchains()
                        if part.subchain_id() == info["chain_id"]]
        if len(source_spans) != 1:
            raise ValueError("ligand source subchain is not uniquely resolvable")
        chain, part = source_spans[0]
        sites = [(r.seqid.num, str(r.seqid.icode).strip()) for r in part]
        if not sites or sites[0] != span.start or sites[-1] != span.end:
            raise ValueError("ligand range must cover the complete source subchain; implicit bond cutting is forbidden")
        if not all(span.contains(*site) for site in sites):
            raise ValueError("ligand range does not cover its complete source subchain")
        selected = {(chain, *site) for site in sites}
        if Path(source_file).suffix.lower() in {".pdb", ".ent"}:
            from .pdb_utils import _read_pdb_conect_bonds

            # PDB CONECT is separate from Gemmi's LINK/SSBOND connections.
            serial_sites = {a.serial: (ch.name, r.seqid.num, str(r.seqid.icode).strip())
                            for ch in source[0] for r in ch for a in r}
            for line in Path(source_file).read_text().splitlines():
                if line.startswith("CONECT"):
                    for i in range(6, len(line), 5):
                        if line[i:i+5].strip():
                            int(line[i:i+5])  # Unsupported serial encoding fails closed.
            for a, b, _ in _read_pdb_conect_bonds(source_file):
                if a not in serial_sites or b not in serial_sites:
                    raise ValueError("source CONECT has an unresolved atom serial")
                if (serial_sites[a] in selected) != (serial_sites[b] in selected):
                    raise ValueError("ligand component has a source CONECT covalent link outside its selection")
        for connection in source.connections:
            if connection.type in {gemmi.ConnectionType.MetalC, gemmi.ConnectionType.Hydrog}:
                continue
            ends = [(p.chain_name, p.res_id.seqid.num, str(p.res_id.seqid.icode).strip())
                    for p in (connection.partner1, connection.partner2)]
            if (ends[0] in selected) != (ends[1] in selected):
                raise ValueError("ligand component has a source-declared covalent link outside its selection")
        actual = gemmi.read_structure(info["file"])
        def inventory(residues):
            return [(r.seqid.num, str(r.seqid.icode).strip(), r.name, a.name,
                     a.element.name, tuple(a.pos)) for r in residues for a in r if not a.is_hydrogen()]
        expected = inventory(part)
        observed = inventory([r for ch in actual[0] for r in ch])
        if len(expected) != len(observed) or any(
                a[:5] != b[:5] or max(abs(x-y) for x, y in zip(a[5], b[5])) > 0.002
                for a, b in zip(expected, observed)):
            raise ValueError("ligand split changed source heavy-atom identity or coordinates")
        used.add(info["file"])
        split["protein_files"].remove(info["file"])
        split["ligand_files"].append(info["file"])
        info.update(chain_type="ligand", ligand_component=dict(spec))


def validate_ligand_conversion(input_file, clean, declaration):
    """Check exact chemistry/coordinates and record each source heavy atom."""
    from rdkit import Chem

    source = Chem.MolFromPDBFile(str(input_file), sanitize=False, removeHs=False)
    output = Chem.SDMolSupplier(clean["sdf_file"], removeHs=False)[0]
    pdb = Chem.MolFromPDBFile(clean["pdb_file"], sanitize=False, removeHs=False)
    template = Chem.MolFromSmiles(declaration["smiles"])
    if any(m is None for m in (source, output, pdb, template)):
        raise ValueError("cannot read ligand source, prepared graph or declared SMILES")
    if any(tag == "?" for _, tag in Chem.FindMolChiralCenters(
            template, includeUnassigned=True, useLegacyImplementation=False)):
        raise ValueError("declared ligand stereochemistry is incomplete")
    # SDF stereo flags alone cannot excuse inverted deposited coordinates.
    Chem.RemoveStereochemistry(output)
    Chem.AssignStereochemistryFrom3D(output)
    if len(Chem.GetMolFrags(output)) != 1 or Chem.MolToSmiles(Chem.RemoveHs(output)) != Chem.MolToSmiles(Chem.RemoveHs(template)):
        raise ValueError("prepared ligand does not match the complete declared isomeric SMILES")
    original = [a for a in source.GetAtoms() if a.GetAtomicNum() != 1]
    prepared = [a for a in pdb.GetAtoms() if a.GetAtomicNum() != 1]
    if len(original) != len(prepared) or pdb.GetNumAtoms() != output.GetNumAtoms():
        raise ValueError("ligand conversion lost or added atoms")
    mapping = []
    for a, b in zip(original, prepared):
        old, new = a.GetPDBResidueInfo(), b.GetPDBResidueInfo()
        delta = source.GetConformer().GetAtomPosition(a.GetIdx()) - pdb.GetConformer().GetAtomPosition(b.GetIdx())
        if a.GetAtomicNum() != b.GetAtomicNum() or delta.Length() > 0.002:
            raise ValueError("ligand conversion changed source heavy atoms or their placement")
        mapping.append({"source": {"chain": old.GetChainId(), "resnum": old.GetResidueNumber(),
                                   "icode": old.GetInsertionCode().strip(), "resname": old.GetResidueName(),
                                   "atom": old.GetName().strip(), "atom_index": a.GetIdx()},
                        "prepared": {"chain": new.GetChainId(), "resnum": new.GetResidueNumber(),
                                     "resname": new.GetResidueName(), "atom": new.GetName().strip(),
                                     "atom_index": b.GetIdx()}})
    rows = [line for line in Path(clean["pdb_file"]).read_text().splitlines()
            if line.startswith(("ATOM  ", "HETATM"))]
    if len({line[17:27] for line in rows}) != 1 or len({line[12:16] for line in rows}) != len(rows):
        raise ValueError("ligand output must have one residue and unique atom names")
    return {"declaration": declaration, "source_file": input_file,
            "prepared_file": clean["pdb_file"], "heavy_atom_mapping": mapping,
            "added_hydrogens": sum(a.GetAtomicNum() == 1 for a in pdb.GetAtoms())
            - sum(a.GetAtomicNum() == 1 for a in source.GetAtoms())}


def audit_converted_merge(ligands, identity_map, merged_file):
    """Extend source correspondence to the final merged atom indices."""
    import gemmi

    merged = [a for chain in gemmi.read_structure(merged_file)[0] for r in chain for a in r]
    for ligand in ligands:
        conversion = ligand.get("source_conversion")
        if not conversion:
            continue
        components = [c for c in identity_map["components"]
                      if Path(c["source_file"]).resolve() == Path(ligand["pdb_file"]).resolve()]
        if len(components) != 1:
            raise ValueError("converted ligand has no unique merged component")
        component = components[0]
        original = [a for ch in gemmi.read_structure(ligand["pdb_file"])[0] for r in ch for a in r]
        start = component["atom_index_start"]
        actual = merged[start:component["atom_index_end_exclusive"]]
        if len(original) != len(actual) or any(
                a.name != b.name or a.element != b.element or a.pos.dist(b.pos) > 0.002
                for a, b in zip(original, actual)):
            raise ValueError("merge changed converted ligand atoms or placement")
        for record in conversion["heavy_atom_mapping"]:
            record["merged_atom_index"] = start + record["prepared"]["atom_index"]
        component["source_conversion"] = conversion
