# Investigation of the SMO membrane-build bug report — 2026-09-08

Target: `/data1/$PROJECT/$MEMBER_A/structures1/SMO_WT_active_6XBL/`.
Below, `nodes/` refers to `study/jobs/main/nodes/` inside that directory.
The user's files were only read. Rebuilds were done inside `/tmp/smo-bug-audit/`,
and the user's DAG, the running production, and the installation were not modified.
The machine-readable summary is [smo-20260908-audit.json](smo-20260908-audit.json).

## Conclusions

- Bug 1, "all residues with AMBER alias names disappear", is refuted both by the saved artifacts and by rebuilds with the current code.
  However, the reporter's analysis script and an independent minimal-reproduction artifact from 9/8 have not been found,
  so this does not refute other files or other runs at the time of the report.
- Bug 2, the wrong neutralization charge, was reproduced. The explicit S–S bond plan is not passed to the temporary System used for neutralization,
  and a CYX whose distance-based detection fails is parameterized with a CYM-like negative charge. That is the cause.
- Bug 3, "there is no usable input state", does not reproduce with the current code.
  Passing the actual CYX input and the prep bond list puts all 9 bonds in the System.
- Bond completion on the PDBFile path for lipid21 is already implemented and is used in the user's normal builds too.
- The scientific-computing code relevant here is identical to the latest version. Use of an old scientific-computing implementation cannot explain the problem.

## Checking the version that ran

The upstream confirmed with `git ls-remote origin refs/heads/main` was
`fce3dbcd14d0e4ab5e6c84a8275b5801580a2213`.
The HEAD of the user's checkout was `1689f13079920480eb87c08258cc63a004565c55`.
Comparing the contents of all 141 Python files, the only difference from upstream was
`mdclaw/slurm/_base.py`.

The user's `mdclaw_wide.sh` specifies
`/data1/$PROJECT/mdclaw-rikyu-arm64-cuda130-cufft121-fusefix-6f171d2f0fa5.sif`
and sets the user's checkout on `PYTHONPATH`.
With the same settings, the import resolved to
`/data1/$PROJECT/$MEMBER_A/mdclaw/mdclaw/__init__.py`.

The SIF alone, without the checkout added, was also inspected directly. The bundled MDClaw release is 0.6.8,
and openff-pablo is 0.2.2. For all 141 Python files in the SIF as well, the only difference from upstream is
`slurm/_base.py`. The SHA256 of the Amber build, the Pablo bridge, membrane building, patch-tile, and
clean_protein were also confirmed to match.
So it is not "exactly the latest commit on main", but the scientific-computing implementation used here is the same as the latest.
The full launch environment of each past command is not recorded in the events, so it cannot be determined retroactively.

## Bug 1: the residues and bonds are present in the actual data

The PDB ATOM/HETATM lines were read directly, and residues with N/CA/C were counted as protein.
Without relying on a list of the 20 standard names, adjacent-residue C–N bonds and SG–SG bonds were checked
by atom index in the HarmonicBondForce of the System XML. Charges were summed independently from the NonbondedForce.

| Normal/workaround build | Atoms | Protein residues | HID | CYX | ASH/GLH | S–S in System | Backbone breaks | Protein charge |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| topo_005 (normal) | 157616 | 476 | 8 | 18 | 1/2 | 9 | 0 | +2 |
| topo_006 (normal, different prep) | 157628 | 476 | 8 | 0 | 0/0 | 0 | 0 | −1 |
| topo_007 (user workaround) | 157616 | 476 | 0* | 0* | 0/0* | 9 | 0 | +2 |

*In the workaround output, OpenMM's PDB writer normalizes the names to HIS/CYS/ASP/GLU.
The loss of protonation states or S–S bonds cannot be judged from names alone.
In the normal build topo_005, 29 alias residues are present under their original names.

`topo_006` has 0 S–S bonds because it is a separate branch derived from `prep_009`.
prep_009's disulfide_pairs is empty, and its input solv_007 also has no CYX/ASH/GLH.
There are 8 HID in both input and output.

Selecting residues by the 20 standard names alone gives 447 residues (476−29) for topo_005 and 468 residues
(476−8) for topo_006, matching the reported missing counts. An apparent loss caused by residue selection
is the most likely explanation, but without the reporter's actual analysis code it is kept distinct from a confirmed cause.
For reference, MDTraj's is_protein was also checked; it gives 475 residues, so this selection alone does not reproduce the 29-residue loss.

