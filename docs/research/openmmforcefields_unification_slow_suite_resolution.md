# openmmforcefields-unification Slow-Suite Resolution

Date: 2026-05-09

This note records the layered set of fixes that closed the slow/integration
test suite on `feat/openmmforcefields-unification`. It is a developer-facing
companion to the inline comments in `mdclaw/amber_server.py` and
`mdclaw/structure_server.py`. Read it before refactoring
`_run_openmmforcefields_build` — the layers are intentional and the order
matters.

Originally (slow suite run on 2026-05-08) **8 tests** failed:

- `tests/test_server_smoke.py::TestMDSimulationServer::test_run_production_xml_restart_cross_ensemble_npt_state_into_nvt`
- `tests/test_pipeline_metal_dag.py::TestPipelineMetalDag::test_step4_topology_auto_resolves_metal_params`
- `tests/test_pipeline_phospho_dag.py::TestPipelinePhosphoDag::test_step5_topology_loads_phosaa`
- `tests/test_pipeline_3pwb_ligand_dag.py::TestPipeline3PWBLigandDag::test_step5_topology_with_ligands`
- `tests/test_pipeline_3pwb_ligand_dag.py::TestPipeline3PWBLigandDag::test_step6_equilibration`
- `tests/test_pipeline_3pwb_ligand_dag.py::TestPipeline3PWBLigandDag::test_step7_production`
- `tests/test_pipeline_membrane_dag.py::TestPipelineMembraneDag::test_step4_membrane_topology`
- `tests/test_pipeline_glycoprotein_dag.py::TestPipelineGlycoproteinDag::test_step3_topology_loads_glycam`

Plus the entire `tests/test_benchmark/*` collection was failing to import
because of a `pydantic` / `pydantic_core` version mismatch. All 8 + the
benchmark collection are now green.

Fixes landed in three commits on `feat/openmmforcefields-unification`:

- `8c2256f fix(unification): pressure_bar=0 NVT semantics + phosaa14SB / metal contracts`
- `66f39b4 fix(unification): close 7/8 slow-suite gaps via Pablo + topology-bond patcher + phospho-atom placement`
- `45ff0c9 fix(unification): drop orphan GLYCAM residues iteratively to close glycoprotein gap`

## Root cause patterns

The eight failures clustered around three patterns inherited from the
legacy `tleap` path that did not translate to the openmmforcefields one:

1. **`tleap` materialised atoms; openmmforcefields does not.**
   `SystemGenerator.create_system` only assigns parameters to *existing*
   atoms — it cannot add a missing phosphate, fill in a backbone H, or
   close a peptide bond around a non-standard residue. Anything the
   prep stages had been emitting in a "tleap will rebuild it" form
   needed an explicit upstream rebuild.
2. **Pablo speaks CCD; mdclaw prep speaks Amber.**
   `OpenFF Pablo`'s CCD-based loader is strict about residue and atom
   names. The prep pipeline (`pdb2pqr`, `cpptraj prepareforleap`,
   `packmol-memgen`) emits Amber-tradition names — `HID`/`HIE`/`HIP`,
   `Na+`/`Cl-`/`K+`, GLYCAM `0YB`/`4YA`/`4YB`/`NLN`, lipid21
   `PA`/`PC`/`OL` — that Pablo cannot match by name. When Pablo bails
   it falls back to `openmm.app.PDBFile`, which loads atoms but does
   not bond non-standard residues.
3. **Run-side defaults that silently translated `0` into "NPT".**
   `pressure_bar=0` should mean NVT (and is documented as such in
   `_effective_pressure_bar` and the `run_equilibration` docstring),
   but three sites in `md_simulation_server.py` only checked
   `is not None` and added a 0-bar barostat. That kept the runtime
   System in the NPT ensemble and prevented the
   `_detect_ensemble_mismatch` warning from firing on
   NPT-state-into-NVT-system restarts.

## Per-test resolution

### `cross_ensemble_npt_state_into_nvt` — `pressure_bar=0` NVT semantics

Three barostat-add sites in `mdclaw/md_simulation_server.py` were
gated on `pressure_bar is not None`, so a caller passing
`pressure_bar=0` got a `MonteCarloBarostat` at zero pressure plus
`ensemble = "NPT"`. The test correctly expects an NVT runtime and
relies on `_detect_ensemble_mismatch` to surface a warning when the
restart state has barostat parameters; with the spurious barostat in
place, neither condition fired. Tightened all three sites to
`pressure_bar is not None and pressure_bar > 0` so the convention
now matches `_effective_pressure_bar` and the docstring's "0 or
None: NVT" rule.

### `metal_dag::test_step4` — pin the structured fail-fast

The `parm7` retirement (PR `1aa3309`) replaced the metal /
modxna build paths with a structured fail-fast
(`metal_openmm_xml_required` / `modxna_openmm_xml_required`); the
test still asserted the legacy success contract. Renamed the test
and asserted the structured `code` per CLAUDE.md guidance to branch
on stable codes rather than human-readable error strings.

