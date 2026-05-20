# MDClaw Prep Workflow Flowchart

Date: 2026-05-20

This note summarizes the current MDClaw preparation implementation as read
from `skills/md-prepare/SKILL.md`, `mdclaw/structure_server.py`,
`mdclaw/solvation_server.py`, `mdclaw/amber_server.py`,
`mdclaw/openmm_system_server.py`, and the developer tool reference. It is a
flowchart-oriented map for future refactors and benchmark discussions.

Rendered SVG diagrams:

- [Prep workflow overview](prep_workflow_overview.svg)
- [prepare_complex detail](prep_workflow_prepare_complex_detail.svg)
- [Prep DAG schema and artifacts](prep_workflow_dag_schema_artifacts.svg)

![Prep workflow overview](prep_workflow_overview.svg)

![prepare_complex detail](prep_workflow_prepare_complex_detail.svg)

![Prep DAG schema and artifacts](prep_workflow_dag_schema_artifacts.svg)

## Scope

"Prep" in the user-facing workflow spans four implementation layers:

1. source normalization and candidate selection,
2. `prepare_complex` and optional prep branches,
3. solvation or membrane embedding,
4. topology build plus short minimization evidence.

The important contract boundary is that atom identity, residue identity,
protonation, terminal caps, standard nucleic hydrogens, ligand chemistry, and
component disposition are prep-owned. Topology builders do not run generic
PDBFixer repair or generic `Modeller.addHydrogens`; they validate the prepared
input, add force-field-required extra particles, apply topology-only specialty
normalization where needed, and serialize the OpenMM artifact triple.

## High-Level Flow

```mermaid
flowchart TD
  A["source node / raw PDB, mmCIF, AF, local, or prediction"] --> B["source_bundle.json and candidate files"]
  B --> C{"candidate selection needed?"}
  C -->|"single candidate"| D["prepare_complex"]
  C -->|"assembly / NMR / ensemble"| E["select source candidate"]
  E --> D

  D --> F{"optional prep branch?"}
  F -->|"mutation"| F1["create_mutated_structure via HPacker"]
  F -->|"SEP/TPO/PTR restore or edit"| F2["phosphorylate_residues"]
  F -->|"none"| G["prepared merged_pdb"]
  F1 --> G
  F2 --> G

  G --> H{"solvent mode"}
  H -->|"explicit water default"| I["solvate_structure"]
  H -->|"membrane"| J["embed_in_membrane"]
  H -->|"implicit or vacuum"| K["use merged_pdb directly"]

  I --> L["solvated_pdb + box_dimensions"]
  J --> L
  K --> M["prepared PDB without box_dimensions"]

  L --> N["build_amber_system"]
  M --> N
  M --> O["build_openmm_system research escape hatch"]

  N --> P["system.xml + topology.pdb + state.xml"]
  O --> P
  P --> Q["minimization_report.json"]
  Q --> R["handoff to equilibration / benchmark submission"]
```

## `prepare_complex` Detail

```mermaid
flowchart TD
  A["resolve source candidate / explicit structure_file"] --> B["inspect_molecules"]
  B --> C["detect PTMs and glycan link records"]
  C --> D["aggregate disulfide plan"]
  D --> E["split_molecules"]
  E --> X["component disposition: exclude deuterium from all component files"]

  X --> P{"protein chains"}
  P --> P1["clean_protein"]
  P1 --> P3["PDBFixer: missing residues, caps, nonstandard residues, heterogens, missing atoms"]
  P3 --> P4["CYS/CYX disulfide handling"]
  P4 --> P5["pdb2pqr + propka protonation or pdb4amber fallback"]
  P5 --> P6["site-specific protonation override with OpenMM Modeller variants"]
  P6 --> P7["ACE/NME cap H completion with non-cap H invariant check"]

  X --> N{"standard nucleic chains"}
  N --> N1["OpenMM Modeller.addHydrogens"]
  N1 --> N2["DNA.OL15 or RNA.OL3"]
  N --> N3["modified DNA/RNA: structured unsupported failure"]

  X --> G{"glycan chains"}
  G --> G1["pass through unchanged"]
  G1 --> G2["record glycan metadata and linkage provenance"]

  X --> L{"ligand chains"}
  L --> L1["clean_ligand"]
  L1 --> L2["write cleaned ligand PDB/SDF"]
  L2 --> L3["write ligand_chemistry.json for topology"]

  X --> I{"ion chains"}
  I -->|"explicit or vacuum / unspecified"| I1["retain supported explicit ions"]
  I -->|"implicit solvent intent"| I2["exclude explicit ions and record component_disposition"]

  P7 --> M["merge_structures"]
  N2 --> M
  G2 --> M
  L3 --> M
  I1 --> M
  I2 --> O5

  M --> O["merged.pdb"]
  M --> O1["chain_identity_map.json"]
  M --> O2["residue_mapping.json / glycan_metadata.json"]
  M --> O3["glycan_linkages.json"]
  M --> O4["disulfide_bonds.json"]
  M --> O5["component_disposition.json / excluded_components.json"]
  M --> O6["preparation_summary and confirmation_needed"]
```

