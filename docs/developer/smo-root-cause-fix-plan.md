# Fix plan for general chemical-state and residue checks, starting from the SMO report

Written: 2026-09-08. A plan, before implementation.
Evidence: [investigation record](../research/smo-20260908-bug-investigation.md) / [reproduction results](../research/smo-20260908-audit.json).

## Goals and the post-fix contract

1. The bond and protonation states fixed in prep are shared by the neutralization System and the final System.
2. When a request cannot be resolved, stop without changing the chemical state, with an error that identifies the cause.
3. Report residue presence, names, sequence length, selection results, and the chemical state of the actual System as distinct things.
4. Do not make the success decision depend only on the number or names of bonds; verify against the requested atom pairs and the force-field state.
5. In addition to SMO succeeding, make the non-SMO regression matrix below a pass condition, and do not break existing working cases.

Removing Pablo entirely, a new lipid builder, or changing the MD engine is not needed for this fix.
Do not overwrite existing users' terminal nodes, and do not unilaterally repair inconsistencies found in the investigation.
Do not base the implementation on the unconfirmed question of "what the reporter read".

## 1. Unify bond planning and execution (top priority)

Main targets: `amber/topology_bonds.py`, `amber/build_system.py`,
`amber/openmm_build.py`, `_topology_pablo.py`, and the existing residue-identification helpers.

- Consolidate normalization and resolution into one routine while keeping input-format compatibility.
  Use not only chain/resnum/insertion code but also the necessary component/chain identity information,
  so that duplicated chain/resnum in a membrane are not mistakenly treated as the same.
- Record "requested pairs", "pairs resolved on the structure", and "existing/added bonds" separately.
  The bond-adding function accepts only resolved pairs and does not reinterpret raw arguments.
- Fail S–S requests on CYS carrying HG, nonexistent/ambiguous sites, and conflicting partners before System creation.
  Abolish the current behavior of reporting `skipped_cys_protonated` while still proceeding.
  Do not silently strip HG in topo, and do not silently skip the user's request.
- On failure, return a stable code, the target site, the observed state, and a suggested fix on a new prep branch.
  Decide the code names after checking the existing guardrail registry, and keep them consistent across the CLI, trace_failure, and skills.
- Preserve the distinction between None (unspecified) and an empty list (explicitly no bonds).
  When existing bonds in the PDB conflict with an explicit specification, do not resolve it by unconditional distance re-estimation.
- Do not add duplicates of existing correct bonds, and make the result stable for the same plan.

Done when: an HG-carrying input equivalent to topo_004 gives an explainable up-front error when bonds are specified,
and succeeds while keeping the reduced state when they are not. A correct CYX input satisfies the 9 requested bonds with exactly those partners.

## 2. Fix the handoff of chemical information to the neutralization System

Main targets: `node/inputs.py`, `solvation/membrane.py`, `solvation/patch_membrane.py`.
Use the shared routine from 1.

- In solv input resolution, obtain the selected prep's bond plan and the necessary identity mapping.
  Do not infer them by directory search or from the "latest prep" on another branch.
- Explicitly map the plan onto the residues after orientation/assembly.
  Verify that the plan still refers to the same protein sites after coordinate rotation and addition of water and lipids.
- Pass the plan to `_compute_membrane_net_charge` and run it through the same bond validation as the production build.
  Treat the temporary neutralization build as processing inside solv, and do not create a fake completed topo in the user's DAG.
- If the standalone entry point is kept, let it accept a bond specification explicitly.
  When CYX is present but the bonds cannot be resolved, do not proceed with neutralization by guessing from distance alone.
- Use the existing NonbondedForce sum for charge. Make the force field, water model, and supported chemical information
  identical between neutralization and production. Do not add new residue-name heuristics.
- Fail when there is no NonbondedForce, when values are non-finite, or when the total deviates from an integer by more than the tolerance.
  Do not hide anomalies with an unconditional initial value of 0 or with round.
