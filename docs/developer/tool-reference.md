# Tool Reference

This file is the developer-maintained index of MDClaw tool packages. Each tool
is a `mdclaw/<name>/` package whose `__init__.py` assembles the public `TOOLS`
dict from responsibility-scoped submodules. When adding or changing a tool
signature, update the relevant section here and the matching skill examples.

## `research/`

- `fetch_structure(...)`: preferred structure acquisition entry point for PDB,
  AlphaFold, and local files. In node mode it records `source_bundle.json`.
  For PDB/local PDB or mmCIF sources, explicit `assembly_ids` or
  `assembly_mode` requests generate Gemmi biological assembly candidates.
- `get_structure_info(...)`: PDB entry metadata.
- `register_local_structure(...)`: copy or symlink a local source structure.
- `list_source_candidates(...)`: list normalized source-bundle candidates,
  including IDs, ranks, files, origin metadata, and candidate metrics.
- `inspect_molecules(...)`: chain, nucleic acid, glycan, ligand, ion, and PTM
  inspection. In node mode, defaults to the primary source candidate and accepts
  the same source candidate selectors as prep. Writes `inspection.json` and
  emits an event without changing node status.
- `detect_ptm_sites(...)`: internal helper (not a registered CLI tool)
  that scans a PDB/CIF for SEP/TPO/PTR sites. Used by `prepare_complex`; not
  in any server `TOOLS` dict, so it is not callable as `mdclaw detect_ptm_sites`.
- `search_structures(...)`, `search_proteins(...)`, `get_protein_info(...)`:
  external database helpers.

## `structure/`

- `prepare_complex(...)`: full structure preparation pipeline. In node mode it
  resolves the source bundle from the `source` ancestor, selects one normalized
  candidate via `source_structure_id` / `source_candidate_id` /
  `source_model_index` when needed, and records `source_selection.json`.
  Protein `residue_ranges` select deposited polymer positions, not every integer
  between author endpoints. Prep audits ordered residue identity against the
  source sequence scheme (or SEQRES alignment), recording source/prepared
  correspondence in `chain_identity_map.json`. Without sequence evidence,
  completeness is unknown. Explicit coordinate-file overrides retain the source
  sequence metadata and must preserve its polymer identity; use a new source
  node for deliberate construct changes, and range/chain options for cropping.
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

## `genesis/`

- `boltz2_protein_from_seq(...)`: Boltz-2 structure prediction. In node mode,
  all predicted structures are registered in the source bundle. Protein-only
  predictions omit `smiles_list`; ligands are optional and required only when
  `affinity=True`. Per-candidate metadata records Boltz rank/model index,
  original output file, confidence JSON path, and `confidence_score` when
  Boltz writes confidence output. Failure returns carry stable `code` values
  such as `boltz_sequence_required`, `boltz_affinity_requires_ligand`,
  `boltz_msa_file_missing`, `boltz_custom_msa_multimer_unsupported`,
  `boltz_backend_not_installed`, `boltz_execution_failed`, and
  `boltz_no_structure_output`. Boltz-2 runs from an isolated backend venv
  resolved through `mdclaw.surrogate.MODEL_BACKENDS["boltz"]`, installed
  with `setup_model_backend --model boltz`, not from the conda `mdclaw` env.
  (Boltz resolves its venv through `mdclaw.surrogate.MODEL_BACKENDS["boltz"]`.)
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
  sequence (e.g. from SEQRES) as the target, and set `template_frame=True` so
  the model is written superposed on the template and renumbered to its author
  numbering — MODELLER otherwise emits its own frame numbered from 1, which
  misplaces any ligand or partner chain carried over from the original. The
  in-place CA deviation is reported under `selected_model.template_frame` either
  way. In node mode, the selected model is
  registered as the source bundle candidate with MODELLER metadata and ranking
  details. Guardrail `code`s: `modeller_target_sequence_conflict`,
  `modeller_target_sequence_required`, `modeller_chain_count_mismatch`,
  `modeller_loop_models_invalid`, `modeller_license_env_missing`,
  `modeller_not_installed`, `modeller_execution_failed`.
- `rdkit_validate_smiles(...)`: SMILES validation and canonicalization.
- `pubchem_get_smiles_from_name(...)`: PubChem name lookup.