Implementation anchors:

- `prepare_complex(...)` starts at `mdclaw/structure_server.py:4864`.
- `clean_protein(...)` starts at `mdclaw/structure_server.py:2148`.
- Standard nucleic H rebuild is in `_prepare_standard_nucleic(...)` at
  `mdclaw/structure_server.py:3680`.
- Terminal cap H completion is in
  `_complete_terminal_cap_hydrogens_with_modeller(...)` at
  `mdclaw/structure_server.py:3524`.
- Deuterium/component disposition starts with
  `_is_deuterium_atom_record(...)` and
  `_exclude_deuterium_atoms_from_pdb(...)` near
  `mdclaw/structure_server.py:102`, and is applied to split component files
  before component-specific preparation.
- `merge_structures(...)` starts at `mdclaw/structure_server.py:3919`.

## Optional Prep Branches

```mermaid
flowchart LR
  A["prepared merged_pdb"] --> B{"branch type"}
  B -->|"mutation"| C["create_mutated_structure"]
  C --> C1["HPacker mutation + nearby side-chain repack"]
  C1 --> C2["mutated_pdb registered as merged_pdb for downstream solv"]

  B -->|"phosphorylation"| D["phosphorylate_residues"]
  D --> D1["SER/THR/TYR to SEP/TPO/PTR with phosphate atoms"]
  D1 --> D2["phosphorylated PDB registered as merged_pdb"]

  B -->|"modified DNA/RNA"| E["standard topology path does not support it"]
  E --> E1["structured unsupported_modified_nucleic_residue / modXNA research path only"]
```

`create_mutated_structure(...)` starts at
`mdclaw/structure_server.py:6317`, and `phosphorylate_residues(...)` starts at
`mdclaw/structure_server.py:6975`.

## Solvation And Membrane Layer

```mermaid
flowchart TD
  A["merged_pdb from prep branch"] --> B{"requested environment"}

  B -->|"explicit water default"| C["solvate_structure"]
  C --> C1["packmol-memgen with water model, buffer, salt"]
  C1 --> C2{"neutralization needs more ions than saltcon?"}
  C2 -->|"yes"| C3["rerun with --salt_override and record warning"]
  C2 -->|"no"| C4["normal output"]
  C3 --> C5["solvated_pdb + box_dimensions + solvation_metadata"]
  C4 --> C5
  C5 --> C6["restore solute identity columns after packmol renumbering"]

  B -->|"membrane"| D["embed_in_membrane"]
  D --> D1["packmol-memgen lipids + water + optional salt"]
  D1 --> D2["same salt_override fallback when required"]
  D2 --> D3["solvated_pdb + box_dimensions + membrane_metadata"]

  B -->|"implicit solvent"| E["skip explicit solvation"]
  E --> E0["prepare_complex solvent_type=implicit excludes explicit ions"]
  E0 --> E1["build_amber_system consumes merged_pdb + implicit_solvent"]
  E1 --> E2["topology still blocks any remaining explicit ions"]

  B -->|"vacuum research path"| F["skip explicit solvation"]
  F --> F1["build_amber_system consumes merged_pdb without box or GB"]
```

Implementation anchors:

- `solvate_structure(...)` starts at `mdclaw/solvation_server.py:627`.
- `embed_in_membrane(...)` starts at `mdclaw/solvation_server.py:1096`.
- Salt override fallback is implemented in
  `_record_salt_override_fallback(...)` near
  `mdclaw/solvation_server.py:413`.
- Solute identity restoration is implemented in
  `_restore_packmol_solute_identity(...)` near
  `mdclaw/solvation_server.py:459`.

## Curated Topology Layer: `build_amber_system`