### `phospho_dag::test_step5` — three layered issues

This single test exercised three independent gaps:

- `protein.ff14SB.xml` (openmmforcefields-canonical, prefixed atom
  types like `protein-N`) and `phosaa14SB.xml` (unprefixed atom
  types like `N`) cannot be loaded together — `app.ForceField`
  raises `KeyError: 'N'`. The catalog comment (`forcefield_catalog.py`
  ff14SB entry) had already documented this; the test now uses the
  catalog-recommended `ff19SB` + `opc` + `phosaa19SB` combination.
  An explicit `phospho_forcefield_atom_type_mismatch` fail-fast in
  `build_amber_system` keeps callers who *do* request `ff14SB` from
  hitting the cryptic upstream KeyError.
- Pablo cannot match Amber `HID` / `HIE` / `HIP` against CCD `HIS`.
  The PDB sanitiser in `_run_openmmforcefields_build` rewrites them
  to `HIS` for the load and restores the Amber variant from each
  residue's `HD1` / `HE2` atoms after Pablo returns so
  `protein.ff*.xml`'s protonation-specific templates apply.
- `phosphorylate_residues` only renamed `SER → SEP` (and analogues)
  and dropped the hydroxyl H, expecting tleap to materialise the
  phosphate atoms. SystemGenerator does not. Implemented
  `_compute_phospho_atom_coords` in `structure_server.py` to place
  `P` along the parent_C → ester_O axis and `O1P` / `O2P` / `O3P`
  on a tetrahedral frame; `HOP2` / `HOP3` are added too because
  Pablo's CCD `PHOSPHOSERINE` ships in the protonated form, then
  stripped back out with `Modeller.delete` after Pablo loads so the
  Amber dianion phosaa templates (no proton on phosphate oxygens)
  apply during `create_system`.

### `3pwb_ligand_dag::test_step5/6/7` — three layered issues

The original cryptic
`No template found for residue 223 (BEN). The set of atoms is similar to NTRP`
turned out to be three problems stacked on top of each other:

