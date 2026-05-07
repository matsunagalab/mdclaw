# Tool Reference

This file is the developer-maintained index of MDClaw tool modules. When adding
or changing a tool signature, update the relevant section here and the matching
skill examples.

## `research_server.py`

- `fetch_structure(...)`: preferred structure acquisition entry point for PDB,
  AlphaFold, and local files.
- `download_structure(...)`: RCSB PDB compatibility wrapper.
- `get_structure_info(...)`: PDB entry metadata.
- `get_alphafold_structure(...)`: AlphaFold DB compatibility wrapper.
- `register_local_structure(...)`: copy or symlink a local source structure.
- `inspect_molecules(...)`: chain, nucleic acid, glycan, ligand, ion, and PTM
  inspection. In node mode writes `inspection.json` and emits an event without
  changing node status.
- `detect_ptm_sites(...)`: scan PDB/CIF for SEP/TPO/PTR sites.
- `search_structures(...)`, `search_proteins(...)`, `get_protein_info(...)`,
  `analyze_structure_details(...)`: external database helpers.

## `structure_server.py`

- `prepare_complex(...)`: full structure preparation pipeline. In node mode the
  source structure resolves from the `source` ancestor.
- `clean_protein(...)`: PDBFixer plus pdb2pqr protonation, with fallback paths.
- `clean_ligand(...)`: ligand cleaning and parameterization.
- `split_molecules(...)`: extract protein, nucleic, glycan, ligand, ion, and
  water components.
- `merge_structures(...)`: merge prepared PDB fragments.
- `run_antechamber_robust(...)`: metal pre-check, amber_geostd, and GAFF2
  fallback ligand parameterization.
- `download_amber_geostd(...)`: fetch the curated ligand parameter database.
- `create_mutated_structure(...)`: FASPR side-chain mutation on a branched prep
  node.
- `prepare_modified_nucleic(...)`: modXNA parameter generation and residue
  renaming on a branched prep node.
- `phosphorylate_residues(...)`: restore or apply SEP/TPO/PTR sites for Amber
  phosaa topology generation.

## `genesis_server.py`

- `boltz2_protein_from_seq(...)`: Boltz-2 structure prediction.
- `rdkit_validate_smiles(...)`: SMILES validation and canonicalization.
- `pubchem_get_smiles_from_name(...)`: PubChem name lookup.
- `analyze_plip_interactions(...)`: protein-ligand interaction analysis.

## `solvation_server.py`

- `solvate_structure(...)`: explicit water box generation. In node mode the PDB
  resolves from the nearest prep ancestor.
- `embed_in_membrane(...)`: membrane embedding and solvation.
- `list_available_lipids(...)`: lipid inventory.

## `amber_server.py`

- `build_amber_system(...)`: openmmforcefields-based topology builder
  (`SystemGenerator` + `GAFFTemplateGenerator`, with OpenFF Pablo for the
  PDB → topology stage). Replaces the legacy tleap path. Handles ligand,
  metal, modXNA, glycan, nucleic acid, water-model, and PTM guardrails via
  `forcefield_catalog`. In node mode it resolves the PDB from `solv` or
  prep ancestors and stamps `system_xml` + `topology_pdb` + `state_xml`
  artifacts plus a `forcefield_provenance` dict on the `topo` node.
- `build_openmm_system(...)`: research-mode escape hatch — accepts
  arbitrary OpenMM ForceField XML files (e.g. GB99dms.xml) plus optional
  ligand SMILES and emits the same modern artifact triple. No FF×water
  guardrail matrix; users supply XML they already trust.

## `md_simulation_server.py`

- `run_equilibration(...)`: staged minimization, warmup, NVT, and optional NPT.
  In node mode topology inputs resolve from the `topo` ancestor.
- `run_production(...)`: production MD with HMR, state/checkpoint persistence,
  DAG restart resolution, and timeline metadata.

## `literature_server.py`

- `pubmed_search(...)`: PubMed search.
- `pubmed_fetch(...)`: article metadata fetch.

## `metal_server.py`

- `detect_metal_ions(...)`: scan structures for metal ions.
- `parameterize_metal_ion(...)`: Amber ion parameter selection with water-model
  and ion-set guardrails.

## `slurm_server.py`

- `inspect_cluster(...)`: discover partitions, GPUs, and local policy.
- `submit_job(...)`: submit one SLURM job and link it to an optional DAG node.
- `submit_array_job(...)`: submit one SLURM array where each task maps to a DAG
  node command.
- `check_job(...)`: sync SLURM state and reflect failures into linked nodes.
- `list_jobs(...)`, `cancel_job(...)`, `check_job_log(...)`: operational
  helpers.
- `set_policy(...)`, `show_policy(...)`: resource policy management.
- `list_tracked_jobs(...)`: read `.mdclaw_jobs.jsonl` history and optionally
  sync state.
- `configure_container(...)`: configure Singularity wrapping for SLURM jobs.

## `node_server.py`

- `create_node(...)`: create a DAG node. `continue_from=<prod_id>` is restricted
  to production continuation and records explicit extension intent.
- `update_job_params(...)`: merge workflow-level metadata into `progress.json`.
- `update_node_status(...)`: synchronized node and progress status update path.

## `study_server.py`

- `init_study(...)`: create an optional campaign directory.
- `add_study_job(...)`: register existing or planned jobs.
- `list_study_jobs(...)`, `summarize_study(...)`: inspect study state.
- `record_study_decision(...)`, `record_study_question(...)`,
  `record_token_usage(...)`: append study-level JSONL logs.

## `evidence_server.py`

- `generate_md_evidence_report(...)`: JSON evidence summary for one job.
- `generate_md_methods_report(...)`: Methods Markdown for one job lineage.
- `generate_study_methods_report(...)`: Methods report across registered jobs.
- `generate_study_evidence_report(...)`: JSON evidence summary across a study.