```mermaid
flowchart TD
  A["solvated_pdb + box_dimensions or merged_pdb + implicit/vacuum"] --> B["resolve node inputs and auto-load artifacts"]
  B --> B1["ligand_chemistry.json"]
  B --> B2["disulfide_bonds.json"]
  B --> B3["glycan_metadata.json / glycan_linkages.json"]
  B --> B4["box_dimensions.json"]

  B --> C["validate forcefield, water model, solvent regime"]
  C --> C1{"implicit + explicit box?"}
  C1 -->|"yes"| C2["fail: implicit_solvent_explicit_box_conflict"]
  C1 -->|"no"| D["scan input content"]

  D --> D1["standard nucleic: add DNA/RNA XML bundle"]
  D --> D2["modified nucleic without supported params: fail"]
  D --> D3["glycan: select GLYCAM bundle"]
  D --> D4["PTM: select phosaa XML when SEP/TPO/PTR present"]
  D --> D5["ligand: validate ligand_chemistry"]

  D3 --> E{"glycan present?"}
  E -->|"yes"| E1["cpptraj prepareforleap for GLYCAM conversion and bond plan"]
  E -->|"no"| F["resolve OpenMM XML bundle"]
  E1 --> F

  F --> G["geostd ligand XML lookup"]
  G --> G1{"geostd compatible with recorded ligand charge/atom count?"}
  G1 -->|"yes"| G2["use topology-time amber_geostd XML"]
  G1 -->|"no or missing"| G3["GAFFTemplateGenerator"]

  G2 --> H["Pablo load"]
  G3 --> H
  H --> H1["sanitize ions and Amber protonation variants for Pablo, then restore"]
  H1 --> H2["pass ligand SMILES from OpenFF Molecule records"]

  H2 --> I["manual topology bonds"]
  I --> I1["disulfide SG-SG bonds"]
  I --> I2["GLYCAM bond plan and glycan-only H completion"]
  I --> I3["template internal/external bond patching for ligands, lipids, glycans"]

  I --> J["Modeller.addExtraParticles only"]
  J --> K["SystemGenerator.create_system"]
  K --> L["short OpenMM minimization"]
  L --> M["atomic write system.xml + topology.pdb + state.xml + minimization_report.json"]
```

Implementation anchors:

- `build_amber_system(...)` starts at `mdclaw/amber_server.py:2577`.
- GLYCAM prepareforleap is in `_prepare_glycam_pdb_with_cpptraj(...)` at
  `mdclaw/amber_server.py:2389`.
- GLYCAM bond-plan parsing and normalization are in
  `_parse_glycam_leap_bond_plan(...)`,
  `_resolve_glycam_bond_endpoint_residue(...)`, and
  `_normalize_glycam_topology(...)` at
  `mdclaw/amber_server.py:718`, `mdclaw/amber_server.py:869`, and
  `mdclaw/amber_server.py:985`.
- The openmmforcefields/Pablo helper starts at
  `mdclaw/amber_server.py:4038`.

## Research Escape Hatch: `build_openmm_system`

```mermaid
flowchart TD
  A["prepared PDB"] --> B["user-supplied OpenMM ForceField XML list"]
  B --> C["validate implicit solvent XML contract if requested"]
  C --> D["Pablo load with optional additional_smiles"]
  D --> E["ForceField.createSystem"]
  E --> F["short minimization unless disabled"]
  F --> G["same system.xml + topology.pdb + state.xml + minimization_report.json"]
```

`build_openmm_system(...)` starts at `mdclaw/openmm_system_server.py:175`.
It is intentionally less opinionated than `build_amber_system`: the user
brings XMLs that are already trusted, while MDClaw still emits the same atomic
artifact triple for downstream run tools.

## Current Design Boundaries

- `source` owns raw source normalization and optional Gemmi biological assembly
  generation. `prep` selects one candidate before making a physical MD system.
- `prepare_complex` owns component selection, component-common disposition
  (including deuterium exclusion for all split components and optional
  implicit-solvent ion exclusion), cleaning, protonation, standard DNA/RNA H
  rebuild, terminal caps, ligand chemistry artifacts, disulfide provenance,
  and glycan provenance.
- `solvate_structure` and `embed_in_membrane` own explicit environment
  construction and `box_dimensions`. Their output is the only explicit-solvent
  input expected by topology.
- `build_amber_system` owns force-field XML selection, topology-time ligand
  template selection, GLYCAM topology normalization, force-field validation,
  extra particles, system creation, and short minimization evidence.
- Generic repair at topology time is intentionally absent. If atom/H
  completeness is wrong, topology should fail with a structured code rather
  than silently repairing the prepared PDB.
- The run-side contract is only the atomic OpenMM artifact triple:
  `system.xml`, `topology.pdb`, and `state.xml`, plus minimization metadata
  where relevant. Equilibration and production consume that triple and do not
  reconstruct systems from ForceField XML.

## Main Artifacts By Stage

| Stage | Main artifacts |
|---|---|
| source | `source_bundle.json`, `artifacts/candidates/candidate_*` |
| prepare_complex | `merged.pdb`, `prepare_complex_summary.json`, `chain_identity_map.json`, `residue_mapping.json`, `disulfide_bonds.json`, `component_disposition.json`, `excluded_components.json`, optional `ligand_chemistry.json`, optional `glycan_metadata.json`, optional `glycan_linkages.json` |
| mutation branch | `mutated.pdb` registered as downstream `merged_pdb` |
| phosphorylation branch | `phosphorylated.pdb` registered as downstream `merged_pdb` |
| explicit solvation | `solvated.pdb`, `box_dimensions.json`, `solvation_metadata.json` |
| membrane embedding | `membrane.pdb`, `box_dimensions.json`, `membrane_metadata.json` |
| topology | `system.system.xml`, `system.topology.pdb`, `system.state.xml`, `system.minimization_report.json`, `amber_metadata.json`, optional `system.glycam_bond_plan.json`, optional `system.glycam_normalization.json` |