## `surrogate/`

- `setup_model_backend(...)`: create or update an isolated venv for a heavy
  model backend. Supported models: `bioemu` (MD surrogate ensembles) and
  `boltz` (structure prediction, pinned to `BOLTZ_VERSION`). Backends live under
  `$MDCLAW_SURROGATE_DIR/<model>/venv` and never touch the conda `mdclaw`
  environment. `MODEL_BACKENDS` is the registry; `boltz2_protein_from_seq`
  resolves the boltz venv through it.
- `check_model_backend(...)`: import/version check for a model backend venv
  without running the model.
- Backends declare capabilities (`supports_sampling`, `supports_prediction`);
  callers dispatch via `models_with_capability(...)` /
  `resolve_prediction_backend(...)`, so models are swappable without touching
  callers. See `docs/developer/model-backends.md` to add or swap a backend.
- `generate_surrogate_candidates(...)`: generate monomer source candidates with
  a sampling backend (currently BioEmu only). In node mode it writes
  `source_bundle.json` with `source_type="surrogate"` and
  `origin.kind="bioemu"` for BioEmu candidates.

## `solvation/`

- `solvate_structure(...)`: explicit water box generation. In node mode the PDB
  resolves from the nearest prep ancestor. It first tries the requested salt
  concentration and records a warning if it must rerun packmol-memgen with
  `--salt_override` to satisfy neutralization. Results expose the prepared
  `solute_net_charge_e` and output-PDB `ion_counts` at top level.
