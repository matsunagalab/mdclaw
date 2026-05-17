# Tool Reference

This file is the developer-maintained index of MDClaw tool modules. When adding
or changing a tool signature, update the relevant section here and the matching
skill examples.

## `research_server.py`

- `fetch_structure(...)`: preferred structure acquisition entry point for PDB,
  AlphaFold, and local files. In node mode it records `source_bundle.json`.
  For PDB/local PDB or mmCIF sources, explicit `assembly_ids` or
  `assembly_mode` requests generate Gemmi biological assembly candidates.
- `download_structure(...)`: RCSB PDB compatibility wrapper.
- `get_structure_info(...)`: PDB entry metadata.
- `get_alphafold_structure(...)`: AlphaFold DB compatibility wrapper.
- `register_local_structure(...)`: copy or symlink a local source structure.
- `list_source_candidates(...)`: list normalized source-bundle candidates,
  including IDs, ranks, files, origin metadata, and candidate metrics.
- `inspect_molecules(...)`: chain, nucleic acid, glycan, ligand, ion, and PTM
  inspection. In node mode, defaults to the primary source candidate and accepts
  the same source candidate selectors as prep. Writes `inspection.json` and
  emits an event without changing node status.
- `detect_ptm_sites(...)`: scan PDB/CIF for SEP/TPO/PTR sites.
- `search_structures(...)`, `search_proteins(...)`, `get_protein_info(...)`,
  `analyze_structure_details(...)`: external database helpers.

## `structure_server.py`

- `prepare_complex(...)`: full structure preparation pipeline. In node mode it
  resolves the source bundle from the `source` ancestor, selects one normalized
  candidate via `source_structure_id` / `source_candidate_id` /
  `source_model_index` when needed, and records `source_selection.json`.
- `clean_protein(...)`: PDBFixer plus pdb2pqr protonation, with fallback
  paths and optional site-specific residue protonation overrides rebuilt via
  OpenMM `Modeller.addHydrogens(variants=...)`.
- `clean_ligand(...)`: ligand chemistry cleaning; emits SDF/PDB chemistry
  artifacts for topology-time ligand force-field resolution.
- `split_molecules(...)`: extract protein, nucleic, glycan, ligand, ion, and
  water components.
- `merge_structures(...)`: merge prepared PDB fragments and emit
  `chain_identity_map` / `*.chain_identity_map.json`; PDB chain IDs are short
  compatibility labels and may repeat in large assemblies.
- `create_mutated_structure(...)`: HPacker side-chain mutation and nearby
  repacking on a branched prep node.
- `prepare_modified_nucleic(...)`: legacy/experimental modXNA file generation.
  The standard MD-ready topology path does not support modified DNA/RNA
  residues; `inspect_molecules` reports them as unsupported and
  `build_amber_system` stops with a structured code.
- `phosphorylate_residues(...)`: restore or apply SEP/TPO/PTR sites for Amber
  phosaa topology generation.

## `genesis_server.py`

- `boltz2_protein_from_seq(...)`: Boltz-2 structure prediction. In node mode,
  all predicted structures are registered in the source bundle; per-candidate
  metadata records Boltz rank/model index, original output file, confidence
  JSON path, and `confidence_score` when Boltz writes confidence output.
- `rdkit_validate_smiles(...)`: SMILES validation and canonicalization.
- `pubchem_get_smiles_from_name(...)`: PubChem name lookup.
- `analyze_plip_interactions(...)`: protein-ligand interaction analysis.

## `surrogate_server.py`

- `setup_surrogate_backend(...)`: create or update an isolated venv for a
  surrogate backend. BioEmu is installed here, never in the conda `mdclaw`
  environment.
- `check_surrogate_backend(...)`: import/version check for a surrogate backend
  venv without running sampling.
- `generate_surrogate_candidates(...)`: generate monomer source candidates with
  a surrogate backend such as BioEmu. In node mode it writes
  `source_bundle.json` with `source_type="surrogate"` and
  `origin.kind="bioemu"` for BioEmu candidates.

## `solvation_server.py`

- `solvate_structure(...)`: explicit water box generation. In node mode the PDB
  resolves from the nearest prep ancestor.
- `embed_in_membrane(...)`: membrane embedding and solvation.
- `list_available_lipids(...)`: lipid inventory.

## `amber_server.py`

- `build_amber_system(...)`: openmmforcefields-based topology builder
  (`SystemGenerator`, topology-time geostd XMLs, and `GAFFTemplateGenerator`,
  with OpenFF Pablo for the PDB → topology stage). Handles ligand, metal,
  modXNA, glycan, nucleic acid,
  water-model, and PTM guardrails via
  `forcefield_catalog`. In node mode it resolves the PDB from `solv` or
  prep ancestors and stamps `system_xml` + `topology_pdb` + `state_xml`
  artifacts plus a `forcefield_provenance` dict on the `topo` node. Standard
  prep emits `ligand_chemistry`; topology resolves compatible Amber geostd
  templates first and uses `GAFFTemplateGenerator` when geostd is missing or
  incompatible with the recorded ligand charge/atom count.
  Implicit solvent: `implicit_solvent="HCT" / "OBC1" / "OBC2" / "GBn" /
  "GBn2"` (case-insensitive; `gbneck2` / `igb1`–`igb8` aliases). The
  matching `implicit/*.xml` is added to the SystemGenerator bundle so
  the saved System carries a `CustomGBForce` / `GBSAOBCForce`, and the
  canonical model name is stamped on `metadata.implicit_solvent` for
  the run-side topology guard. Failure codes:
  `implicit_solvent_model_unsupported`, `implicit_solvent_explicit_box_conflict`,
  `implicit_solvent_force_missing`.