The events of the saved normal builds topo_005/006 are dated 9/7, and those of the workaround build topo_007 are dated 9/8.
The new rebuilds done here also kept the alias residues, and both conditions gave 157452 atoms
(the original 157616 atoms minus only the 164 Na/Cl atoms).

## Bug 2: the S–S plan is not carried over to the temporary build

The actual path is as follows.

1. `embed_in_membrane` in `solvation/membrane.py` selects patch-tile.
2. Around `patch_membrane.py:2601`, a partially assembled PDB is built and net_charge_fn is called.
3. `_compute_membrane_net_charge` at `membrane.py:1231` calls `build_amber_system`.
   Neither job/node nor `disulfide_bonds` is passed in this call.
4. The partial charges of the actual NonbondedForce are summed to decide the number of ions.
   This is not a formal-charge heuristic based on residue names.

Using the same PDB, `solv_005/artifacts/membrane.pdb` with only Na/Cl removed,
`build_amber_system` was run twice with the current checkout + the user's SIF.
Common conditions: ff19SB/OPC, is_membrane=True, hmr=True, pablo_auto_download=False,
and box_dimensions set to the value saved in solv_005. The only change is whether the bond list is given.

| Condition | Atoms | SG–SG bonds | A:490 charge | A:507 charge | Total charge |
|---|---:|---:|---:|---:|---:|
| No explicit plan (equivalent to the temporary charge build) | 157452 | 8 | −1 | −1 | about 0 |
| prep_008's disulfide_bonds.json passed as a list | 157452 | 9 | 0 | 0 | about +2 |

The SG–SG distance of 490–507 is about 3.345 Å in the initial coordinates of the normal topo_005.
This bond is not created by distance-only auto-detection; in the build with a bond plan, that one bond is added explicitly.
Without the plan, the residue names are still restored to CYX, but the System charge of each is −1.
Force-field graph matching gives them a CYM-like chemical state, so restoring names alone cannot prevent this.

Both builds have success=True and topology_validation=passed.
Without the plan, disulfides.status=not_requested, and protonation_variants also passed 21/21.
The CYX check in `amber/topology_validation.py` only verifies the absence of HG and does not require
an SG–SG bond or a residue charge for every CYX; this is the second cause of the miss.

The actual metadata of solv_005 is net_charge=0, with 82 each of Na/Cl.
Since the protein is really +2, the ion difference should be Cl−Na=2.
The user's solv_006 is a manually fixed version in which one Na was changed to Cl, and both
the normal topo_005 and the workaround topo_007 built from that parent have a total charge of 0.

The old solv_001 recorded net_charge=−2. The corresponding normal topo_001 has 9 S–S bonds,
2 of which have an initial distance above 3 Å, which is consistent with a −4e offset from the same mechanism.
A rebuild with this old input has not been done. The −1 of solv_007 matches the actual protein charge −1
of a different input that has no acidic aliases; it is not the same state that should be +2.

## Bug 3 and lipid21

With the current code, a rebuild using the actual CYX input and the 9 bond pairs specified by prep_008 succeeded.
The general problem of directly bonding CYS with HG, and every combination of operations in the old SIF, were not re-verified here,
but the claim that "with the current public arguments there is no usable input" does not hold.

`_topology_pablo.load_topology` falls back to PDBFile when lipid recognition fails.
The saved reports of topo_005/006 and both rebuilds here all have used_pablo=False.
From `amber/openmm_build.py:1219` onward, internal and external bonds are completed from force-field templates;
in the normal topo_005, 28,951 internal bonds and 442 external bonds were added.
It cannot be concluded that abolishing Pablo entirely or a new lipid-connectivity implementation is required for this fix.

## Fix targets and limits of the investigation

The priority fix is to carry the explicit bond plan from prep over to the neutralization build, with the same residue identification.
In addition, check the SG–SG bond and charge of each CYX, and do not report success for a state in which the bond plan cannot be resolved.
A check of the final System charge after ion placement is also needed. build_amber_system already has a mismatch guard based on the DAG's
neutralization_expected, so that contract should be kept.
A fix that merely replaces a heuristic with a sum of partial charges does not solve this.

`atom_count_preserved` currently checks mutual agreement among the output Topology/System/State;
it is not an item that independently guarantees preservation relative to the original input. Here this was supplemented by counting the input and output separately.

This work is a root-cause investigation; no product code has been fixed yet.
The user's equilibration temperature, density, and full time series were not re-analyzed; the focus was on the bonds/charges of the existing topologies
and control rebuilds. Temporary logs and reproduction scripts are in `/tmp/smo-bug-audit/`.

## Additional investigation: how the "residue loss" report came about, and a skill audit

