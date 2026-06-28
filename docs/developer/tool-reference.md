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
- `detect_ptm_sites(...)`: internal helper (not a registered CLI/MCP tool)
  that scans a PDB/CIF for SEP/TPO/PTR sites. Used by `prepare_complex`; not
  in any server `TOOLS` dict, so it is not callable as `mdclaw detect_ptm_sites`.
- `search_structures(...)`, `search_proteins(...)`, `get_protein_info(...)`,
  `analyze_structure_details(...)`: external database helpers.

## `structure_server.py`

- `prepare_complex(...)`: full structure preparation pipeline. In node mode it
  resolves the source bundle from the `source` ancestor, selects one normalized
  candidate via `source_structure_id` / `source_candidate_id` /
  `source_model_index` when needed, and records `source_selection.json`.
  Standard DNA/RNA chains are hydrogen-rebuilt with OpenMM Modeller using the
  current DNA.OL15/RNA.OL3 XML libraries before topology. DNA.OL24 is deferred
  until openmmforcefields ships a released `DNA.OL24.xml`. Terminal caps can be
  requested independently with `n_terminal_cap="ACE"` and/or
  `c_terminal_cap="NME"`; the legacy `cap_termini=True` shortcut means both.
  ACE/NME cap hydrogens are completed in prep with OpenMM Modeller using the
  requested `terminal_cap_forcefield` or the ff19SB default. `solvent_type`
  declares prep-stage solvent intent and defaults to `"explicit"`; pass
  `"implicit"` to exclude explicit ion components from `merged_pdb` and record
  them in `component_disposition.json`. The same component disposition layer
  excludes experimental deuterium across all split components before
  component-specific preparation. Chain-associated ligands discovered by
  `inspect_molecules.associated_ligand_candidates` require explicit
  `include_ligand_ids`, residue-name scoped `include_ligand_resnames`, or
  deliberate `include_associated_ligands=True`; otherwise prep fails with
  `code="associated_ligands_require_selection"` instead of silently dropping
  ligand components.
- `clean_protein(...)`: PDBFixer plus pdb2pqr protonation, with fallback
  paths and optional site-specific residue protonation overrides rebuilt via
  OpenMM `Modeller.addHydrogens(variants=...)`. If ACE/NME caps are present,
  cap-specific H completion runs here; topology builders do not repair them.
  Heavy internal missing-residue gaps stop with
  `code="pdbfixer_missing_residues_out_of_scope"` and recommend regenerating
  the source through MODELLER or Boltz-2.
- `clean_ligand(...)`: ligand chemistry cleaning; emits charged-graph SDF/PDB
  artifacts for topology-time ligand force-field resolution.
- `split_molecules(...)`: extract protein, nucleic, glycan, ligand, ion, and
  water components. Same-author ligand candidates are surfaced in inspection
  output. Targeted ligands can be included by exact `include_ligand_ids` or by
  `include_ligand_resnames`, which selects matching associated ligand chains
  even when the ligand label chain differs from the selected polymer chain.
  `include_associated_ligands=True` remains available only for deliberately
  including all same-author ligand candidates; otherwise selection blocks with
  `code="associated_ligands_require_selection"` when `ligand` is in
  `include_types`.
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
  all predicted structures are registered in the source bundle. Protein-only
  predictions omit `smiles_list`; ligands are optional and required only when
  `affinity=True`. Per-candidate metadata records Boltz rank/model index,
  original output file, confidence JSON path, and `confidence_score` when
  Boltz writes confidence output. Failure returns carry stable `code` values
  such as `boltz_sequence_required`, `boltz_affinity_requires_ligand`,
  `boltz_msa_file_missing`, `boltz_custom_msa_multimer_unsupported`,
  `boltz_executable_not_found`, `boltz_execution_failed`, and
  `boltz_no_structure_output`.
- `modeller_from_alignment(...)`: MODELLER comparative modeling from a template
  PDB plus one of: a single `target_sequence`, per-chain `target_sequences`
  (multi-chain complexes such as heterodimers), or a full PIR/ALI
  `alignment_file`. With `target_sequences` (≥2) the tool builds the complex
  alignment automatically via MODELLER `align2d` against the template structure
  (chains joined with `/`); `template_chains` selects/orders the template chains
  that map to the target chains. Set `loop_refinement=True` to fill and refine
  missing residues with MODELLER loop modeling (`LoopModel`): the base model
  builds the full target sequence (including residues absent from the template),
  then every gap loop is rebuilt by the loop protocol. `loop_models` sets the
  number of refined loop models per base model; `loop_min_length` /
  `loop_max_length` bound which gap loops are refined. To model the missing
  residues of a structure, pass that structure as the template and its full
  sequence (e.g. from SEQRES) as the target. In node mode, the selected model is
  registered as the source bundle candidate with MODELLER metadata and ranking
  details. Guardrail `code`s: `modeller_target_sequence_conflict`,
  `modeller_target_sequence_required`, `modeller_chain_count_mismatch`,
  `modeller_loop_models_invalid`, `modeller_license_env_missing`,
  `modeller_not_installed`, `modeller_execution_failed`.
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
  resolves from the nearest prep ancestor. It first tries the requested salt
  concentration and records a warning if it must rerun packmol-memgen with
  `--salt_override` to satisfy neutralization.