- `embed_in_membrane(...)`: membrane embedding and solvation.
  Defaults to `membrane_backend="patch-tile"`: build a small composition-keyed
  lipid patch once, equilibrate it under PBC (`build_amber_system` +
  `run_minimization` + `run_equilibration`, called in non-node mode), cache it
  under a protein-size-independent fingerprint (composition + build defaults;
  the packmol-memgen version is excluded so patches are reusable across conda
  and container environments), then orient the protein with MEMEMBED, restore
  non-water HETATM solutes that MEMEMBED drops (e.g. pore ions/cofactors), align
  the cached patch to MEMEMBED's dummy-membrane midplane, tile the patch to
  cover it, carve overlaps with periodic-boundary awareness, and neutralize by
  swapping bulk waters for ions. Beta-barrel proteins can request MEMEMBED
  `-b` via `memembed_beta_barrel`, which applies only when MEMEMBED is the
  orientation backend. `memembed_force_span` passes MEMEMBED `-l` on
  the patch-tile path. `n_terminal_side` (`in`/`out`) passes MEMEMBED `-n`, which
  fixes which leaflet the first residue faces; without it MEMEMBED infers the
  topology from its knowledge-based potential, and a large soluble domain can
  invert the whole protein. `memembed_search_type` maps to MEMEMBED `-s` and
  defaults to 3 (genetic algorithm repeated five times), matching what
  packmol-memgen itself uses; MEMEMBED's own default is a single GA run.
  `orientation_method` selects the orientation backend: `auto` (the default),
  `opm-homolog`, `ppm`, or `memembed`. Orientation runs once before any packing
  backend is chosen, and both packing paths then receive an already-oriented
  structure, so switching packing backend cannot move the protein.
  `auto` first tries to transfer a frame from an OPM homolog
  (`mdclaw/solvation/opm_orient.py`): it asks RCSB for entities that both match
  a query sequence and carry an OPM annotation, aligns with gemmi, superposes
  corresponding CA atoms with a Kabsch fit, and applies the transform to the
  whole input including ligands. **Every protein chain is searched**, longest
  first, stopping at the first donor that clears the gates — a complex is often
  a large soluble partner plus a small membrane subunit, and only the subunit
  has a homolog. Chains with identical sequences are searched once. One chain
  failing to reach the service does not end the attempt; only an outage on every
  chain yields `opm_homolog_search_unavailable`. The superposition uses only
  residues the donor places inside its own bilayer (its DUM markers, widened by
  a small fixed 2 A margin), so a shared extramembrane domain cannot decide the
  frame.
  Candidates must clear identity **over the membrane subset as well as the whole
  chain**, query coverage, membrane-CA-count, fit-conditioning and fit-RMSD
  gates (`opm_min_*`/`opm_max_*`), all computed from the alignment made here —
  RCSB's own match context is recorded as provenance only (normalised to the
  same 0-1 scale; it reports identity out of 100, never reports coverage, and
  omits the context entirely on some hits). Membrane-subset identity is gated
  separately because the frame is fitted to that subset: two proteins sharing a
  large soluble domain can clear a whole-chain gate while their membrane
  domains are unrelated. `opm_min_fit_condition` rejects a fitted CA cloud
  whose *second* principal spread is a negligible fraction of its largest —
  collinear points superpose at RMSD 0 with a proper rotation matrix while the
  spin about their axis is arbitrary, and that spin would be applied to every
  soluble domain and partner in the input. It is the second and not the third
  because rank 2 already determines a rotation: the proper-rotation constraint
  fixes the plane normal, so a coplanar cloud is a valid fit. A search matching
  nothing answers 204 with an empty body, read as no homolog for that chain; an
  empty body with any other status is a truncated response and is reported as
  unavailable. Among a donor's chains only those that clear **every** gate
  compete, and the lowest-RMSD survivor wins; ranking on RMSD first would let a
  chain matching a short unrelated stretch displace the real counterpart.
  **Every** searchable chain and **every** candidate it returns are judged, and
  one ranking over all acceptable (chain, donor) pairs picks the winner —
  because RCSB orders by search relevance, not orientation quality, and chain
  order is not evidence either. The key is the Wilson lower bound of
  membrane-subset identity given its residue count, to 2 dp, then fit RMSD: 40
  matches out of 40 is a weaker claim than 198 out of 200, and rounding lets a
  one-residue difference fall through to the fit instead of deciding it. The
  full ranking is recorded. Each query
  chain's search result, error and candidates — including every donor chain's
  numbers and rejection reason — are written separately to
  `opm_homolog_search.json`, and one OPM entry is downloaded and parsed once per
  build, renamed into place so a kill mid-write cannot leave a partial file for
  later runs to trust, with its SHA-256 recorded. Candidates that could not be
  downloaded are counted apart from those that failed a gate, so an outage at
  OPM's asset host reports `opm_homolog_fetch_unavailable` instead of claiming
  the donors were examined and found wanting. Only the input's first model is used, with one altLoc conformer chosen per
  *residue* by summed occupancy — choosing atom by atom can assemble a side
  chain that exists in no structure — for both the fit and the transformed
  output. `TER` records are carried through, because dropping them fuses two
  polymer segments into one chain. `opm_total_budget_seconds` bounds the whole backend
  rather than each request, so a many-chain complex on an unreachable network
  drops to PPM3 promptly instead of multiplying per-request timeouts. An
  out-of-range, non-finite, or fractional-count gate is a caller error
  (`opm_homolog_gates_invalid`) and fails rather than falling back: silently
  loosening a gate is worse than refusing the request. When no donor is
  accepted but candidates or chains went unjudged, the code is
  `opm_homolog_evaluation_incomplete` rather than `rejected` or `no_match` —
  those two are verdicts, and an agent branching on them will not retry an
  outage. If the budget truncates the field *after* an acceptable donor was
  found, that donor is still used (a gated frame beats switching method) but
  `evaluation_complete` is false and a warning says what went unjudged.
  Being offline, finding no hit, or rejecting every candidate is not a
  failure — it is recorded as an explicit fallback and orientation continues
  with `ppm` (PPM3 `immers`, rebuilt from patched source by the container).
  `result["orientation"]` records the backend that actually ran, every attempt,
  and why each earlier one was not used.
  `n_terminal_side` is applied only when the caller states it; PPM3 needs a
  value regardless, so an unstated side is run under PPM's own convention and
  flagged as assumed rather than presented as a decision. `dist_wat` is water beyond the
  membrane **or the solute**, whichever reaches further — the meaning
  packmol-memgen gives it. The cell the patch-tile backend builds is therefore
  `[min(solute_z_min, -leaflet) - dist_wat, max(solute_z_max, +leaflet) + dist_wat]`,
  asymmetric because proteins are: mirroring a large extracellular domain's
  water below the bilayer would carry tens of thousands of molecules that do
  nothing (143 A rather than 191 A on 5L7D).
  The **bilayer patch is not resized**. Its height is part of the cache
  fingerprint and most membrane proteins reach past the leaflet, so sizing the
  patch from the solute would miss the cache and pay for a fresh pack and
  equilibration nearly every time. The patch is always requested at the caller's
  `dist_wat`; the extra volume is filled afterwards by stacking copies of the
  patch's own water slabs — already equilibrated, already at the right density,
  already carrying its ions — the way a solvation program replicates a water
  box. Copies meet at bulk-water faces that were not periodic partners, so a
  whole molecule landing on one already placed is dropped and minimisation
  closes the gap. `result["solute_box_interval"]` and
  `result["water_extension"]` record the interval, how far each side grew, and
  how many molecules were added and dropped.
  Containment is tested as the solute's z **span** against the cell length, not
  as atom positions against faces: under PBC the origin is a choice, and a
  molecule reaching past a face simply re-enters at the other one. What cannot
  be translated away is a molecule longer than the period. Judging by faces
  placed at the membrane centre would assume a cell centred on the bilayer —
  false of both this interval and packmol-memgen's — and would reject a 143 A
  box holding a 108 A solute. The assembly refuses a solute longer than its cell
  (`membrane_patch_solute_exceeds_box_z`). A PBC-aware post-build geometry check
  writes `membrane_embedding_geometry.json` and fails with
  `membrane_embedding_geometry_failed` if the protein does not intersect the
  bilayer headgroup span (`protein_does_not_intersect_bilayer_headgroup_span`)
  **or is longer than the periodic cell in z**
  (`protein_exceeds_periodic_box_z`) — a receptor can sit correctly in the
  bilayer and still overlap its own image, so the two are checked separately. The cold build runs once per composition and is
  surfaced via `warnings`, `patch_cold_build_notice`, and `patch_build`.
  Patch cold-build topology generation disables Pablo CCD auto-download
  (`pablo_auto_download=False`) because the patch contains known local
  Lipid21/water/ion chemistry and should not block on network fetches. The
  cached patch PDB is exported from the equilibrated `state.xml`; the state
  periodic box is authoritative, and the final cache validation rejects
  CRYST1/manifest/box mismatches plus PBC close-contact overlaps.
  `membrane_backend="packmol-memgen"` runs the legacy full-box packing path
  (bounded adaptive Packmol as a 4-lane parallel race; set
  `packmol_race_lanes=1` for sequential retries). On that path
  `memembed_beta_barrel` maps to packmol-memgen `--barrel`; `memembed_force_span`
  is recorded as a warning because packmol-memgen does not expose MEMEMBED `-l`
  directly. `membrane_backend="auto"` tries patch-tile then falls back to
  packmol-memgen. Patch caching honors `membrane_cache_mode` (`off` /
  `read-only` / `auto` / `refresh`),
  `membrane_cache_dir`, and the read-only bundled cache root
  `MDCLAW_MEMBRANE_BUNDLED_CACHE_DIR`. See `scripts/warmup_membrane_cache.py`.