### A sequence-length inconsistency in the actual CLI

The public CLI was run with the user's SIF and the current code. `inspect_molecules` is read-only,
and the output directory of `split_molecules` was `/tmp/smo-bug-audit/`.

```bash
mdclaw inspect_molecules --structure-file <node>/artifacts/system.topology.pdb
mdclaw split_molecules --structure-file <node>/artifacts/system.topology.pdb \
  --output-dir <temporary-directory> --include-types protein --select-chains A
```

| Input | inspect_molecules num_residues / sequence_length | split_molecules.all_chains num_residues / sequence_length | CA count in extracted PDB |
|---|---|---|---:|
| topo_005 | 476 / 476 | 476 / **447** | 476 |
| topo_006 | 476 / 476 | 476 / **468** | 476 |
| topo_007 (workaround) | 476 / 476 | 476 / 476 | 476 |

Besides a merely hypothetical 20-standard-residue filter, the product's actual CLI also has a place that returns the reported numbers.
The cause is that the inspection implementation is duplicated.

- The public `inspect_molecules` uses `PROTEIN_RESNAMES` in `mdclaw/research/inspection.py:464`
  and counts AMBER aliases.
- `_inspect_molecules_impl` in `mdclaw/structure/split.py:364`
  counts only `AMINO_ACIDS` and excludes HID/CYX/ASH/GLH from `sequence_length`.
- This internal implementation is used by `split_molecules` (around line 1002 of the same file) and
  `prepare_complex` (prepare_complex.py:1346).
- In the same JSON, `num_residues` and `chain_file_info.residue_count` are 476.
  The actual extracted PDB also has 476 residues, so the error for this SMO input is in the displayed sequence length,
  not atom deletion.

The workaround script normalizes the AMBER names to HIS/CYS/ASP/GLU via PDBFile,
so the same internal check gives 476. This explains the phenomenon "it looks complete only after the workaround".
However, no execution record was found showing that the reporter used this CLI output or internal function for the judgment,
so it cannot yet be concluded that this display bug was the direct cause of the report.
The 468 in the split_metadata of the existing prep_008/009 is the actual residue count of the original structure before aliases were assigned
(8 residues were completed afterwards); it is not a past record of this display bug occurring.

Visualization was also isolated. The user's preview script uses `polymer.protein`.
Loading topo_005/006/007 in PyMOL from the same SIF, each gives
476 atoms for `polymer.protein and name CA`. The 18 CYX SG atoms of topo_005 are also selected.
So the protein selection of this preview itself does not reproduce the 29-residue loss.

### Timeline established from the saved records (JST)

1. **9/7 15:33**: solv_005 completed with neutralization charge 0 and 82 each of Na/Cl.
2. **9/7 15:37**: solv_006 was created. The node warning states the manual Na→Cl fix.
   There is a node_created event but no tool_started/tool_completed.
3. **9/7 15:39**: the normal topo_005 completed. The saved XML has 476 residues, 9 S–S bonds, and charge 0.
4. **9/7 15:58–16:07**: separate branch prep_009→solv_007→topo_006.
   It has no CYX/ASH/GLH from the input, and the S–S specification is empty. The 8 HID are kept.
5. **9/8 11:26**: the workaround topo_007 was created.
   The node_created event is timestamped 11:26:17, and the created_at in the current node.json is 11:26:32.
   metadata is empty, there are no tool_started/tool_completed events, and the warnings say
   "Pablo deleted 29 residues" and "even HID alone lost 8/8".
   This is not a tool report with measurements; it is text recorded as the reason for adopting the workaround.
6. **9/8 11:28–12:43**: min/eq of the workaround System completed.
   That success is not evidence that the preceding normal build was broken.

All SMO files in the shared area, and the scripts, text, JSON, and hidden files in the user area,
were searched for residue-loss wording and checking code. The most direct saved claim is
the warnings in topo_007/node.json above; no independent minimal reproduction code or failure output was found.
`direct_openmm_test/` contains 4 workaround build scripts, but no checking code that measured
29 missing residues against the normal build.

### Evaluation of the skills

All 55 Markdown skills in the user's checkout were compared.
The only differences are hpc-run/SKILL.md, hpc-run/sif-slurm.md, and common/preamble.md.
md-prepare, md-analyze, md-equilibration, visual-qa, and tool-output are the same as the current version.
However, determining which installation's skills the conversation in question actually read requires the conversation log.