- Save the raw charge value, the plan's provenance, the pair-validation results, the number of ions placed, and the charge balance after placement.
  Keep the existing `neutralization_charge_mismatch` check in production topo as well.
  Do not add unnecessary rebuilds of the same huge System; verify with the charge balance right after ion replacement,
  and with the actual System's charge in the final topo.

Done when: the SMO neutralization build returns +2e and 9 S–S bonds, and ion placement gives Cl−Na=2.
Another branch in the standard state keeps that branch's correct charge and is not forced to the original +2.

## 3. Strengthen verification of chemical state and input preservation

Main targets: `amber/topology_validation.py` and the template application / validation sites at System creation.
Implement a minimal guard together with 1, and make it an acceptance condition for 2.

- Change S–S validation from "observed count at least the expected count" to matching the resolved atom pairs.
  Verify the requested pairs in both Topology and System, and reject cases that bond different partners with the same count.
  Also handle the System's bond representation (force terms / corresponding constraints).
- Whether or not an explicit plan exists, require a correct SG–SG connection and no HG for every CYX.
  Also reject cases where a CYS carrying HG has an S–S bond.
- Match the applied template against the chemical state so that CYM-like charges are not assigned under the display name CYX.
  If needed, pin the corresponding template via the existing residueTemplates mechanism.
  Template names depend on the force field and terminal state, so do not simply fix every CYX charge to 0.
  Verify agreement with the correct template, including terminal charges.
- For HID/HIE/HIP, ASH/GLH, and LYN/CYM as well, keep validation of the required H patterns and identifiers.
  Use the insertion code consistently all the way through final validation.
- Report atom/residue preservation relative to the original input separately from the mutual consistency of the output Topology/System/State.
  Record permitted transformations such as adding virtual sites individually, and do not substitute a mere total-atom-count match.
- Keep the actual loader, the fallback reason, and identifiers of the inputs/outputs and the plan in the structured result.
  Do not change the meaning of existing fields; add them as new validation fields.

Done when: a negatively charged isolated SG that used to give "CYX, 21/21 passed" no longer succeeds.
The same number of wrong bonds, missing residues, and mix-ups with a different insertion-code site can be detected.

## 4. Share residue classification and sequence generation

Main targets: `chemistry_constants.py` and the necessary shared helpers,
`research/inspection.py`, `structure/split.py`.

- Use the same residue classification and conversion to a one-letter sequence in public inspect and inside prep/split.
  Map AMBER aliases to the base names of the standard amino acids, and do not drop them from the sequence length.
- Do not stop at a local fix that only swaps the definition sets; clean up the duplicated AA_CODE and classification logic.
  Merging the whole of the large inspect function is not needed. Extract the shared parts whose meanings have diverged.
- Define how amino-acid sequence length, total residue count, cap count, and unknown/modified residues are handled.
  Do not assume "sequence length = total residue count" even when ACE/NME are present.
  Do not silently exclude unsupported modifications, and check consistency with the existing PTM, nucleic, and glycan classifications.
- Do not change atom or residue names of the actual structure for display purposes.
- Keep the existing JSON structure as much as possible and correct the values that were wrong.

Done when: for all 3 SMO inputs, public inspect, the split sequence length, and the extracted PDB agree at 476.
A short protein made only of AMBER aliases is not misclassified as a ligand either.

## 5. Update skills and the guidance in diagnostic results

Carry this out after the implemented codes and validation fields are finalized.
Edit `skills/` as the canonical source and verify mirror consistency.

- Add one shared anomaly-diagnosis leaf, referenced conditionally from md-prepare/md-analyze/md-report.
  Do not turn the normal spine into a long checklist.
- When "residues disappeared", "S–S is missing", or "the charge is wrong" is suspected, have the agent check, in order, the target node and files,
  the actual loader, the input mapping, the validated atom pairs/charges, the selection expression, and the tool results.
