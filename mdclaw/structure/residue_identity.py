"""Source polymer identity, independent of coordinate numbering and presence."""

from difflib import SequenceMatcher

from .residue_range import resolve_ordered_ranges


def canonical_name(name):
    from .protonation import _PRESERVABLE_INPUT_PROTONATION_BASES

    return {"CYX": "CYS", "CYM": "CYS", "MSE": "MET"}.get(
        name, _PRESERVABLE_INPUT_PROTONATION_BASES.get(name, name)
    )


def chain_residues(subchain):
    return [
        {"number": r.seqid.num, "icode": str(r.seqid.icode).strip(),
         "name": canonical_name(r.name), "sequence_position": r.label_seq,
         "observed": True}
        for r in subchain if r.name not in {"ACE", "NME", "NMA"}
    ]


def selection_identity(structure, subchain, ranges, block=None):
    """Select source rows in polymer order; never invent integer-numbered sites.

    mmCIF supplies explicit author/sequence correspondence. For PDB, Gemmi's
    SEQRES alignment supplies sequence positions, but unobserved author IDs
    remain unknown. Without either, only coordinate identity is verifiable.
    """
    observed = chain_residues(subchain)
    rows = []
    if block is not None:
        table = block.find("_pdbx_poly_seq_scheme.", [
            "asym_id", "seq_id", "mon_id", "pdb_seq_num", "pdb_ins_code"])
        present = {(r["number"], r["icode"]) for r in observed}
        for row in table:
            if row[0] != subchain.subchain_id():
                continue
            number = int(row[3]) if row[3].lstrip("-").isdigit() else None
            icode = "" if row[4] in {".", "?"} else row[4].strip()
            rows.append({"number": number, "icode": icode,
                         "name": canonical_name(row[2]),
                         "sequence_position": int(row[1]),
                         "observed": (number, icode) in present})
    evidence = "polymer_scheme" if rows else "coordinates_only"
    if not rows:
        entity = next((e for e in structure.entities
                       if subchain.subchain_id() in e.subchains), None)
        if entity and entity.full_sequence and all(r["sequence_position"] for r in observed):
            by_position = {r["sequence_position"]: r for r in observed}
            rows = [by_position.get(i, {
                "number": None, "icode": "", "name": canonical_name(name),
                "sequence_position": i, "observed": False})
                for i, name in enumerate(entity.full_sequence, 1)]
            evidence = "sequence_alignment"
        else:
            rows = observed
    identified = [(i, r) for i, r in enumerate(rows) if r["number"] is not None]
    resolution = resolve_ordered_ranges(
        ranges, [(r["number"], r["icode"]) for _, r in identified])
    selected = set()
    unresolved = []
    for span in ranges:
        item = resolution[span.spelled()]
        indices = [identified[i][0] for i in sorted(item["indices"])]
        if indices:
            selected.update(range(indices[0], indices[-1] + 1))
        for end, found in ((span.start, item["start_observed"]),
                           (span.end, item["end_observed"])):
            if not found:
                unresolved.append(f"{end[0]}{end[1]}")
    selected_rows = [rows[i] for i in sorted(selected)]
    atoms = {(r.seqid.num, str(r.seqid.icode).strip()): r for r in subchain}
    bonds = []
    for i, (left, right) in enumerate(zip(selected_rows, selected_rows[1:])):
        position = left["sequence_position"]
        if position is None or right["sequence_position"] != position + 1:
            continue
        a = atoms.get((left["number"], left["icode"]))
        b = atoms.get((right["number"], right["icode"]))
        carbon = a.find_atom("C", "*") if a is not None else None
        nitrogen = b.find_atom("N", "*") if b is not None else None
        bonds.append({"left_index": i, "source_distance_angstrom":
                      carbon.pos.dist(nitrogen.pos) if carbon and nitrogen else None})
    return {"evidence": evidence, "residues": selected_rows, "peptide_bonds": bonds,
            "sequence_completeness": "unknown" if evidence == "coordinates_only" else "mapped",
            "unresolved_endpoints": unresolved}


def compare_identity(expected, actual):
    """Audit ordered sequence and retain correspondence through renumbering.

    Alignment describes differences; it never excuses additions, deletions or
    substitutions. Exact sequence equality is required for a passing audit.
    """
    matcher = SequenceMatcher(None, [r["name"] for r in expected],
                              [r["name"] for r in actual], autojunk=False)
    missing, unexpected, mapping = [], [], []
    for tag, i, j, k, end in matcher.get_opcodes():
        if tag == "equal":
            mapping.extend({"source": source, "prepared": prepared}
                           for source, prepared in zip(expected[i:j], actual[k:end]))
        else:
            missing.extend(expected[i:j])
            unexpected.extend(actual[k:end])
    return {"requested": len(expected), "delivered": len(mapping),
            "missing": missing, "unexpected": unexpected, "mapping": mapping}