- `build_openmm_system(...)`: research-mode escape hatch — accepts
  arbitrary OpenMM ForceField XML files plus optional ligand SMILES and
  emits the same modern artifact triple. No FF×water guardrail matrix;
  users supply XML they already trust. Implicit solvent has two
  research tiers: (a) **shipped GB XML** — pass
  `forcefield_xml=[..., "implicit/<model>.xml"]` *plus*
  `implicit_solvent="<MODEL>"` so the canonical name lands on
  `metadata.implicit_solvent` and the run-side topology guard matches;
  missing or duplicate `implicit/*.xml` returns
  `implicit_solvent_xml_missing` / `implicit_solvent_xml_ambiguous`.
  (b) **External GB XML** (e.g. the Greener group's `GB99dms.xml`) —
  loadable as arbitrary OpenMM XML, but `forcefield_catalog` cannot
  canonicalize a non-catalog GB XML. `metadata.implicit_solvent` stays
  `None` and the run-side topology guard cannot validate the build vs
  runtime match; the user owns XML correctness, GB-force presence, and
  build/run consistency. Out-of-version checks (e.g. `GB99dms.xml`
  needs OpenMM ≥ 8.0) still fire via existing guards.

## `md_simulation_server.py`

- `inspect_openmm_platforms(...)`: lightweight OpenMM platform inventory and
  atom-count feasibility guidance before local explicit-water runs.
- `run_equilibration(...)`: staged minimization, warmup, NVT, and optional NPT.
  In node mode topology inputs resolve from the `topo` ancestor. Agents should
  prefer `nvt_time_ns` / `npt_time_ns` (CLI: `--nvt-time-ns` /
  `--npt-time-ns`) for user-facing duration requests; explicit
  `nvt_steps` / `npt_steps` remain available for low-level reproducibility.
- `run_production(...)`: production MD with HMR, state/checkpoint persistence,
  DAG restart resolution, and timeline metadata.

## `visualization_server.py`

- `render_structure_preview(...)`: PyMOL headless PNG rendering for PDB/mmCIF
  structure artifacts. In node mode it resolves a representative structure
  artifact from the current node, parent, or ancestors, writes a ray-rendered
  preview PNG plus PyMOL script and manifest under `artifacts/previews/`, and
  registers `structure_preview_png` / `structure_preview_manifest` on the node.
  The executed Python script is `structure_preview_pymol_script`; the companion
  `.pml` preview is registered separately as `structure_preview_pymol_pml`.
  Styles include `overview`, `publication`, `ligand_site`, `membrane`,
  `solvent_ions`, and `topology_check`; the manifest records camera/view and
  representation choices for human review.
- `register_visual_review(...)`: register a best-effort visual QA review of a
  preview PNG as `visual_review_json`. The tool does not perform image
  understanding; a multimodal LLM or human reviews the PNG first and records
  coarse accident-check findings (`severity`, `recommendation`, `findings`,
  `limitations`). This is not scientific validation and high-severity findings
  request user confirmation without marking the DAG node failed.

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
- `inspect_job(...)`: read-only summary of node statuses, leaves, claims, open
  needs, warnings, and the progress index for weak-agent re-entry.
- `explain_node(...)`: read-only node details plus execution-context validation
  and auto-resolved inputs for a candidate node.
- `update_job_params(...)`: merge workflow-level metadata into `progress.json`.
- `update_node_status(...)`: synchronized node and progress status update path.

## `study_server.py`

- `init_study(...)`: create an optional campaign directory.
- `add_study_job(...)`: register existing or planned jobs.
- `list_study_jobs(...)`, `summarize_study(...)`: inspect study state.
- `record_study_plan(...)`, `get_study_plan(...)`, `list_study_plans(...)`:
  persist and inspect a lightweight scientific-question-to-MD-plan record. The
  plan is study-level intent only; job DAGs remain the execution source of truth.
- `record_study_decision(...)`, `record_study_question(...)`,
  `record_token_usage(...)`: append study-level JSONL logs.

## `evidence_server.py`

- `generate_md_evidence_report(...)`: JSON evidence summary for one job.
  Registered `visual_review_json` artifacts are included as evidence artifacts;
  they are best-effort visual accident checks, not scientific validation.
- `generate_md_methods_report(...)`: Methods Markdown for one job lineage.
- `generate_study_methods_report(...)`: Methods report across registered jobs.
- `generate_study_evidence_report(...)`: JSON evidence summary across a study.
  When `study_plan.json` exists, its question, MD goal, analysis list, and
  decision criteria are included in the study-level evidence report.

## `benchmark/`

- `list_benchmark_tasks(...)`: list MDAgentBench tasks, families, scoring axes,
  modes, and short intent summaries.
- `validate_benchmark_task(...)`, `validate_benchmark_submission(...)`: validate
  task contracts and submitted artifacts.
- `score_benchmark_submission(...)`: score one task submission.
- `write_benchmark_schemas(...)`: regenerate task / manifest / score JSON
  schemas from pydantic models.

Run setup and aggregation helpers live in `mdclaw.benchmark.run` for
harness/admin code, but are not exposed as `mdclaw` CLI tools.