- State explicitly that sequence_length, residue count, MDTraj/PyMOL selection, and normalization of PDB aliases
  are distinct concepts.
- Document the PDBFile fallback and the existing lipid template completion in the explicit-water build description.
  Prefer structured loader information from the run over guesses made by reading the source.
- Do not call it a fix when success is obtained by changing the chemical request.
  Fix an HG-carrying CYS on a new prep branch, and show the normal DAG procedure.
- For histories with manual intervention, do not rely only on the current completed status; cross-check events, tool_result,
  and the actual System. No feature to automatically overwrite contradictory histories is added this time.
- When reporting an error, keep the command, target files, observed values, expected values, and the version run,
  and distinguish observations from inferred causes. Save the stdout JSON, and do not rely on grep excerpts alone as evidence.

Done when: a walkthrough using actual new CLI output correctly leads the three symptoms seen this time to the right codes/evidence.
If CLI arguments change, update tool-reference, --list-json, CLI input validation, and examples at the same time.

## Verification and release order

Split the implementation into 4 reviewable units.

1. Unified bond planning + the corresponding chemical validation (the required parts of 1 and 3).
2. Handoff to neutralization + final charge validation (2 and the rest of 3).
3. Shared residue classification and sequence generation (4).
4. Skills, diagnostic material, and release notes (5).

Extend existing tests in each unit; new tests verify the contracts that failed.

- Small local fixtures: an explicit pair with a long SG–SG distance, CYS carrying HG, an isolated CYX, CYM,
  insertion codes, duplicated chain/resnum, the same number of bonds with different partners, an empty specification, and a correct CYX without HG.
- DAG integration tests: the plan and IDs are preserved from the correct prep branch through solv → neutralization build → topo.
- Inspection-consistency tests: classification of AMBER aliases, caps, PTMs, and nucleic/ligand in the public CLI and the internal split.
- Existing disulfide/variant/topology, solvation, chain-selection, and CLI/registry/guardrail tests.
  If node input resolution changes, a dedicated resolver test is required.
- Smoke/pipeline tests with real dependencies via the SIF overlay. Add --nv for OpenMM paths.
- Finally, in a new isolated study on the real SMO input, without modifying the original files and using the prep-derived plan,
  check solv → topo → a short min. 476 residues, 9 correct S–S pairs, 0 backbone breaks, total charge 0, finite energy.
  Also confirm that the 9/3 HG-carrying input gives an up-front error.
  Do not run the same huge system routinely as a unit test; keep synthetic fixtures and real-case verification separate.
- Confirm in the existing pipelines that lipid bond completion and standard-state/non-membrane systems have not regressed.
  The user-specified 2.1 ns equilibration test of the same SMO system below is a required condition for final acceptance.
  Production beyond that is not part of this test.

During Python/skill development, overlay the checkout on the existing SIF. For distribution, follow the release/container procedure
to align the SIF containing the updated code with the version and manifest, and check the SIF's own import targets and the hashes of the fixed files.
Do not automatically replace users' existing runs; tell them under which conditions re-preparation and re-validation on a new branch are needed.

## Required run test: confirm the fix on the same SMO system as the report

Per the user's additional instruction, do not call the fix complete based only on synthetic fixtures or a short min.
A controlled test with the fixed inputs below, and a run with the normal CLI from membrane building through 2.1 ns equilibration, are required.
This is an execution plan; no new computation is started at the time this plan is written.

### Inputs to fix and comparison targets

Root of the original data:
`/data1/$PROJECT/$MEMBER_A/structures1/SMO_WT_active_6XBL/study/jobs/main/nodes/`