def audit_merged_identity(coverage, chain_identity_map, merged_file):
    """Use the existing component indices to audit and persist final identity."""
    import gemmi

    contracts = {c["output_file"]: c for record in coverage.values()
                 for c in record["components"]}
    residues = [r for chain in gemmi.read_pdb(str(merged_file))[0] for r in chain]
    failures = []
    seen = set()
    for component in chain_identity_map.get("components", []):
        contract = contracts.get(component["source_file"])
        if contract is None:
            continue
        seen.add(component["source_file"])
        fragment = residues[component["residue_index_start"]:
                            component["residue_index_end_exclusive"]]
        fragment = [r for r in fragment if r.name not in {"ACE", "NME", "NMA"}]
        expected = contract["residues"]
        audit = compare_identity(expected, chain_residues(fragment))
        audit["broken_peptide_bonds"] = []
        if not audit["missing"] and not audit["unexpected"]:
            for bond in contract.get("peptide_bonds", []):
                i = bond["left_index"]
                left, right = expected[i:i + 2]
                carbon, nitrogen = fragment[i].find_atom("C", "*"), fragment[i + 1].find_atom("N", "*")
                distance = carbon.pos.dist(nitrogen.pos) if carbon and nitrogen else None
                source_distance = bond["source_distance_angstrom"]
                # Do not relabel a deposited geometry outlier as damage caused
                # by preparation. 0.01 A allows PDB coordinate rounding only.
                unchanged = (distance is not None and source_distance is not None
                             and abs(distance - source_distance) <= 0.01)
                if distance is None or not (unchanged or 1.0 <= distance <= 2.0):
                    audit["broken_peptide_bonds"].append({"left": left, "right": right,
                                                         "distance_angstrom": distance})
        component["residue_identity"] = {"evidence": contract["evidence"],
                                         "source_peptide_bonds": contract.get("peptide_bonds", []), **audit}
        if audit["missing"] or audit["unexpected"] or audit["broken_peptide_bonds"]:
            failures.append(component["component_id"])
    failures.extend(sorted(set(contracts) - seen))
    return failures


def checked_structure_override(source_file, override_file, output_file):
    """Keep the source's polymer identity/sequence on coordinate-only overrides.

    A different construct belongs to a new source node, not an unrecorded prep
    override. Reattaching metadata prevents stripped SEQRES from disabling checks.
    """
    import gemmi

    source = gemmi.read_structure(str(source_file))
    override = gemmi.read_structure(str(override_file))
    for structure in (source, override):
        structure.setup_entities()
        if len(structure) != 1:
            raise ValueError("A prep coordinate override must contain exactly one selected model")

    def inventory(structure):
        return [(chain.name, r["number"], r["icode"], r["name"])
                for chain in structure[0] for subchain in chain.subchains()
                if subchain[0].entity_type == gemmi.EntityType.Polymer
                for r in chain_residues(subchain)]

    if inventory(source) != inventory(override):
        raise ValueError(
            "Explicit structure_file changes the source polymer identity. "
            "Keep source author IDs and sequence; use select_chains/residue_ranges "
            "for selection. Deliberate construct changes require a new source node."
        )
    def spans(structure):
        return {(chain.name, span[0].seqid.num, str(span[0].seqid.icode)): span
                for chain in structure[0] for span in chain.subchains()
                if span[0].entity_type == gemmi.EntityType.Polymer}

    labels, entities = {}, {}
    targets = spans(override)
    for key, source_span in spans(source).items():
        target_span = targets[key]
        labels[source_span.subchain_id()] = target_span.subchain_id()
        source_entity = source.get_entity_of(source_span)
        target_entity = override.get_entity_of(target_span)
        if source_entity and target_entity:
            target_entity.full_sequence = list(source_entity.full_sequence)
            entities[source_entity.name] = target_entity.name
    override.assign_label_seq_id(force=True)
    output_block = override.make_mmcif_document().sole_block()
    if str(source_file).lower().endswith((".cif", ".mmcif")):
        original = gemmi.cif.read(str(source_file)).sole_block()
        scheme = original.get_mmcif_category("_pdbx_poly_seq_scheme.")
        if scheme:
            scheme["asym_id"] = [labels.get(label, label) for label in scheme["asym_id"]]
            if "entity_id" in scheme:
                scheme["entity_id"] = [entities.get(entity, entity) for entity in scheme["entity_id"]]
            output_block.set_mmcif_category("_pdbx_poly_seq_scheme.", scheme)
    output_block.write_file(str(output_file))
    return str(output_file)
