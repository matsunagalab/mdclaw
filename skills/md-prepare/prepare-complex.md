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

Expand every omission into effective `--residue-ranges` first. All resulting ranges are separate components by default, including pieces created by "leave it out"; an enclosing range, shared source chain, nearby residue numbers, or physical plausibility never implies a peptide bond.
Use `--join-range-pieces` only when the request explicitly joins every piece of that chain; for only named boundaries, use `--join-range-groups 'A:29-173,A:183-227'`. Before topology, verify the returned component sizes match the requested construct.

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

Important outputs (the brief result stubs blocks larger than a few KB as
`{"_omitted": true, "see": "<result_file>#<key>"}`; read
`<job_dir>/nodes/<prep_node_id>/result.json` when such a block matters, and
report from `success`, `node_status`, `warnings` and `confirmation_needed`
first):

- `merged_pdb`: downstream structure for solvation or topology.
- `split/`: extracted components.
- `ligand_chemistry`: ligand SDF/SMILES/provenance, including protonation
  provenance (`protonation_method`, `protonation_ph`, `smiles_protonated`).
  Neutral ligand SMILES are protonated at the protein `--ph` via Dimorphite-DL
  by default; override with `--ligand-ph`, disable with
  `--no-protonate-ligands`. An explicitly charged SMILES (`[O-]`/`[NH3+]`) or a
  known expected `net_charge` takes precedence (charge selects the matching
  candidate and fails if none matches). When the task supplies an expected
  ligand charge, pass it without inventing a SMILES, for example
  `--structure-analysis '{"ligands":[{"resname":"AMH","net_charge":0}]}'`.
  If several candidates remain and no expected charge is known, report the
  ambiguity instead of treating visual inspection as chemical validation.
- `missing_residue_detection`: per chain, whether gaps could be detected at
  all. A chain whose input carries no reference sequence (SEQRES) reports
  `status="not_detectable"` — there zero gaps means "not checked", not "none
  present". Also lists the unresolved terminal residues that were deliberately
  left unmodeled, which are excluded from the repair on purpose.
- `missing_residue_repair`: per chain, how requested gaps were rebuilt, by which
  method, how many residues in how many segments, and for MODELLER the random
  seed and the template's checksum. Rebuilt residues are predicted coordinates;
  report them to the user rather than treating them as measured.
- `complex_missing_residue_repair`: present when the gaps of every selected
  protein chain were rebuilt in one MODELLER pass over the whole complex
  instead of chain by chain. Carries `chain_ids`, the segment list, the
  total rebuilt, `nonpolymer_in_template` (whether the selected ligands and
  ions sat in the MODELLER template as rigid blocks) and
  `nonpolymer_clearance` (closest approach of every rebuilt segment to a
  ligand or ion heavy atom). See "What the rebuild can and cannot see" below.
- `residue_mapping`: source-to-merged nucleic residue mapping.
- `glycan_metadata` and `glycan_linkages`: GLYCAM topology inputs.

## What the rebuild can and cannot see

Unresolved termini stay unmodeled by default. Build them only when the request
explicitly includes them, with `--build-terminal-missing-residues`. A terminal
segment has one structural anchor rather than an internal loop's two; MDClaw
records it as a deterministic predicted tail and requires confirmation. The
MODELLER route refuses terminal segments longer than 10 residues rather than
presenting a poorly constrained tail as recovered structure.

A rebuilt loop is placed by what surrounds it, so what is *present* while it is
built decides where it goes. Three things follow.

**Chains.** Gaps are rebuilt for all selected protein chains together, so a loop
at a chain-chain interface is modeled with its neighbours there. This is not an
inference about the biological unit: it is exactly the chains `--select-chains`
named. Select the biological assembly, not whatever the asymmetric unit happens
to hold. Measured on 9UT9: rebuilt chain by chain, chain B's 48-52 loop landed
0.42 A from chain A -- backbone included -- and nothing checked; rebuilt in
complex context the same loop sits 3.3 A away.

The corollary is a real caveat. Select two chains that only touch as a crystal
contact and MODELLER will now respect that contact while building. Better than
building straight through it, but not free, so the chain selection is a
scientific choice worth stating rather than defaulting.

**Ligands.** The selected ligands and ions ride along in the MODELLER template
as rigid BLK residues (`--repair-nonpolymer-context`, on by default), so a loop
beside a binding site is built around them rather than into them. Whether or
not the block was used, every rebuilt segment is then measured against every
ligand and ion heavy atom: `complex_missing_residue_repair.nonpolymer_clearance`
lists the closest approach per segment, a segment inside 3 A is a warning, and
a segment inside 2.2 A stops the prep with `modeller_loop_nonpolymer_clash` --
the model would be a loop threaded through the ligand (measured on 9OPZ before
this existed: the trigger loop 45-57 at 0.25 A from sucralose, twenty loop atoms
inside 2.5 A, no warning). Pass `--no-repair-nonpolymer-context` only to
reproduce the old ligand-blind build; the clearance check still runs.

**Comparisons.** Two deposits of the same system rarely leave the same residues
unresolved, so the rebuilt regions differ between them. Report which residues
are predicted in each, and keep predicted coordinates out of any collective
variable or observable the comparison rests on. Protonation is decided on those
coordinates too, so a pKa call inside a rebuilt loop is a property of the model,
not of the protein -- pin it with `--protonation-states` rather than letting the
two systems diverge.

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