- `Molecule.from_file(mol2)` raises `NotImplementedError` in a
  RDKit-only OpenFF registry (which mdclaw's environment is). The
  loop that built `ligand_molecules` swallowed the exception as a
  warning and handed `molecules=None` to `SystemGenerator`. Replaced
  the loader with a ParmEd → RDKit → `Molecule.from_rdkit` route
  that preserves TRIPOS bond orders (a PDB round-trip would lose
  the benzene aromaticity and the amidine `C=N`, which are exactly
  what the GAFF graph match needs); failure now fail-fasts with
  `ligand_mol2_load_failed` instead of swallowing the error.
- `packmol-memgen` ships `Na+` and `Cl-` as the residue / atom names
  for ions. Pablo's CCD only knows `NA` and `CL`. Pablo bailed on
  the topology and the PDBFile fallback then could not bond ligand
  residues. The PDB sanitiser rewrites the ion names so Pablo
  succeeds.
- After Pablo loads correctly, GAFFTemplateGenerator still needs the
  ligand SMILES so it can register the residue. Pass each ligand's
  `Molecule.to_smiles()` to `_topology_pablo.load_topology` via
  `extra_smiles=[(residue_name, smiles), …]` so Pablo's
  graph-matching identifies BEN / GOL etc.

### `membrane_dag::test_step4` — lipid21 has bonds, packmol does not

`packmol-memgen` does not write CONECT records for lipid21 residues
(`PA` / `PC` / `OL`), and `openmm.app.Topology._proteinResidues` does
not auto-bond them. The result is a topology of correctly-positioned
atoms with zero internal bonds, and `SystemGenerator.create_system`
fails with "the residue has no bonds between its atoms". Two pieces:

- **Intra-residue bond patcher**: walk the topology after the
  SystemGenerator loads its `ForceField`, find any residue that has
  a matching `_templates` entry but no internal bonds, and copy the
  template's bond list onto the topology atoms (matched by name).
  Patches ~26 000 lipid bonds on a typical membrane build.
- **External-bond patcher with name preference**: each `lipid21`
  template advertises `externalBonds` (`PA.C12`, `PC.C11`,
  `PC.C21`, `OL.C12`); pair them across residues by spatial
  proximity (2.0 Å heavy-atom cutoff). A pure greedy nearest-neighbour
  match was tricked by `packmol-memgen`'s pre-min lipid stacking
  where a leaflet's `PC.C21` can sit 1.37 Å from a neighbour leaflet's
  `PC.C21` (closer than the true `OL.C12` partner at 1.52 Å). The
  pairing runs in two passes — first restricted to differently-named
  residue pairs (`PC.C21 ↔ OL.C12`, `PA.C12 ↔ PC.C11`), then a
  fallback pass that admits same-name pairings for legitimate
  glycan-glycan polymerisation.

### `glycoprotein_dag::test_step3` — the deepest cascade

`cpptraj prepareforleap` emits GLYCAM residue codes
(`0YB` / `4YA` / `4YB` / `NLN` …) that are *not* CCD entries, so
Pablo bails on the entire topology. Several independent fixes had
to land before the test could pass:

- **`PDBFixer.addMissingHydrogens` skipped when input already
  hydrogenated**: PDBFixer's H pass goes through
  `Modeller.addHydrogens`, which knows only standard amino acids and
  nucleotides. For unknown residues like `BEN` it pulls a CCD
  template and *appends* a duplicate H of every standard name, so
  every antechamber-already-hydrogenated ligand ended up with two
  copies of `H2`/`H3`/`HN1`/etc. Skip the pass when the input
  contains any `H` element.
- **Intra- and external-bond patchers** (same as the membrane case)
  also handle GLYCAM residues since their templates ship with
  `GLYCAM_06j-1.xml`.
- **`glycam-hydrogens.xml` loaded before `addHydrogens`**:
  `Modeller.addHydrogens(forcefield=sg.forcefield)` cannot place
  NLN's `H` / `HA` / `HB2` / `HB3` / `HD21` without a hydrogen
  definition. OpenMM ships `glycam-hydrogens.xml` covering NLN /
  0YB / 4YA / 4YB; load it explicitly via
  `Modeller.loadHydrogenDefinitions` before the pass.
- **NLN orphan salvage**: cpptraj's `prepareforleap` writes a NLN at
  every detected N-glycan attachment site, but the matching glycan
  chain may end up spatially detached after the merge step. With no
  glycan partner within bond range the residue is functionally a
  plain ASN — rename it back so addHydrogens places HD22 from the
  ASN template and the protein FF matches the side chain.
- **Iterative orphan glycan removal**: dropping one isolated glycan
  leaves its neighbour glycan with a newly-unpaired external bond
  (chain-leaf cascade). Walk the GLYCAM residues, recompute
  realised cross-residue bonds from `omm_topology.bonds()` each
  pass, drop any residue whose realised count is below its
  template's `externalBonds`. Cap at 8 passes for safety.

## Companion env-side fix

`pydantic 2.12.5` paired with `pydantic_core 2.41.5` raises
`ImportError: cannot import name 'validate_core_schema' from
'pydantic_core'` and breaks the entire `tests/test_benchmark/*`
collection. `pip install -U pydantic` pulled `pydantic 2.13.4` +
`pydantic_core 2.46.4` which restored the collection. The
`pyproject.toml` pin `pydantic>=2.12.3` was left as-is — pip
resolves a compatible `pydantic_core` transitively when the env
is built from scratch; only the partially-upgraded local env hit
the mismatch.

## Read order for future maintainers

When auditing `_run_openmmforcefields_build`, read the inline
comments in this order — each layer assumes the previous ones have
already run:

1. PDBFixer hydrogenation conditional (`input_has_hydrogens`)
2. ParmEd-based ligand mol2 loader (`_load_ligand_molecule`)
3. Pablo SMILES feed for non-CCD ligands
4. PDB sanitiser (`Na+` / `Cl-` / `K+` / `HID` / `HIE` / `HIP` →
   CCD names; restore HID-variant after load)
5. Strip phospho HOP2 / HOP3 from SEP / TPO / PTR after Pablo
6. Intra-residue bond patcher (template-driven)
7. Two-pass external-bond patcher (cross-name preferred)
8. NLN orphan → ASN salvage
9. Iterative orphan-glycan removal (`_GLYCAN_RESNAMES`)
10. `glycam-hydrogens.xml` load + `Modeller.addHydrogens`
11. `Modeller.addExtraParticles`
12. `SystemGenerator.create_system`

The matching prep-side change is `_compute_phospho_atom_coords` /
`_emit_phospho_atoms` in `structure_server.py`, which is what makes
step 5's strip-back-to-dianion possible — the synthesised HOP2 /
HOP3 only exist for the duration of the Pablo load.

## Known follow-ups (not blocking)

- The orphan-NLN / orphan-glycan paths are workarounds for prep-side
  spatial-detachment bugs. The "real" fix lives upstream in
  `cpptraj prepareforleap` invocation or chain-merge alignment, not
  the topology builder. Warnings (`Renamed N NLN residue(s) ...`,
  `Dropped N orphan GLYCAM residue(s) ...`) surface so operators can
  tell when these triggered.
- `_GLYCAN_RESNAMES` is a hard-coded shortlist. Extending GLYCAM
  coverage (e.g. additional sialic / fucose linkages) means adding
  to this set or generalising to "any residue whose template comes
  from `GLYCAM_06j-1.xml`".
- `metal_openmm_xml_required` and `modxna_openmm_xml_required` are
  still intentional fail-fasts. The `build_openmm_system` research
  escape hatch + a pre-converted OpenMM ForceField XML remains the
  documented workaround until the ParmEd → OpenMM XML bridge ships
  in `forcefield_catalog`.