- `embed_in_membrane(...)`: membrane embedding and solvation.
  Runs the bounded adaptive Packmol membrane plan as a 4-lane parallel race by
  default; set `packmol_race_lanes=1` / `--packmol-race-lanes 1` for the
  previous sequential retry behavior on CPU-constrained hosts.
- `list_available_lipids(...)`: lipid inventory.

## `amber_server.py`

- `build_amber_system(...)`: openmmforcefields-based topology builder
  (`SystemGenerator` and `GAFFTemplateGenerator`,
  with OpenFF Pablo for the PDB → topology stage). Handles ligand, metal,
  modXNA, glycan, nucleic acid,
  water-model, and PTM guardrails via
  `forcefield_catalog`. In node mode it resolves the PDB from `solv` or
  prep ancestors and stamps `system_xml` + `topology_pdb` + `state_xml`
  artifacts plus a `forcefield_provenance` dict on the `topo` node. Standard
  prep emits `ligand_chemistry`; ligand formal charge comes from the
  charged SMILES/SDF molecule graph, topology assigns small-molecule partial
  charges with OpenFF NAGL first, and falls back to
  `GAFFTemplateGenerator` AM1-BCC when NAGL is unavailable or fails. For
  glycoproteins,
  `cpptraj prepareforleap` is scoped to Amber/GLYCAM residue conversion and
  bond-plan generation; `build_amber_system` records
  `system.glycam_bond_plan.json` and `system.glycam_normalization.json` while
  applying GLYCAM bonds and glycan-only hydrogen completion inside the topo
  node.
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
- `export_state_pdb(...)`: export a PDB by combining atom/residue records from
  `topology.pdb` with positions from `state.xml`. Useful for report artifacts
  and MDPrepBench `minimized_structure.pdb` submissions.
- `run_minimization(...)`: standalone post-topology minimization. In node mode
  topology inputs resolve from the `topo` ancestor, and the `min` node records
  `state`, `minimized_structure`, and `minimization_report` artifacts for
  downstream `eq` nodes.
- `run_equilibration(...)`: restrained equilibration with an NVT heating stage
  and optional NPT density stage. In node mode topology inputs resolve from the
  `topo` ancestor. New DAGs should parent `eq` from `min`; the minimized state
  is then auto-resolved and coordinate minimization is skipped while low-
  temperature warmup remains in eq. Eq-chain restarts resolve from eq/prod
  ancestors.
  Agents should prefer `nvt_time_ns` / `npt_time_ns` (CLI:
  `--nvt-time-ns` / `--npt-time-ns`) for user-facing duration requests;
  explicit `nvt_steps` / `npt_steps` remain available for low-level
  reproducibility.
- `run_production(...)`: production MD with HMR, state/checkpoint persistence,
  DAG restart resolution, and timeline metadata. Accepts an optional custom
  force / CV bias via `custom_force_script` (an autograd-backed
  `energy(positions, ctx)` wrapped in `PythonTorchForce`) or
  `custom_force_module` (a TorchScript `.pt` wrapped in `TorchForce`), plus
  `custom_force_parameters` (JSON dict → `ctx.params`). The bias is added to
  the System in a dedicated force group before the Simulation is built, and
  bias energy + optional CV values are logged to
  `collective_variables.csv` (+ `.meta.json`). See
  `mdclaw/simulation/custom_forces.py`. (The legacy `restraint_file` argument
  is deprecated and ignored.)

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
  When the run command requests a GPU OpenMM platform (`--platform CUDA`/
  `OpenCL`) but no `--gpus`/`--gres` is given, it auto-sets `--gpus 1` (warning
  emitted) so a CUDA run is never scheduled on a CPU-only node.
- `submit_array_job(...)`: submit one SLURM array where each task maps to a DAG
  node command. Shares the same `--platform`-driven GPU autodetection as
  `submit_job`; a single GPU-platform task command flips the whole array to
  `--gpus 1`.
- `check_job(...)`: sync SLURM state and reflect failures into linked nodes.
- `list_jobs(...)`, `cancel_job(...)`, `check_job_log(...)`: operational
  helpers.
- `set_policy(...)`, `show_policy(...)`: resource policy management.
- `list_tracked_jobs(...)`: read `.mdclaw_jobs.jsonl` history and optionally
  sync state.
- `configure_container(...)`: configure Singularity wrapping for SLURM jobs.

## `node_server.py`

- `create_node(...)`: create a DAG node. `continue_from=<prod_id>` is restricted
  to production continuation and records explicit extension intent. Analyze nodes
  require `conditions.analysis_data_scope`; comparison analyses also require
  explicit subjects and mapping. When `parent_node_ids` is omitted, the
  canonical forward parent auto-resolves from the current completed frontier
  (the single completed leaf of the preferred parent type) and is reported as
  `auto_resolved_parent`; ambiguous or empty frontiers stay parent-less so
  branching/sketching is unaffected. Failure returns carry a stable `code`
  (e.g. `invalid_node_type`, `source_already_exists`, `analyze_parents_mixed`,
  `referenced_node_missing`).
