# MDPrepBench Capability Coverage

This document enumerates the standard production MD-preparation capability
surface that any general-purpose MD-prep workflow must handle, and maps each
capability to the MDPrepBench task(s) and the **exact machine-checked check**
that verifies it. Every row is a claim backed by a deterministic check or an
artifact recompute — not a self-report. Framing is neutral: these are
capabilities, each backed by an artifact-level check, with no reference to any
particular tool or service.

## How verification works

The scorer treats the submitted artifact as the source of truth:

- OpenMM is detected by **deserializing** the `system.xml` + `topology.pdb` +
  `state.xml` triple, not by trusting a declared `topology.backend` label.
- Physical and identity properties are **recomputed** from artifacts whenever
  possible: force-field application, model/assembly choice, net charge,
  water-model fingerprint, ion molarity, component presence, and residue state.
- Normalized metadata is evaluator-generated. Prep agents submit no metrics or
  backend declarations.

Every completed preparation task must clear the **physical-validity gate** (it
appears in every task): the OpenMM system loads (`openmm_system_load`), has
finite energy (`openmm_energy_rescan`), has a force field applied to every atom
(`forcefield_applied_rescan`), passes a geometric sanity scan with no steric
clashes (`structure_geometry_quality`), and ships the required minimized
structure with a completed-minimization report (`minimization_report_check`).
Failing the gate scores the task zero. Everything below is graded partial credit
on top of the gate.

A finite energy alone is not enough: `structure_geometry_quality` reloads the
`system.xml` + `state.xml` bundle and flags non-bonded atom pairs that overlap
inside a fraction of their VDW `r_min` (bonded/exception pairs and virtual sites
excluded). Optional per-task settings extend it to bond-length outliers,
bond-angle outliers, cis non-proline peptide bonds, and inverted (D) CA
chirality, so a system that minimizes to a finite energy but still contains a
severe clash or bad geometry does not pass. A task can also promote any
deterministic check to the gate with `hard_fail: true`.

## Check-type glossary

| Check type | What it verifies (capability axis) |
| --- | --- |
| `openmm_system_load` | System deserializes and builds a context (physical_validity) |
| `openmm_energy_rescan` | Single-point energy is finite and physically plausible (physical_validity) |
| `forcefield_applied_rescan` | Every particle has nonbonded params; bonded terms exist; no NaNs (physical_validity) |
| `minimization_report_check` | Minimization attempted/completed with finite energies/positions (physical_validity) |
| `structure_geometry_quality` | No steric clashes (+ optional bond/angle outliers, cis non-proline, D-chirality) recomputed from the OpenMM bundle (physical_validity) |
| `net_charge_check` | Sum of recomputed particle charges is near-integer / neutral (physical_validity) |
| `water_model_fingerprint` | Particles-per-water + virtual sites classify the water model (fidelity) |
| `ion_concentration_recompute` | Ion residue count + box volume → molarity vs request (fidelity) |
| `structure_component_rescan` | Required residues/components present in prepared structure (identity) |
| `minimized_structure_component_rescan` | Same, in the minimized structure (identity) |
| `pdb_residue_state` | Specific residue name/atoms (mutation, PTM, protonation, capping); supports multiple accepted names/atom sets (e.g. HID vs HIE tautomers) for deterministic multi-answer scoring (identity) |
| `rmsd_recompute` | Ligand pose, NMR model, or assembly coordinates vs a scorer-side reference (fidelity) |
| `assembly_identity_check` | Chain/copy count matches the requested biological assembly (identity) |
| `pdb_no_deuterium_atoms` | No stray deuterium atoms left in the structure (identity) |
| `disulfide_bond_rescan` | Required disulfide geometry appears in the submitted structure (identity) |
| `nucleic_content_rescan` | DNA/RNA chain and residue content appear in the submitted structure (identity) |
| `solvent_regime_rescan` | Explicit, implicit, or membrane regime is visible in submitted topology/structure (fidelity) |

## Capability → task → check map

