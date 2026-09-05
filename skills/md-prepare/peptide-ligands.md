# A peptide represented as one ligand

Use this branch only when the request explicitly asks for a peptide to be one
ligand residue. `inspect_molecules` describes its input chemistry as `protein`;
that does not override the requested representation. Do not automatically
convert short proteins or infer a ligand merely from chain length.

Keep the original source node. On a new prep node, pass `--ligand-components`
as a JSON list, with one `selection`, `residue_name` and isomeric `smiles` per
component. Select both the receptor and the peptide chains. Example shape
(replace all placeholders from the request/source inspection):

```bash
mdclaw create_node --job-dir <job_dir> --node-type prep
mdclaw explain_node --job-dir <job_dir> --node-id <returned_prep_id>
mdclaw --job-dir <job_dir> --node-id <returned_prep_id> prepare_complex \
  --solvent-type explicit --select-chains <receptor_chain> <peptide_chain> \
  --include-types protein \
  --ligand-components '[{"selection":"B:1-7","residue_name":"LIG","smiles":"<complete isomeric SMILES>"}]'
```

`include-types` selects input types: the explicit declaration routes this
protein-classified component through `clean_ligand` even when ordinary ligands
are excluded. Use the usual ligand selectors separately for any other cofactors.
Do not select the new output name using `include-ligand-resnames`: it does not
exist in the input yet.

The declared range must cover the complete source subchain. Cutting a fragment,
inventing caps, or dropping an external covalent bond is not this operation.
Missing atoms/ambiguous chemistry require a corrected, explicit source/SMILES,
not renumbering or manual reconstruction to pass a check. Keep ligand optimization
off; heavy-atom placement must survive this representation-only operation.

Read `source_conversion` in `ligand_chemistry.json` and `chain_identity_map.json`:
the tool validates chemical identity and records source/prepared/merged heavy
atom indices. Continue through the usual DAG; no hand-built PDB or direct
topology override is needed. If required chemical intent is absent, ask the user
(also in autonomous mode); never guess a peptide's modified residues or caps.