- `list_available_lipids(...)`: lipid inventory.

## `amber/`

- `build_amber_system(...)`: openmmforcefields-based topology builder
  (`SystemGenerator` and `GAFFTemplateGenerator`,
  with OpenFF Pablo for the PDB → topology stage). Handles ligand, metal,
  modXNA, glycan, nucleic acid,
  water-model, and PTM guardrails via
  `forcefield_catalog`. In node mode it resolves the PDB from `solv` or
  prep ancestors and stamps `system_xml` + `topology_pdb` + `state_xml`
  artifacts plus a `forcefield_provenance` dict on the `topo` node. The
  topology build performs a short initial relaxation (10 iterations by
  default) and marks it `scope="topology_initial_relaxation"` with
  `satisfies_min_node_contract=false`; the separate `min` node owns the
  post-topology minimization contract. Standard prep emits
  `ligand_chemistry`; ligand formal charge comes from the
  charged SMILES/SDF molecule graph, topology assigns small-molecule partial
  charges with OpenFF NAGL first, and falls back to
  `GAFFTemplateGenerator` AM1-BCC when NAGL is unavailable or fails. For
  glycoproteins,
  `cpptraj prepareforleap` is scoped to Amber/GLYCAM residue conversion and
  bond-plan generation; `build_amber_system` records
  `system.glycam_bond_plan.json` and `system.glycam_normalization.json` while
  applying GLYCAM bonds and glycan-only hydrogen completion inside the topo
  node.
  `pablo_auto_download` defaults to `True` for general prepared structures so
  Pablo can fetch missing CCD definitions; set it to `False` only for known
  local/offline topology loads where PDBFile fallback plus template-bond
  patching is preferred to a network wait.
  Implicit solvent: `implicit_solvent="HCT" / "OBC1" / "OBC2" / "GBn" /
  "GBn2"` (case-insensitive; `gbneck2` / `igb1`–`igb8` aliases). The
  matching `implicit/*.xml` is added to the SystemGenerator bundle so
  the saved System carries a `CustomGBForce` / `GBSAOBCForce`, and the
  canonical model name is stamped on `metadata.implicit_solvent` for
  the run-side topology guard. Failure codes:
  `implicit_solvent_model_unsupported`, `implicit_solvent_explicit_box_conflict`,
  `implicit_solvent_force_missing`.