**No skill was found that explicitly instructs wrong residue deletion.**
On the contrary, prep-chemistry.md explains that HID/CYX/ASH/GLH are supported and forbids
removing S–S bonds to avoid downstream failures.
visual-qa.md also states explicitly not to judge chemical correctness from images.

On the other hand, the guidance that would prevent misdiagnosis has the following gaps.

- Around `md-prepare/explicit-water.md:84`, the build is described as the Pablo path,
  but the PDBFile fallback and lipid bond completion are not mentioned.
  There is also no instruction to check the path actually used via `topology_validation.loader.used_pablo`.
  This omission makes it easy to misattribute to Pablo, but a causal link to this report is unconfirmed.
- The topo Handoff guides checking for completed, but there is no dedicated procedure for comparing the input/output atom and residue
  correspondence and the bonds in the System when an anomaly such as residue loss is suspected.
  There is also no guidance distinguishing sequence length, residue count, selection results, and residue-name normalization.
- There is no explanation that the default selection="protein" of `md-analyze` is a subset for analysis
  and is not synonymous with a full residue list for a completeness audit.
  MDTraj gives 475 here, PyMOL 476, and the internal sequence length 447, so the selection scheme must be made explicit.
- common/run-loop.md instructs avoiding moving files and rewriting terminal nodes.
  The manual-fix/workaround nodes here have no normal execution events, and carry no
  command, target file, or observed result of the check that serves as their basis. They are not the standard path that the skill instructs.
  However, the user may have separately approved diagnostic experiments, so this is not judged to have been an unauthorized operation.

The above is not "evidence that a skill directly caused the misdiagnosis", but gaps in explanation and diagnostic procedure confirmed independently.
In addition to unifying the inspection implementation on the CLI side, it is appropriate to give the skills a common verification procedure for anomalies
that records the execution path, the correspondence with the original input, the System bonds/charges, and what was measured versus inferred.

### The last unconfirmed link

`/home/$MEMBER_A` is accessible only to its owner, and the conversation history and shell history
cannot be read because of OS permissions. Permissions were not bypassed.
The conversation log in question was not found in the shared area.

So what can be established at this point is: (1) the actual CLI bug that returns the reported numbers,
(2) the mechanism by which the workaround normalizes those numbers, (3) the time at which the wrong loss claim was saved, and
(4) the gaps in the skills for preventing misdiagnosis.
**Who / which agent concluded atom deletion by Pablo, and from which check values,** is unconfirmed.
Establishing that requires the SMO conversation log from before 9/8 11:26, the check commands and stdout, and the referenced file names.
The user has already been asked for the path of a shareable conversation log.

## Final re-investigation: establishing the cause of the old S–S failure, and closing items that lack evidence

Following the user's instructions, the search scope was widened, and items for which no information remains are closed here.
This section updates the previous section's assessment that "the cause of the old-version S–S failure is unconfirmed".

### Search scope

- The current project `/data1/$PROJECT/$MEMBER_A` and the additionally found old project
  `/data1/$OLD_PROJECT/$MEMBER_A`. The file listings, excluding git internals and the membrane cache, have 6079/5114 entries respectively.
  These are inventory counts; they do not mean every file's content was read by hand.
- Related JSON/JSONL, scripts, Markdown, logs, Slurm output, and hidden files in both areas were
  searched by the terms and paths SMO/6XBL/residue loss/histidine/S–S failure.
- All failure/latest/tool_result.json of the 6XBL job, the amber_metadata of every topo,
  node.json, and the append-only events were cross-checked.
- The SMO, CHARMM-GUI attempts, and job index in the old area were investigated. A separate SMO attempt on 8/21 had
  a different protonation validation failure, but it did not prove the 29-residue loss in 6XBL.
- The skill change history was cross-checked against the code before and after `8e703ed`.
- `/tmp` was limited to an inventory of related file names such as SMO/user name.
  The contents of unrelated temporary files of other users were not examined.
- `/fast1/$PROJECT/$MEMBER_A` and `/fast1/$OLD_PROJECT/$MEMBER_A` do not exist.
  `/home/$MEMBER_A` remains owner-only and unreadable. Permissions were not bypassed.

### Bug 3: the mechanism of the actual 9/3 failure was reproduced

The error saved in `topo_004/artifacts/failure/latest/tool_result.json` is
a template mismatch with one external S bond too many for the CYS atom composition.
In `amber_metadata.json` in the same directory, the `disulfide_bond_plan` has all 9 pairs as
`skipped_cys_protonated`.

