# Prepare Complex

Create a `prep` node after `source` and run `prepare_complex`. The
`--solvent-type` value comes from the study-level `solvent_regime`:
`explicit` for explicit-water and membrane workflows, `implicit` for GB, and
`vacuum` for deliberate no-solvent topologies.

```bash
mdclaw create_node --job-dir <job_dir> --node-type prep
mdclaw explain_node --job-dir <job_dir> --node-id <prep_node_id>
mdclaw --job-dir <job_dir> --node-id <prep_node_id> prepare_complex \
  --solvent-type explicit \
  --select-chains A \
  --include-types protein nucleic glycan ligand
```

In node mode, `structure_file` resolves from the source ancestor's normalized
candidate files. Do not pass `--source-node-id`; the prep node's parent edge is
the source selection. If `source_bundle.json` lists more than one candidate,
add `--source-candidate-id <candidate_id>` to the validated
`prepare_complex` command.

For NMR-style model numbering, `--source-model-index 2` selects the second
model-derived candidate.

For the default explicit-solvent path, retain standard bare crystallographic
ions when they are part of the requested system by including `ion` in
`--include-types`. With the default OPC water XML this includes common ions such
as NA, CL, K, MG, CA, MN, and ZN; non-OPC water models can differ and are
checked later by `build_amber_system` against the active water XML. For implicit
solvent, pass `--solvent-type implicit`; `prepare_complex` will exclude
explicit ion components from `merged_pdb` and record them in
`component_disposition.json`. For a deliberate vacuum/no-solvent topology,
explicit ions may be retained.

For chain-associated ligands, use `inspect_molecules.associated_ligand_candidates`.
If the task names a target residue/cofactor such as `NDP`, `ATP`, or `AP5`,
prefer residue-name scoped selection by adding `--select-chains A B
--include-types protein ligand --include-ligand-resnames NDP` to the validated
run command.

This selects matching associated ligands even when their ligand label chain IDs
differ from the selected protein chain IDs. If the exact instance matters, use
the returned `ligand_selection.recommended_include_ligand_ids` with
`--include-ligand-ids`. Use `--include-associated-ligands` only when all listed
same-author ligand candidates should be included. Omit `ligand` from
`--include-types` for a ligand-free task. Do not retry unchanged.

Crystallization additives (`EOH`, `GOL`, `PEG`/`2PE`/`PG4`, `MPD`, `SO4`,
`ACT`, ...) and unknown residues (`UNX`, `UNL`, `UNK`) are swept into `ligand`
by the default `--include-types` and then fail here or at topology with
`No template found for residue <RESNAME>`. Triage them first
(`skills/md-prepare/inspection-and-chains.md`); the safe default is to omit
`ligand` and keep only `protein`/`nucleic`/`glycan`/`ion`.

For a ligand-free system, use `--select-chains A --include-types protein
nucleic glycan --no-process-ligands` in the validated run command.

Do not express "no ligands" as `--include-ligand-ids []` or as a bare
`--include-ligand-ids` flag. Omit the flag entirely unless one or more ligand
IDs are being included.

If `--include-ligand-ids` is wrong, `split_molecules` fails with
`requested_ligand_ids_not_found` and lists the available ligand `unique_id`
values. Rerun a new prep node with one of those IDs, or use
`--include-ligand-resnames <RESNAME>` when the task names a residue/cofactor and
all matching associated instances should be retained.

Important outputs:

- `merged_pdb`: downstream structure for solvation or topology.
- `split/`: extracted components.
- `ligand_chemistry`: ligand SDF/SMILES/provenance, including protonation
  provenance (`protonation_method`, `protonation_ph`, `smiles_protonated`).
  Neutral ligand SMILES are protonated at the protein `--ph` via Dimorphite-DL
  by default; override with `--ligand-ph`, disable with
  `--no-protonate-ligands`. An explicitly charged SMILES (`[O-]`/`[NH3+]`) or a
  known `net_charge` takes precedence (charge selects the matching state).
- `residue_mapping`: source-to-merged nucleic residue mapping.
- `glycan_metadata` and `glycan_linkages`: GLYCAM topology inputs.

`prepare_complex` records ligand chemistry. `build_amber_system` handles
topology and ligand partial charges.

If ligand chemistry preparation returns a blocking structured result, do not
retry the same command. Follow `workflow_recommendation.options`.

When a ligand fails chemistry but the protein/nucleic/glycan side succeeded,
`prepare_complex` returns `overall_status="completed_with_blocking_ligand_failure"`
with `code="blocking_ligand_failure"` and a protein-only `merged_pdb`. This is
**not** an `unhandled_error`: do not "fix and retry" the same command. Branch on
`workflow_recommendation.options` — provide the ligand SMILES/SDF and rerun a new
prep node, exclude the ligand (`ligand` omitted from `--include-types`) and
continue protein-only, or stop. A common trigger is a crystallization additive
(e.g. `EOH`, `GOL`) that has no CCD/SMILES match and cannot be templated; the
right move is almost always to exclude it.

`prepare_complex` preflights the retained ligands against a curated additive
list. Placeholder residues (`UNX`/`UNL`/`UNK`) have no chemistry and block with
`code="unparametrizable_ligand_selected"`; follow
`workflow_recommendation.options` (drop `ligand`, or name the real target).
Known additives/buffers (glycerol, PEG, sulfate, ...) do not block the preflight
but populate `warnings` and `likely_additive_ligands`. If you keep them anyway
and their chemistry cannot be resolved, the run ends in
`code="blocking_ligand_failure"` (above). Rerun with `ligand` omitted from
`--include-types` unless the additive is intentional and you can supply its
chemistry.

After `prepare_complex` succeeds, verify the completed node before solvation:

- If the user requested no ligand, confirm the prep node has no
  `artifacts.ligand_chemistry`.
- If the wrong ligand or chain choice was used, create a new prep node from
  the same source ancestor. Do not rerun the existing prep node with changed
  molecular contents.