| Purpose | Files/nodes to fix |
|---|---|
| Same receptor coordinates and chemical state | prep_008/artifacts/merge/merged.pdb |
| The 9 requested S–S pairs | prep_008/artifacts/disulfide_bonds.json |
| Canonical component identification | prep_008/artifacts/chain_identity_map.json and related prep artifacts |
| Original membrane build conditions/placement | solv_005's membrane_metadata, patch metadata, box/geometry metadata |
| Original unmodified membrane | solv_005/artifacts/membrane.pdb |
| Reference values from the normal regular topo | topo_005's System/Topology/State |
| Known failure with HG-carrying CYS | topo_004/artifacts/system.prepared.pdb and the 9 pairs in amber_metadata |
| Comparison with the user's workaround results | topo_007, min_002, eq_001 (read-only) |

At the start, record in a manifest the SHA256 of every input, the pre-fix/post-fix commits, the SIF, dependency versions, and import targets.
Do not treat inputs as identical just because the PDB names match. Also cross-check the hashes of the fixed files actually imported.

Create an independent test study/job and, using the existing test-fixture approach, prepare a baseline that read-only copies the original completed source/prep and the necessary
provenance. Make the copy origin and the original hashes explicit,
and do not record that prep was rerun this time. This avoids coordinate differences from rerunning MODELLER etc.
Create only the new solv/topo/min/eq through the normal node API/CLI.
Verify the post-copy references and the DAG with inspect/explain, and forbid any path that writes back into the user's area.
Do not modify the production running in the original run, terminal nodes, or shared caches.

### Test A: before/after control on a fixed structure (isolating the chemical-state cause)

1. From the same solv_005 input, make a PDB with only the ions removed, and keep a record of the conversion.
   Do not change protein, lipid, or water coordinates.
2. As the pre-fix control, use the recorded "no bond plan → 8 bonds, charge 0" and
   "with plan → 9 bonds, charge +2"; if the manifest does not match, remeasure.
3. Confirm that the post-fix neutralization build receives the prep plan and gives **the 9 requested bonds and charge +2**.
4. Confirm that an input missing the plan while leaving isolated CYX does not succeed as charge 0 but gives a structured error.
5. Confirm that topo_004's HG-carrying CYS + 9 specified pairs stops with the new error before System creation.
   An empty specification on the same input succeeds as the reduced state, and correct CYX + 9 pairs succeeds as the oxidized state.
6. Run public inspect and split on the same 3 saved PDBs, and confirm that all sequence lengths are 476 and the extracted PDBs are also 476.

Pass condition: the normal and abnormal inputs for each cause give the expected results; do not judge by the final success alone.

### Test B: from normal membrane building through minimization (required)

With the test job's fixed prep as the parent, run the following.

```text
create/explain solv → embed_in_membrane
create/explain topo → build_amber_system
create/explain min  → run_minimization
```

- The actual input is prep_008. Fix the receptor's 476 residues, HID×8, CYX×18, ASH×1, GLH×2, and the 9 S–S pairs.
- Lipid POPC, ff19SB/OPC, NaCl 0.15 M, patch-tile.
  The original patch cache key is `92fd031d235d13c1aa47f9a8b054212099d68d798d916eea475d6243d213a65a`.
  If possible, copy the same patch into a test cache and cross-check the coordinate/composition hashes.
- Finalize the saved conditions such as the original orientation, dist/dist_wat, salt, and box in a table before running.
  Do not assume that missing settings are "the same as the original". If an existing oriented intermediate structure is used,
  state that scope explicitly, and verify the passing of bond-plan IDs through the normal solv path.
- Let the fixed embed_in_membrane perform neutralization. Do not use solv_006's manual ion fix or
  the direct_openmm_test workaround script.