The parent `prep_005` is a branch without S–S, with `disulfide_pairs=[]`.
Its merged.pdb, solv_004's membrane.pdb, and topo_004's system.prepared.pdb actually contain
HG on the target CYS, including CYS64/178.
However, the plan above shows that the topo_004 build received the 9 explicit bond specifications.

With the current code and the user's SIF, a control experiment was run using the saved `topo_004/artifacts/system.prepared.pdb`
as is. The box/ff19SB/OPC conditions are the same as the saved metadata.
Output is in `/tmp/smo-bug-audit/final-sweep/`.

| Condition | Result |
|---|---|
| CYS with HG, the 9 pairs from the saved metadata specified | Fails like on 9/3 with `No template found for residue 6 (CYS)`, excess external S bond |
| Same PDB, bond specification empty | Succeeds, 157532 atoms, total charge about 0 |

This comparison does not show physical equivalence of the reduced and oxidized states.
It is a diagnostic experiment to isolate that "S–S bonds added while HG is still present" create the failure.

Right after structure loading there are 0 SG–SG bonds. The planning function reports all 9 pairs as skipped.
Nevertheless, passing the original specification to `add_disulfide_bonds` adds 9 bonds, and HG remains.
The actual call inside `build_amber_system` follows this same flow.

1. `amber/build_system.py:1336` calls `_plan_disulfide_topology_bonds`.
2. Around `amber/topology_bonds.py:260`, the CYS are marked `skipped_cys_protonated`.
3. **The plan's skip result is not used for execution control**, and build_system.py:1410 passes the original
   `disulfide_bonds` to the build implementation.
4. From around `amber/openmm_build.py:1113`, `_topology_pablo.add_disulfide_bonds` is called, and
   that function calls `topology.addBond` without checking for HG.
5. CYS–HG and SG–SG coexist, causing a template mismatch.

In other words, rather than "the API rejects early because hydrogens are present", **a plan/execution inconsistency that actually adds
bonds recorded as skipped** is the direct cause of the saved old failure.
It still reproduces by the same mechanism today.

`8e703ed` changed clean_protein, tests, and memo; the Amber build path above
was not changed. The preceding commit also has the same plan call and passes the unprocessed argument.
So it cannot be said that `8e703ed` fixed this inconsistency.

However, **the claim that "no valid input state exists" is a separate matter and does not hold.**
The normal topo_003 of 9/2 has a completed event and 9 S–S bonds in the System,
and with the current code, CYX without HG + the correct 9-pair plan succeeds.
The fix is to validate the chemical consistency of the request in advance and make plan and execution agree.
This is distinct from silently moving hydrogen reconstruction, which prep owns, into topo.

### A real example where "completed" alone cannot reconstruct a past run

The current node.json of topo_001 says completed, but the 9/2 execution event is
`tool_failed` / `neutralization_charge_mismatch`, and amber_metadata also has success=False.
The saved System has a protein charge of +2 and ions +2 for a total charge of +4, so the charge guard actually fired.
There is no normal tool_completed event after that.

For this reason, the current node status seen in the first inventory is not used as evidence that the 9/2 build completed normally.
This does not affect the conclusions from the independent inspection of the saved Systems of topo_005/006.
The skill's completed check is valid for normal execution management, but a bug investigation after manual intervention
must cross-check events, tool results, and the actual files.

### Final verdict

| Item | Verdict |
|---|---|
| Wrong neutralization charge | Cause established. Missing S–S plan in the neutralization build and insufficient CYX validation |
| CLI returning the same numbers as the residue loss | Cause established. Duplicated inspection implementation excludes AMBER aliases. Actual residues do not disappear |
| 9/3 failure with HG-bearing CYS + S–S specification | Cause established. Inconsistency: the plan skips, the execution adds the bonds. Still reproduces today |
| No valid S–S input exists at all | Refuted by the saved old success and by current reproduction |
| No PDBFile connectivity path for lipid21 | Refuted by the implementation, saved artifacts, and rebuilds |
| Possibility that old scientific-computing code was used | Not the case for the SIF/checkout that could be confirmed. The scientific-computing code is the same as current |
| From which check values the loss was actually concluded | Cannot be established without the original check and conversation log. Closed for lack of evidence |
| Which skills were actually read, and direct causation of the misdiagnosis | Cannot be established without the conversation log. Closed at pointing out explanation gaps |
| How it was judged that there is no lipid21 path | No basis remains other than the diagnostic text; closed for lack of evidence |

Unless a new conversation log is provided, examining the same artifacts again will not settle the final attribution.
Following the user's instructions, this evidence-lacking part will not be pursued further.
The product code, skills, and the user's DAG are unchanged. The investigation builds have finished.