- `inspect_job(...)`: read-only summary of node statuses, leaves, unfinished-node
  claims/open needs, warnings, and the progress index for weak-agent re-entry.
- `wait_node(...)`: read-only polling helper for long-running nodes. It waits
  for a node to reach `completed` or `failed` and reports timeout with a
  structured `node_wait_timeout` code instead of encouraging duplicate branches.
- `explain_node(...)`: read-only node details plus execution-context validation
  and auto-resolved inputs for a candidate node.
- `trace_failure(...)` / `explain_failure(...)`: read-only failed-node
  diagnosis. Reads `metadata.errors`, the latest failure artifact, recent
  events, and parent/dependency status, then returns `recovery_options` and
  `next_commands` for explicit branch creation.
- `update_job_params(...)`: merge workflow-level metadata into `progress.json`.
- `update_node_status(...)`: synchronized node and progress status update path.

## `study_server.py`

- `init_study(...)`: create a study directory used by both direct runs and
  campaigns.
- `bootstrap_md_workflow(...)`: create or reuse the canonical
  `study_dir/study.json` + `study_plan.json` + `jobs/<job_id>/progress.json`
  layout for any MD workflow, including simple one-system direct runs.
- `add_study_job(...)`: register existing or planned jobs.
- `list_study_jobs(...)`, `summarize_study(...)`: inspect study state.
- `record_study_plan(...)`, `get_study_plan(...)`, `list_study_plans(...)`:
  persist and inspect a lightweight scientific-question-to-MD-plan record. The
  plan is study-level intent only; job DAGs remain the execution source of truth.
- `record_study_decision(...)`, `record_study_question(...)`,
  `record_token_usage(...)`: append study-level JSONL logs.

## `evidence_server.py`

- `generate_md_evidence_report(...)`: JSON evidence summary for one job.

## `benchmark`

- `list_benchmark_tasks(...)`: list MDPrepBench or MDStudyBench tasks from a
  selected dataset directory, including family and intent summary.
- `prepare_benchmark_run(...)`: create a run directory, export an agent-safe
  public task package, write per-task prompt/submission instructions for the
  evaluated agent, and write separate harness scoring metadata.
- `run_benchmark_agent(...)`: SWE-bench-style external-agent runner. It
  creates public/private packages, runs a templated Pi / Claude Code / Codex
  command per selected task, records measured `harness_execution.json`, then
  scores and summarizes with the private evaluator package. It also records
  harness-owned `solver_context` for skill-free / skill-system / skill-text
  comparisons. `agent_skills_dir` installs an explicit skill root into
  `skills/`, `.agents/skills/`, `.claude/skills/`, `.codex/skills/`, and Pi's
  `package.json` inside the solver workspace. Built-in `agent_profile` values
  provide practical Pi, Claude Code, and Codex command templates, including
  non-interactive approval-bypass flags for Claude Code / Codex, explicit
  default model selection via `agent_model`, and process-group cleanup on
  timeout.
- `score_benchmark_run(...)`: validate and score every `submission/` under a
  run directory, then summarize the run.
- `init_benchmark_run(...)` / `summarize_benchmark_run(...)`: lower-level run
  record helpers used by harnesses.
- `export_benchmark_public_package(...)`: export prompt/contract-only task
  files for external agents; omits canonical `task.json`, `truth/`, and
  scorer-only material.
- `export_benchmark_private_package(...)`: export evaluator-only task
  contracts, held-out `truth/`, scorer-only references, and schemas for a
  separate scorer repository or container mount; omits agent prompts and public
  checklists. See `docs/benchmark/evaluation-workflow.md` for the intended
  public/private package evaluation flow.
- `validate_benchmark_task(...)`, `validate_benchmark_submission(...)`,
  `validate_and_score_benchmark_submission(...)`,
  `score_benchmark_submission(...)`, `write_benchmark_schemas(...)`: evaluator
  and maintenance helpers for task/submission lifecycle.
  Registered `visual_review_json` artifacts are included as evidence artifacts;
  they are best-effort visual accident checks, not scientific validation.
- `generate_md_methods_report(...)`: Methods Markdown for one job lineage.
- `generate_study_methods_report(...)`: Methods report across registered jobs.
- `generate_study_evidence_report(...)`: JSON evidence summary across a study.
  When `study_plan.json` exists, its question, MD goal, analysis list, and
  decision criteria are included in the study-level evidence report.

## `benchmark/`

- `list_benchmark_tasks(...)`: list benchmark tasks, families, scoring axes,
  modes, and short intent summaries.
- `validate_benchmark_task(...)`, `validate_benchmark_submission(...)`: validate
  task contracts and submitted artifacts.
- `score_benchmark_submission(...)`: score one task submission.
- `write_benchmark_schemas(...)`: regenerate task / manifest / score JSON
  schemas from pydantic models.

Run setup and aggregation helpers live in `mdclaw.benchmark.run` for
harness/admin code, but are not exposed as `mdclaw` CLI tools.