- To match the user's comparison target, use hmr=False for topo, and for min use solute_heavy restraints,
  restraint constant 100 (confirm the CLI's default unit from the signature and saved metadata), and max_iterations=5000.
  Pass the topo/min state files downstream through normal DAG resolution.

Required checks at each stage:

| Stage | What is checked / pass condition |
|---|---|
| prep→solv | The mapping of the 476 residues and the input heavy atoms is preserved; the chemical state and the 9-pair plan are unchanged |
| Neutralization System | 9 correct S–S pairs, charge +2e (numerical tolerance 1e-3e) |
| Ion placement | Cl count − Na count = 2. Water/ion replacement counts and the charge balance match the record |
| topo | Topology/System/State are consistent, the 9 pairs match by atom pair, absolute total charge <1e-3e, 0 backbone breaks |
| lipid | Same lipid composition as the original. Check internal/external bonds and 0 unconnected external-bond atoms |
| min | Energy and coordinates are finite, and the requested structure and bonds are kept. Tally SG–SG distances and backbone C–N distances directly |

In Test A, where the original composition and placement can be fully fixed, require exact agreement of atom counts.
In Test B, which re-places the membrane, water/ion counts can change, so do not use the total of 157616 alone as a success condition.
Always explain the protein and lipid mapping and the permitted water/ion differences.

### Test C: equilibration under the same conditions as the original report (required)

Once Test B's min passes, continue directly with the normal `run_equilibration`.
Fix the following to match the saved eq_001.

- NVT: **0.1 ns / 50000 steps**.
- NPT: **2.0 ns / 1000000 steps**.
- **300 K, 1 bar, 2 fs, hmr=False**.
- Confirm the semi-isotropic setting for membranes in the System/run metadata.
- For the solute_heavy restraints and membrane headgroup restraints, check the original settings and use the same ones.
  The original observed counts are protein heavy 3738 and lipid headgroup 221. Compare the selected sites, not only the counts.

At the end of NVT, at the end of NPT, and on saved trajectory frames, independently check the residue/heavy-atom mapping,
S–S atom pairs, PBC-aware SG–SG/C–N distances, and finiteness.
Save temperature, volume, and density time series and statistics over the last 10%, and put them side by side with the original eq_001.
Exact agreement with previous values for the stochastic trajectory, final energy, and volume shrinkage ratio is not required.

- Loss of atoms or bonds, backbone breaks, NaN/Inf, incomplete simulated time, or mismatched run conditions are failures.
- Even if the run completes numerically, if temperature, density, or membrane structure is abnormal, put it on hold and investigate the cause.
- Fix the numerical acceptance ranges for temperature, density, and bond distances in the test specification **before running**, from the actual statistics of the original eq_001 and the existing validation conventions.
  Do not move thresholds conveniently after the run.
- Finally, make whole-membrane/top-view images from the same viewpoint as an auxiliary check for obvious placement accidents.
  Do not use the images as a substitute for bond and charge validation.

### Execution environment, deliverables, and completion conditions

- Use a GPU node close to production. At run time, check resources following the hpc-run procedure and submit to Slurm.
  Save the job script, stdout/stderr, measured wall time, and GPU utilization.
- For the first run, plan on 1 GPU and a 60-minute walltime assuming a reused patch, and before submission
  check plausibility against the available GPUs and existing logs. This is not a guarantee of run time.
  Do not treat a timeout or insufficient resources as a scientific pass; follow the normal restart/new-branch contract.
- Combine the output study_plan, nodes/events, manifest, structured check JSON, comparison tables, time-series plots,
  and failure-control results into one test report.
- After updating the release SIF, recheck the SIF's own CLI, import/hash, and Test A on the fixed structure.
  Do not consider it complete if only the overlay passes while the distributed build is stale.
- **Only once A/B/C all pass and there is evidence that neutralization → build → min → 2.1 ns eq succeeded with the normal CLI,
  report "the fix was confirmed on the same SMO system". Declaring the product-wide fix complete and deciding on distribution
  additionally require passing the non-SMO regression tests below.**
- If any of them is not run, fails, or has mismatched conditions, state that fact explicitly and do not declare the fix complete.

## Required design and test conditions to prevent overfitting to SMO

SMO is the acceptance case for a known defect, not the source of the specification.
What follows are required conditions for each implementation unit and for the release. Even if SMO passes, breaking another working case is a failure.

### SMO dependencies that must not go into the implementation

- Do not embed the PDB ID, residue numbers 490/507, chain A, 476 residues, 9 bonds, +2e, POPC/OPC, or specific cache keys or paths
  into decision or repair branches. Limit these to the expected values of the SMO fixture.
- Do not widen the detection distance to fit SMO's long SG–SG distances.
  Prefer the explicit bond plan, and do not misconvert metal-coordinating sulfurs into S–S.
- Do not automatically convert CYM to CYX. Keep legitimately reduced CYS, free thiolate, and metal-coordinating CYM.
- Do not make "CYX is always 0e", "the whole system is always neutral", or "a protein selection includes all residues" a shared specification.
  Take termini, caps, the selected force field, neutralization intent, and metals/ligands/nucleic acids/charged lipids into account.
- Do not make the System fit by hand-editing names or partial charges. Check consistency between the chemical state and the applied template.
- Do not let systems the new validation cannot handle pass via a generic error or silent deletion.
  If a change that narrows the existing supported scope becomes necessary, put the specific systems, reasons, and migration path up for review.

### Grounds for shared correctness

- Take expected bonds from the explicit plan / existing validated structures, charges from the selected force field's templates and System,
  and residue presence from the input's atom/residue inventory.
- Do not generate expected values with the check functions being fixed. Compare against the fixture's independent list of atom pairs,
  column reads of the raw PDB, and an independent tally of the System XML.
- For existing working cases, compare the old and new versions with the same inputs, random numbers, and computation settings.
  Compare atom mapping, bond partners, templates/charges, particle counts, and box/force classes,
  and also check energies/forces of the same state against the existing numerical tolerances.
  Explain each legitimate difference that comes with the fix by its cause. Updating goldens just to make failures disappear is forbidden.
- For cases where the old version was also wrong, do not take the old output as correct; use an independent chemical expectation.

### Required non-SMO test matrix

Do not run every combination as a huge MD; split into small tests of the shared helpers, System builds with real dependencies,
and short DAG runs on representative systems. Explicitly include combinations that cross the boundaries changed this time,
such as membrane + metal, membrane + protonation variant, and ID collisions.

| Category | Fixed representative cases | Contract to keep | Required test depth |
|---|---|---|---|
| S–S positive case | tests/data/disulfide_bpti.pdb (BPTI) | Numbering different from SMO, 3 correct S–S pairs | Detection/plan/System build + non-membrane solv→topo→min→short eq |
| No S–S | Existing small CYS-containing reduced-state fixture, non-SMO membrane fixture without S–S | Do not add bonds automatically; keep CYS/HG | Working-case diff comparison + System build |
| Metal coordination | metal_site_cys4.pdb, existing metal pipeline | Do not break metal coordination even with close SGs; keep CYM | Metal guard + System/pipeline, small handoff test to membrane |
| Protonation | Small fixture containing HID/HIE/HIP, ASH/GLH, LYN, CYM | H pattern, template, charge, sequence length | Per-variant parameter tests + actual System |
| Termini/caps | N/C-terminal CYS/CYX, ACE/NME, multiple chains | Do not misjudge terminal charges and caps | Template matching/classification/bond validation |
| Non-SMO membrane | Existing 2LOP membrane pipeline | Keep neutralization, lipid connectivity, and the DAG for a different structure | embed→topo→min→short eq |
| Membrane composition | Supported POPC, POPC/POPE/CHL1 mixture, charged lipids confirmed as supported in the catalog | Correctly include lipid composition and total charge | Small membrane System + ion balance |
| Membrane representation/path | lipid21 modular/full, patch-tile/existing packmol-memgen, Pablo success/PDBFile fallback | Same chemical contract even when the loader/representation changes | Real-dependency smoke of existing paths + boundary tests |
| Force field/water | Supported ff19SB/OPC, ff14SB/TIP3P | Do not make it an ff19SB/OPC-only fix | Small System/solvation for both pairs |
| Non-protein | Existing pipelines for standard DNA/RNA, GLYCAM glycoproteins, phosphorylation, charged ligands/standard ions | Alias support does not misclassify ligands or glycans as protein | Existing pipelines + classification tests |
| Neutralization intent | Positive/negative/zero charge, with/without retained ions, salt=0, neutralization disabled, explicit/implicit/vacuum | No forced neutralization of the whole system or double counting of ions | Condition-table tests + representative actual Systems |
| IDs/input formats | Multiple chains, duplicated numbering, insertion codes, PDB/mmCIF, unspecified/empty/duplicate/conflicting pairs | Do not mix up partners; unknown requests fail explainably | Resolver/CLI/System tests |
| Run settings | With/without HMR, the corresponding constraint settings | Do not confuse mass changes with bond validation | System/short restart smoke |

Reuse and extend the existing `test_pipeline_*`, `test_disulfide_metal_guard.py`,
`test_disulfide_insertion_codes.py`, `test_amber_variant_restore.py`,
`test_lipid21_xml_selection.py`, `test_solvation_server.py`, and `test_chain_selection.py`.
"Supported" in the table means confirmed with the current catalog/existing tests and turned into fixtures; do not extend
the scientific scope of support to suit this fix.

### Verify that answers do not change when IDs or representations change

Build deformed fixtures with a fixed seed and, using the mapping to the original, check the following.

- Bond partners and charges are unchanged under chain renaming, residue-number offsets, and reordering of atoms/chains.
- Adding lipid/water/ions to the same input does not mix up targets even when their chain/resnum collide with the protein's.
- The meaning of the plan is unchanged after a proper global rigid-body rotation/translation or orientation processing.
- Under legitimate aliases of the same chemical state or a PDB round-trip, residue presence and chemical meaning are unchanged.
  Actual chemical-state changes such as HIP↔HID or CYX↔CYS are not treated as "the same".
- Reapplying the same plan does not add duplicate bonds.
- Distinguish whether the user's bond specification is partial or complete according to the existing contract,
  and do not reject legitimate existing bonds merely by a simple "total count = specified count".
  Evaluate the match of the requested pairs and the legitimacy of existing additional pairs separately.

### Execution order and release gates

1. **Before implementation**, fix the baseline of existing working cases and the list of non-SMO fixtures, expected values, and tolerances.
   Choose representative cases for coverage of the changed boundaries, and do not pick only systems that passed after the fix.
2. Run small fixtures and existing tests for each implementation unit.
   Do not hide regressions by "rewriting expected values" unrelated to what was changed.
3. Run the matrix's build/pipeline tests in a real-dependency environment.
   Fetch network-dependent structures in advance and pin their hashes.
   Do not count skips due to missing dependencies as passes; run them in the required environment before moving on.
4. Short min/eq for the BPTI system and the non-SMO membrane system (existing 2LOP fixture) are required.
   A short eq for explicit systems is in principle 10 ps NVT + 20 ps NPT, with the corresponding force field, water, HMR, and
   restraint conditions fixed before running. Density convergence in that short time is not required;
   it is used to check execution, restart, and chemical preservation.
5. After the required non-SMO checks pass, run the 2.1 ns equilibration test on the fixed SMO system.
6. On the release SIF alone as well, in addition to the fixed SMO control, run the non-SMO S–S positive case, the metal negative case,
   the inspection CLI, and smoke tests with another force field/water, to rule out confusion with the overlay.
7. **If only SMO succeeds, the release is not allowed.** Publish a table of pass/fail/skip for every required case, the differences from the baseline,
   and the reasons for changes. If there is any new non-SMO regression, do not treat the work as complete until the cause is fixed.

Estimate the resource plan for the SMO 2.1 ns test and the resource plan for the added non-SMO tests separately.
Rather than running long MD on every system, choose test depths that can detect the effects of the change.
