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
- Physical properties are **recomputed** from the system: force-field
  application, net charge, water-model fingerprint, and ion molarity.
- `metrics.json` values are cross-checked declarations; a mismatch with the
  recomputed value is an integrity warning and the recomputed value scores.

Every completed preparation task must clear the **physical-validity gate** (it
appears in every task): the OpenMM system loads (`openmm_system_load`), has
finite energy (`openmm_energy_rescan`), has a force field applied to every atom
(`forcefield_applied_rescan`), and ships the required minimized structure with a
completed-minimization report (`minimization_report_check`). Failing the gate
scores the task zero. Everything below is graded partial credit on top of the
gate.

## Check-type glossary

| Check type | What it verifies (capability axis) |
| --- | --- |
| `openmm_system_load` | System deserializes and builds a context (physical_validity) |
| `openmm_energy_rescan` | Single-point energy is finite and physically plausible (physical_validity) |
| `forcefield_applied_rescan` | Every particle has nonbonded params; bonded terms exist; no NaNs (physical_validity) |
| `minimization_report_check` | Minimization attempted/completed with finite energies/positions (physical_validity) |
| `net_charge_check` | Sum of recomputed particle charges is near-integer / neutral (physical_validity) |
| `water_model_fingerprint` | Particles-per-water + virtual sites classify the water model (fidelity) |
| `ion_concentration_recompute` | Ion residue count + box volume → molarity vs request (fidelity) |
| `structure_component_rescan` | Required residues/components present in prepared structure (identity) |
| `minimized_structure_component_rescan` | Same, in the minimized structure (identity) |
| `pdb_residue_state` | Specific residue name/atoms (mutation, PTM, protonation, capping) (identity) |
| `rmsd_recompute` | Ligand-pose RMSD vs a scorer-side reference (fidelity) |
| `assembly_identity_check` | Chain/copy count matches the requested biological assembly (identity) |
| `candidate_selection_check` | A structured source/model candidate was selected (identity) |
| `json_equals` / `json_allowed_values` / `json_min` / `json_min_length` | Declared metric cross-checks (fidelity/provenance) |
| `artifact_provenance_text` | Required decision text present in provenance/evidence (provenance) |
| `pdb_no_deuterium_atoms` | No stray deuterium atoms left in the structure (identity) |

## Capability → task → check map

| Capability | Task(s) | Primary verifying check(s) |
| --- | --- | --- |
| Simple monomer prep + explicit solvent | P01 | `structure_component_rescan`, full physical-validity gate |
| Chain selection + ligand retention | P02 | `structure_component_rescan` (chain residues + ligand), `json_equals` |
| Ligand pose preservation | P03 | `rmsd_recompute` vs reference pose, `pdb_residue_state`, `structure_component_rescan` |
| Multi-ligand inclusion/exclusion | P04 | `structure_component_rescan` (kept vs excluded), `json_equals` |
| Charged cofactor-like ligand | P05 | `structure_component_rescan` (cofactor retained), `json_equals` |
| Supported metal-ion retention | P06 | `structure_component_rescan` (Ca2+ retained), `json_equals` |
| Crystallographic ion triage (RNA) | P07 | `structure_component_rescan` (K+ kept, waters excluded), `artifact_provenance_text` |
| Point mutation (branched) | P08 | `pdb_residue_state` (mutated residue), `json_equals` |
| Multi-point mutation | P09 | `pdb_residue_state` (both mutations), `json_equals` |
| Disulfide detection/override | P10 | `json_min` / `json_min_length` (SS bonds), `pdb_no_deuterium_atoms`, `json_equals` |
| Site-specific protonation override | P11 | `pdb_residue_state` (GLH + HE2), `json_equals` |
| PTM detect + restore (deposited) | P12 | `pdb_residue_state` (SEP restored), `json_equals` |
| PTM apply (user-requested) | P13 | `pdb_residue_state` (new SEP), `json_equals` |
| Glycoprotein / glycan pass-through | P14 | `structure_component_rescan` (glycans kept), `artifact_provenance_text` |
| Standard DNA topology | P15 | physical-validity gate, `json_equals` (nucleic FF) |
| Standard RNA topology | P16 | physical-validity gate, `json_equals` (RNA FF) |
| DNA duplex retention + neutralization | P17 | `structure_component_rescan` (both chains + ions), `json_equals` |
| Mixed-lipid membrane + model selection | P18 | `candidate_selection_check`, `structure_component_rescan` (lipids), `json_allowed_values` |
| NMR model selection | P19 | `candidate_selection_check` (model 5), `json_equals` |
| Terminal capping | P20 | `structure_component_rescan` (ACE/NME caps), `json_equals` |
| Altloc / MSE / numbering cleanup | P21 | `structure_component_rescan`, `json_equals` |
| Force-field + water-model fidelity | P22 | `water_model_fingerprint` (requested model), `json_equals` |
| Implicit vs explicit solvent | P23 | `structure_component_rescan` (no spurious water box), `json_equals` |
| Biological assembly choice | P24 | `assembly_identity_check` (chain/copy count), `json_equals` |
| Specified ion concentration + neutrality | P25 | `ion_concentration_recompute` (molarity from box), `net_charge_check`, `structure_component_rescan` |

## Capability axes

Each check is tagged with a capability axis and rolled up into a per-run profile:

- **identity** — the right thing was built (chains, ligands, ions, residues,
  mutations, PTMs, protonation, caps, assembly, selected candidate).
- **physical_validity** — the system is a sane MD system (loads, finite energy,
  force field applied, neutral/integer charge, minimized).
- **fidelity** — explicit requests were honored where artifact-verifiable
  (water model fingerprint, ion molarity, ligand-pose RMSD, declared metrics).
- **provenance** — non-obvious decisions and execution evidence are recorded.

## Candidate gaps (future work only)

The following capabilities are **not** covered by the current task set and are
noted only as candidates for a later pass (adding tasks is out of scope here):

- Protein–protein complex preparation (interface retention across partners).
- RNA + small-molecule ligand complexes.
- Stressed custom-ligand GAFF parameterization for non-standard chemistries.

These gaps do not affect the verification of the capabilities listed above; they
mark where the coverage surface could be extended.