## `openmm_system/`

- `build_openmm_system(...)`: research-mode escape hatch — accepts
  arbitrary OpenMM ForceField XML files plus optional ligand SMILES and
  emits the same modern artifact triple. It also emits the same final
  `topology_validation` report used by `build_amber_system`; a failed core
  artifact check returns `topology_validation_failed`. Its short topology-time
  initial relaxation has the same `scope="topology_initial_relaxation"` and
  `satisfies_min_node_contract=false` markers as `build_amber_system`; it is
  not a replacement for a `min` node. No FF×water guardrail matrix;
  users supply XML they already trust. Implicit solvent has two
  research tiers: (a) **shipped GB XML** — pass
  `forcefield_xml=[..., "implicit/<model>.xml"]` *plus*
  `implicit_solvent="<MODEL>"` so the canonical name lands on
  `metadata.implicit_solvent` and the run-side topology guard matches;
  missing or duplicate `implicit/*.xml` returns
  `implicit_solvent_xml_missing` / `implicit_solvent_xml_ambiguous`.
  (b) **External GB XML** (e.g. the Greener group's `GB99dms.xml`) —
  loadable as arbitrary OpenMM XML, but `forcefield_catalog` cannot
  canonicalize a non-catalog GB XML to a named model. When the built System
  carries a GB force, the builder records `metadata.implicit_solvent="custom"`;
  downstream run tools inherit that value and verify that a GB force is present.
  The user still owns the external XML's scientific correctness.
  Out-of-version checks (e.g. `GB99dms.xml`
  needs OpenMM ≥ 8.0) still fire via existing guards. Like
  `build_amber_system`, this builder accepts `pablo_auto_download=False` for
  known local/offline topology loads. Successful results and node metadata use
  the curated-builder key shapes for `statistics`, `system_net_charge_e`,
  `forcefield_provenance`, `topology_notes`, and topology-build stage history.

## `simulation/` (registry name `md_simulation`)

- `inspect_openmm_platforms(...)`: lightweight OpenMM platform inventory and
  atom-count feasibility guidance before local explicit-water runs.
- `export_state_pdb(...)`: export a PDB by combining atom/residue records from
  `topology.pdb` with positions from `state.xml`. Useful for report artifacts
  and MDPrepBench `minimized_structure.pdb` submissions.
- `run_minimization(...)`: standalone post-topology minimization. In node mode
  topology inputs resolve from the `topo` ancestor, and the `min` node records
  `state`, `minimized_structure`, and `minimization_report` artifacts for
  downstream `eq` nodes. Its `solute_heavy` default uses prep provenance to
  include structural solute components while excluding added solvent, ions,
  and membrane lipids.
- `run_equilibration(...)`: restrained equilibration with an NVT heating stage
  and optional NPT density stage. In node mode topology inputs resolve from the
  `topo` ancestor; omitted HMR and implicit-solvent settings inherit that
  topology, with a 4 fs HMR or 2 fs non-HMR timestep default. New DAGs should
  parent `eq` from `min`; the minimized state
  is then auto-resolved and coordinate minimization is skipped while low-
  temperature warmup remains in eq. Eq-chain restarts resolve from eq/prod
  ancestors.
  Agents should prefer `nvt_time_ns` / `npt_time_ns` (CLI:
  `--nvt-time-ns` / `--npt-time-ns`) for user-facing duration requests;
  explicit `nvt_steps` / `npt_steps` remain available for low-level
  reproducibility.
- `run_production(...)`: production MD with topology-inherited HMR/implicit
  solvent, state/checkpoint persistence,
  DAG restart resolution, and timeline metadata. Accepts an optional custom
  force / CV bias via `custom_force_script` (an autograd-backed
  `energy(positions, ctx)` wrapped in `PythonTorchForce`; upstream deprecated
  the TorchScript `TorchForce`, so this is the only route), plus
  `custom_force_parameters` (JSON dict → `ctx.params`). The bias is added to
  the System in a dedicated force group before the Simulation is built, and
  bias energy + optional CV values are logged to
  `collective_variables.csv` (+ `.meta.json`). See
  `mdclaw/simulation/custom_forces.py`.
  It also accepts `distance_restraints` as one JSON `list[dict]` for native
  harmonic atom/center-of-mass distances. Every entry requires `name`,
  `selection_group1`, `selection_group2`,
  `force_constant_kj_mol_nm2`, and `target_distance_nm`. This route uses an
  OpenMM `CustomCentroidBondForce` with per-bond parameters, physical elemental
  mass weights (independent of HMR), automatic periodic displacement handling,
  and the same collective-variable artifacts. It is
  mutually exclusive with `custom_force_script`; biased restarts require an
  XML state rather than a binary checkpoint.

## `analyze/`

- `concat_trajectory(...)`: walks the selected production continuation chain
  oldest first, applies atom selection and stride, and writes combined DCD,
  reference PDB, selection JSON, and (when available) combined energy CSV.
  For DAG-resolved production inputs it also writes `frame_times_ns` from the
  aligned energy `Step` values and each prod node's `timestep_fs`; trajectory,
  energy, and timestep are collected in one lineage walk so skipped artifacts
  cannot shift their correspondence.
- `fit_trajectory(...)`: aligns trajectories without changing frame count;
  downstream analyze nodes retain the ancestor `frame_times_ns` artifact.
- `analyze_rmsd(...)`, `analyze_distance(...)`, and `analyze_q_value(...)`:
  write a CSV `time_ns` column only when a DAG-resolved `frame_times_ns`
  artifact exists. Direct and legacy inputs without it produce frame-only CSVs
  instead of assuming a fixed output cadence.

## `visualization/`

- `render_structure_preview(...)`: PyMOL headless PNG rendering for PDB/mmCIF.
  `style="system_box"` is the assembled-system view used from `solv` onward
  (`overview` after `prep`, which has no solvent or cell): protein cartoon
  coloured per chain, lipids sticks, water a transparent surface, ions spheres,
  everything else sticks, and the periodic cell as a wire box drawn around the
  solvent and lipids — centring it on the whole system would let a protein
  leaving the box drag the box after it. It renders two axis-aligned
  orthographic views, `structure_preview_png` down x with z vertical and
  `structure_preview_png_top` down z, because one projection hides whatever
  lines up with it; other styles keep their own camera and one image. Only
  orthorhombic cells are drawn. The manifest reports the representations
  actually rendered, read back from PyMOL, so a fallback cannot make it
  disagree with the image
  structure artifacts. In node mode it resolves a representative structure
  artifact from the current node, parent, or ancestors, writes a ray-rendered
  preview PNG plus PyMOL script and manifest under `artifacts/previews/`, and
  registers `structure_preview_png` / `structure_preview_manifest` on the node
  (for terminal nodes, whose `node.json` is sealed, the attachment is recorded
  as an append-only `preview_registered` event that the resolvers also read).
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

## `literature/`

- `pubmed_search(...)`: PubMed search.
- `pubmed_fetch(...)`: article metadata fetch.

## `slurm/`

- `inspect_cluster(...)`: discover partitions, GPUs, and local policy.
- `submit_job(...)`: submit one SLURM job and link it to an optional DAG node.
  When the run command requests a GPU OpenMM platform (`--platform CUDA`/
  `OpenCL`) but no `--gpus`/`--gres` is given, it auto-sets `--gpus 1` (warning
  emitted) so a CUDA run is never scheduled on a CPU-only node. Container
  runtime commands in the payload are refused unless `allow_container_command`
  is explicitly set; `configure_container` normally owns that wrapper.
- `submit_array_job(...)`: submit one SLURM array where each task maps to a DAG
  node command. Shares the same `--platform`-driven GPU autodetection as
  `submit_job`; a single GPU-platform task command flips the whole array to
  `--gpus 1`, and it applies the same container-command guard.
- `check_job(...)`: sync SLURM state and reflect failures into linked nodes.
- `list_jobs(...)`, `cancel_job(...)`, `check_job_log(...)`: operational
  helpers.
- `set_policy(...)`, `show_policy(...)`: resource policy management.
- `list_tracked_jobs(...)`: read `.mdclaw_jobs.jsonl` history and optionally
  sync state.
- `configure_container(...)`: configure Singularity wrapping for SLURM jobs.
  Use `--extra-flags=--nv` for GPU passthrough. The invalid `-nv` flag is
  rejected with `container_extra_flags_invalid` when configuring or submitting
  a container job (including arrays), also for previously saved settings.
  `source_mode` chooses which mdclaw the compute node runs. `image` (default)
  runs the package baked into the `.sif`, so a queued job is unaffected by later
  edits to a checkout. `overlay` binds the checkout and puts it on `PYTHONPATH`,
  matching what `bin/mdclaw` already does on the login node -- use it while
  developing, or a fix reaches the login node but not the job it submits.
  Overlay needs a checkout or plugin install (a directory holding both
  `bin/mdclaw` and `mdclaw/`); it is refused with
  `container_overlay_source_unavailable` where the package lives in
  site-packages, because binding that would replace the image's dependencies
  with the host's.

## `node/`

- `create_node(...)`: create a DAG node. `continue_from=<prod_id>` is restricted
  to production continuation and records explicit extension intent. Analyze nodes
  require `conditions.analysis_data_scope`; comparison analyses also require
  explicit subjects and mapping. When `parent_node_ids` is omitted, the
  canonical forward parent auto-resolves from the current completed frontier
  (the single completed leaf of the preferred parent type) and is reported as
  `auto_resolved_parent`. In canonical study jobs, ambiguous or empty frontiers
  return `node_context_required` plus candidate parents without creating a
  node; bare repair job directories keep the legacy parent-less behavior.
  Failure returns carry a stable `code` (e.g. `invalid_node_type`,
  `source_already_exists`, `analyze_parents_mixed`, `referenced_node_missing`).
  Successful creation returns a `next_command`
  pointing to the read-only `explain_node` preflight for the new node.
- `inspect_job(...)`: read-only summary of node statuses, leaves, unfinished-node
  claims/open needs, warnings, and the progress index for weak-agent re-entry.
- `wait_node(...)`: read-only polling helper for long-running nodes. It waits
  for a node to reach `completed` or `failed` and reports timeout with a
  structured `node_wait_timeout` code instead of encouraging duplicate branches.
- `explain_node(...)`: read-only node details plus execution-context validation
  and auto-resolved inputs for a candidate node.
- `trace_failure(...)`: read-only failed-node
  diagnosis. Reads `metadata.errors`, the latest failure artifact, recent
  events, and parent/dependency status, then returns `recovery_options` and
  `next_commands` for explicit branch creation.
- `update_workflow_state(...)`: unified writer for node status (`--node-id` +
  `--status`) and/or job-level params (`--params`, e.g. `execution_mode`). Merges
  the former `update_node_status` and `update_job_params` tools; the underlying
  `update_node_status` / `update_job_params` functions remain importable. Direct
  terminal updates are rejected; producer/failure helpers seal nodes only after
  recording their evidence.
- `manage_node_need(...)`: manage a node's open needs behind an `--action`
  selector (`add` / `clear` / `record_attempt`). Merges the former
  `add_node_need` / `clear_node_need` / `record_node_need_attempt` tools.

## `study/`

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
- `record_study_log(...)`: append study-level JSONL logs behind a
  `--record-type` selector (`decision` / `question` / `token_usage`). Merges the
  former `record_study_decision` / `record_study_question` / `record_token_usage`
  tools.

## `evidence/`

- `generate_md_evidence_report(...)`: JSON evidence summary for one job.
