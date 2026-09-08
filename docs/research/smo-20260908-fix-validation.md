# SMO chemical-state fix: implementation and acceptance

2026-09-08. Implements [the approved plan](../developer/smo-root-cause-fix-plan.md), based on [the investigation](smo-20260908-bug-investigation.md).

## What changed

- A shared disulfide resolver validates exact chain/residue/insertion-code identities and partners before adding any bond. Hydrogen-bearing reduced CYS, CYM, orphan CYX, missing/ambiguous sites and conflicting partners produce `disulfide_chemistry_conflict`. Actual resolved/emitted pairs replace the previous advisory plan.
- Membrane neutralization and final topology receive the same nearest-prep chemistry through the DAG. Charge remains the sum of force-field NonbondedForce charges; missing/nonfinite/nonintegral charge results now fail explicitly. No SMO-specific charge, distance, residue number or patch identifier was added to production code.
- Final validation checks requested pairs in both Topology and System, including constraints. Loader conservation compares prepared-input heavy-atom identities and residue count with the loaded topology. It does not merely compare output artifact sizes.
- Public/internal inspection and splitting share AMBER alias classification and sequence conversion. Caps and supported inspection aliases for modified residues are explicit. This is distinct from claiming force-field support for every inspection alias.
- GLYCAM pipeline testing exposed cpptraj renumbering of protein residues. Disulfide identities now follow unique, complete heavy-atom name/coordinate signatures through that coordinate-preserving transformation; ambiguous mappings fail.
- Skills share chemistry diagnostics distinguishing residue inventory, sequence length, display selections, executed loader, saved System and manual DAG interventions.

The originally alleged physical deletion of 29 SMO residues was **not reproduced** in the user's saved outputs. This implementation corrects the proven inspection inconsistency and adds conservation guards; it does not assert that the unverified deletion narrative was established. Inaccessible reporting history remains unresolved.

## Same-SMO acceptance

Isolated study: `/home/rku00161/mdclaw/.validation/smo-20260908`.
Original files under `/data1/rkp00079/rku00140/structures1/SMO_WT_active_6XBL` were read only. Source/prep and the saved oriented structure were imported as explicit fixtures, not represented as newly executed preparation/orientation. Normal CLI/DAG then executed embed, topology, minimization and equilibration; no manual ion replacement or alternative topology builder was used.

Slurm account: **rkp00079**. Main job 87152; separate trajectory-observation branch 87258. ff19SB/lipid21/OPC, HMR off, 2 fs, 300 K, 1 bar, 0.1 ns NVT + 2.0 ns NPT. The normal 1,000-step/2 ps low-temperature warmup also ran. The observation branch adds test-only reporters and snapshots, without changing forces, integration or random numbers.

| Independent check | Result |
| --- | --- |
| Protein residues / heavy atoms | 476 / 3,738 |
| Variants retained | HID × 8, CYX × 18, ASH × 1, GLH × 2 |
| Declared S–S pairs / peptide C–N bonds | 9 / 475; exact partners preserved |
| Charge before neutralization | +1.999999999999593 e |
| Ions | Na 82, Cl 84 |
| Total atoms | 157,610 |
| Final System charge | −4.82 × 10⁻¹⁴ e |
| Minimized SG–SG distances | 2.021–2.051 Å |
| Final SG–SG distances | 1.950–2.100 Å |
| Saved trajectory | 105 frames; all finite, all 9 S–S and 475 C–N distances within predeclared limits |
| NPT last 10% mean temperature | 300.200 K |
| NPT last 10% mean density | 1.028664 g/mL |
| Final volume | 1,240.150 nm³ |
| Barostat | MonteCarloMembraneBarostat, XYIsotropic / ZFree |

The six-atom difference from 157,616 is expected: the correct automatic ion count replaces two additional four-site OPC waters with two ions. Protein atoms are retained, allowing only the recorded terminal H1→H PDB naming alias. Original bypass-run coordinates, density and energy are not exact numerical goldens for a regenerated system. Top/side membrane views were inspected as supplementary evidence; they do not replace bond/charge tests.

Evidence: study `fixture.json`, `validation/runtime.json`, `validation/stage_nodes.json`, `validation/commands.jsonl`, `validation/independent-audit.json`, `validation/npt-statistics.png`, `validation/membrane-views.png`, immutable node artifacts/events. Reproduction and independent audit: `scripts/validate_smo_fix.py`, `scripts/audit_smo_acceptance.py`.

Additional original-input controls:

- Missing plan for oxidized SMO fails with orphan CYX at A490/A507 instead of accepting the wrong charge.
- HG-bearing old input with an S–S request fails before parameterization; the same reduced input without the request succeeds with zero disulfides.
- User topo_005, topo_006 and topo_007 all give 476 residues and sequence length 476 in public CLI and internal inspection; each split PDB contains 476 CA atoms. Evidence in `/tmp/smo-bug-audit/fixed-selection`.

## Non-SMO regression and runtime evidence