| Capability | Task(s) | Primary verifying check(s) |
| --- | --- | --- |
| Simple monomer prep + explicit solvent | P01 | `structure_component_rescan`, full physical-validity gate |
| Chain selection + ligand retention | P02 | `structure_component_rescan` (chain residues + ligand) |
| Ligand pose preservation | P03 | `rmsd_recompute` vs reference pose, `pdb_residue_state`, `structure_component_rescan` |
| Multi-ligand inclusion/exclusion | P04 | `structure_component_rescan` (kept vs excluded) |
| Charged cofactor-like ligand | P05 | `structure_component_rescan` (cofactor retained) |
| Supported metal-ion retention | P06 | `structure_component_rescan` (Ca2+ retained) |
| Crystallographic ion triage (RNA) | P07 | `structure_component_rescan` (K+ kept, waters excluded) |
| Point mutation (branched) | P08 | `pdb_residue_state` (mutated residue and WT parent residue artifact) |
| Multi-point mutation | P09 | `pdb_residue_state` (both mutations) |
| Disulfide detection/override | P10 | `disulfide_bond_rescan`, `pdb_no_deuterium_atoms`, required component-disposition artifact |
| Site-specific protonation override | P11 | `pdb_residue_state` (GLH + HE2) |
| PTM detect + restore (deposited) | P12 | `pdb_residue_state` (SEP restored) |
| PTM apply (user-requested) | P13 | `pdb_residue_state` (new SEP) |
| Glycoprotein / glycan pass-through | P14 | `structure_component_rescan` (glycans kept) |
| Standard DNA topology | P15 | `nucleic_content_rescan`, physical-validity gate |
| Standard RNA topology | P16 | `nucleic_content_rescan`, physical-validity gate |
| DNA duplex retention + neutralization | P17 | `nucleic_content_rescan`, `structure_component_rescan` (ions), `net_charge_check` |
| Mixed-lipid membrane + model selection | P18 | `rmsd_recompute` vs model-1 reference, `solvent_regime_rescan`, `structure_component_rescan` (lipids) |
| NMR model selection | P19 | `rmsd_recompute` vs model-5 reference |
| Terminal capping | P20 | `structure_component_rescan` / `minimized_structure_component_rescan` (ACE/NME caps) |
| MSE cleanup | P21 | `structure_component_rescan` (MSE absent, MET present) |
| OPC water-model fidelity | P22 | `water_model_fingerprint` (OPC), `solvent_regime_rescan` |
| Implicit vs explicit solvent | P23 | `solvent_regime_rescan`, `structure_component_rescan` (no spurious water box) |
| Biological assembly choice | P24 | `rmsd_recompute` vs assembly-1 reference, `assembly_identity_check` (four chains) |
| Specified ion concentration + neutrality | P25 | `ion_concentration_recompute` (molarity from box), `net_charge_check`, `structure_component_rescan` |
| Zinc metalloenzyme retention + His shell | P26 | `structure_component_rescan` (Zn2+ retained), `pdb_residue_state` (coordinating His), `net_charge_check` |
| Non-zinc multi-metal cofactor retention (Mn2+/Ca2+) | P27 | `structure_component_rescan` (Mn2+ and Ca2+ retained), `pdb_residue_state` (coordinating His24), `net_charge_check` |
| Custom drug-like ligand parameterization + pose | P28 | `rmsd_recompute` vs imatinib pose reference, `forcefield_applied_rescan`, `structure_component_rescan` (STI), `net_charge_check` |
| Protein-protein interface retention | P29 | `assembly_identity_check` (both partner chains), `net_charge_check` |
| Protein-DNA complex + structural metals | P30 | `nucleic_content_rescan` (DNA duplex), `structure_component_rescan` (3x Zn2+), `net_charge_check` |
| Histidine tautomer / protonation (HIP) | P31 | `pdb_residue_state` (HIP + HD1/HE2) |
| Missing side-chain reconstruction | P32 | `pdb_residue_state` (rebuilt Glu CG/CD/OE1/OE2 heavy atoms) |
| Physiological NaCl concentration + neutrality | P33 | `ion_concentration_recompute` (0.15 M NaCl), `net_charge_check`, `structure_component_rescan` (Na/Cl) |
| Anionic-lipid membrane + neutralization | P34 | `solvent_regime_rescan` (membrane), `structure_component_rescan` (POPC/POPG), `net_charge_check` |
| RNA structural Mg2+ retention | P35 | `nucleic_content_rescan` (RNA aptamer), `structure_component_rescan` (Mg2+), `net_charge_check` |
| Protein-RNA complex + zinc knuckles | P36 | `nucleic_content_rescan` (RNA), `assembly_identity_check`, `structure_component_rescan` (2x Zn2+), `net_charge_check` |
| Beta-barrel membrane protein | P37 | `solvent_regime_rescan` (membrane), `assembly_identity_check`, `unexpected_residue_rescan` (detergent excluded), `net_charge_check` |
| Implicit protein-peptide complex | P38 | `solvent_regime_rescan` (implicit), `assembly_identity_check` (two partners), `structure_component_rescan` (no explicit waters) |
| Oligomeric potassium-channel membrane | P39 | `solvent_regime_rescan` (membrane), `assembly_identity_check` (tetramer), `structure_component_rescan` (pore K+), `net_charge_check` |
| TIP3P water-model fidelity | P40 | `water_model_fingerprint` (TIP3P), `solvent_regime_rescan` (explicit), common topology/minimization checks |

## Capability axes

Each check is tagged with a capability axis and rolled up into a per-run profile:

- **identity** — the right thing was built (chains, ligands, ions, residues,
  mutations, PTMs, protonation, caps, assembly, selected candidate).
- **physical_validity** — the system is a sane MD system (loads, finite energy,
  force field applied, neutral/integer charge, minimized).
- **fidelity** — explicit requests were honored where artifact-verifiable
  (water model fingerprint, ion molarity, ligand-pose RMSD, declared metrics).
- **provenance** — the harness recorded the required execution stages. This axis
  does not use agent-authored MDPrepBench evidence or command logs.

## Candidate gaps (future work only)

The following capabilities are **not** covered by the current task set and are
noted only as candidates for a later pass (adding tasks is out of scope here):

- RNA + small-molecule ligand complexes (e.g. a riboswitch aptamer + ligand).
- Non-standard / modified nucleotides (methylated DNA, modified RNA bases).
- Alternate water-model families beyond OPC/TIP3P (TIP4P-Ew/SPC/E
  fingerprints).

Protein–protein interface retention (P29), protein–nucleic complexes with
structural metals (P30), and custom drug-like ligand GAFF/OpenFF
parameterization (P28) were previously listed here and are now covered.

These gaps do not affect the verification of the capabilities listed above; they
mark where the coverage surface could be extended.