Counts below are separate suites, with overlap; they must not be added as a count of unique tests.

| Suite | Result / evidence |
| --- | --- |
| Broad affected unit, DAG, CLI, guardrail and server smoke | 639 passed; job 87260, `.validation/regression-20260908` |
| BPTI, 2LOP, DNA, RNA, phosphorylated, metal and ligand pipelines | 25 passed initially; GLYCAM failure fixed and its 3-case pipeline rerun passed (jobs 87154, 87243) |
| Variant, terminal, exact identity, inspection and glycan boundaries | 111 passed; initial additional lipid fixture failed before its final correction (job 87541) |
| Mixed POPC/POPE/CHL1 with OPC/TIP3P and charged POPG | 3 passed, job 87560; `.validation/final-acceptance-20260908/junit.xml` |
| BPTI old/new physical equivalence | 22,698 particles; System XML byte-identical; common-state Reference energy and maximum force differences both exactly zero |
| Candidate standalone SMO | Installed source hashes match; +2 e / 9 S–S with plan, stable chemistry error without plan |
| Candidate container smoke | 27 passed, zero failed |
| Shared candidate GPU smoke | OpenMM CUDA/PME, cuFFT prefault and PyTorch FFT passed; job 87562 |
| Lint / skill structure | Ruff passed; md-prepare, md-analyze, md-report skill validators passed |

Variant System tests cover HID/HIE/HIP, ASH/GLH, LYN/CYM/CYS under both ff19SB/OPC and ff14SB/TIP3P, plus charged terminal CYX. BPTI and non-SMO membrane 2LOP include short minimization/equilibration. None of these constitutes an exhaustive test of every chemical combination.

The POPG fixture initially assumed salt settings, CRYST1, element columns and Na-only neutralization that native Packmol output does not guarantee. The final fixture strips its automatically added K+ as well as Na/Cl and independently counts 28 P31 atoms: expected charge −28 e. These were test-construction corrections, not product special cases. The first CPU equivalence calculation showed parallel floating-point force accumulation differences; the strict comparison uses the Reference platform at identical coordinates, not independently minimized output states.

Four pre-existing missing guardrail registrations (`mddb_export_failed`, `mddb_metadata_required`, `report_invalid_input`, `report_selection_required`) were included to restore the registry/golden contract; they are unrelated to the SMO chemistry fixes.

## Packaged artifact and deployment

Packaged Python source tree SHA-256: `561049fe825429b42086115b672d46bf79f51c6011969b62068f1f1f9e088c76`.
Base checkout: `fce3dbcd14d0e4ab5e6c84a8275b5801580a2213`; changes are represented by the source manifest, not claimed to be contained in that commit. Base shared SIF was `6f171d2f0fa5`. Installed MDClaw imports from `/opt/mdclaw/lib/python3.12/site-packages/mdclaw`, without checkout overlay. This is a cluster-local hotfix, not a tagged/GHCR release; package version remains 0.6.8 and the separate source manifest identifies the actual code.

Standalone evidence and exact old/new comparison live under `/tmp/mdclaw-sulfur-candidate`. Shared deployment status and image checksums are recorded in `deployment.json` and sidecars after the final GPU check. Skills remain repository/plugin content; replacing a runtime SIF alone does not update another user's skill checkout.

### Shared SIF activated

The existing path `/data1/rkp00079/mdclaw-rikyu-arm64-cuda130-cufft121-fusefix-6f171d2f0fa5.sif` now points to `mdclaw-rikyu-arm64-cuda130-cufft121-fusefix-sulfur-561049fe8254.sif` in the same directory. The old revision-bearing filename is a compatibility alias, not a statement of the active source revision.

- Active SIF SHA-256: `c4073304b856f66fc322f1a0e5c497441fab5d645c6760df04b18a5d9583cd8a`.
- Original backup: `/data1/rkp00079/mdclaw-rikyu-arm64-cuda130-cufft121-fusefix-6f171d2f0fa5.pre-sulfur-fix-20260908.sif`.
- Original SHA-256: `83a351ea43ab4f48418a13e395703a326b0b9f72ea9f12e68abfe59ab537c5a0`.
- The backup preserves the previous image inode; the compatibility symlink was switched atomically. Running jobs using the already-open old image are not rewritten.
- Image checksum, source manifest and deployment JSON are sidecars beside the new shared SIF. Rollback consists of atomically repointing the compatibility symlink to the backup.

**Checkout precedence:** the reporter's `bin/mdclaw` explicitly puts its own checkout on `PYTHONPATH`. It therefore overrides the Python code baked into the SIF. Updating only the shared image does not apply these fixes when launching that old wrapper/checkout. Use the updated checkout (including skills) for wrapper-based operation, or explicitly launch the installed SIF CLI without an old checkout on `PYTHONPATH` and without using that checkout as the Python working directory. The reporter's repository and completed simulation nodes were not modified. No GitHub/GHCR publication was performed.
