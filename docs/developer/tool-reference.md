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
  Missing internal loops rebuilt with MODELLER are built with the selected
  ligands and ions in the template as rigid BLK residues
  (`repair_nonpolymer_context`, default True) and measured against them
  afterwards (`complex_missing_residue_repair.nonpolymer_clearance`; a segment
  inside 2.2 A fails with `modeller_loop_nonpolymer_clash`). Declared and
  detected disulfides carry a `geometry` (`bonded` / `overlap` / `not_formed`);
  only `not_formed` after a repair is an error, `overlap` is the warning
  `disulfide_sg_overlap`. Hetero residues joined by a Covale record or by a
  heavy-atom contact within 1.9 A on the same author chain are one ligand unit
  in inspection and splitting (sucralose from a PDB, RRY+RRJ).
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
  ligand components. That refusal, like every refusal raised before the split
  has delivered the selection, leaves the prep node pending and rerunnable.
- `clean_protein(...)`: PDBFixer plus pdb2pqr protonation
  (`protonation_method="propka"` predicts states at `ph`; `"no-prediction"`
  keeps the force field's fixed states and ignores `ph`; `"standard"` is a
  deprecated alias), with fallback
  paths and optional site-specific residue protonation overrides rebuilt via
  OpenMM `Modeller.addHydrogens(variants=...)`. `context_pdb_files` (the other
  pieces of a multi-piece prep, passed by `prepare_complex`) are present, cut
  to a 12 A neighbourhood, while PDBFixer places missing atoms (best of up to
  four seeded placements by distance from the context, reported in the
  `completion_context` operation) and while pdb2pqr debumps and titrates
  (`protonation_context`); they never reach the output. If ACE/NME caps are present,
  cap-specific H completion runs here; topology builders do not repair them.
  Heavy internal missing-residue gaps stop with
  `code="pdbfixer_missing_residues_out_of_scope"` and recommend regenerating
  the source through MODELLER or Boltz-2.
- `clean_ligand(...)`: ligand chemistry cleaning; emits charged-graph SDF/PDB
  artifacts for topology-time ligand force-field resolution.
  Multiple input residues are unified and colliding names made unique.
  For an explicitly requested peptide-as-ligand, use `prepare_complex`'s
  `ligand_components=[{"selection":"B:1-7","residue_name":"LIG","smiles":"..."}]`.
  It validates complete source-subchain selection, external covalent links,
  exact isomeric chemistry and unchanged heavy-atom placement, then persists
  source/prepared/merged correspondence. It cannot implicitly cut/cap a fragment.
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
  Optional `disulfide_bonds` and `ligand_chemistry` describe standalone inputs;
  node mode resolves chemistry from the same nearest prep as `merged_pdb`.
  Patch-tile charge evaluation builds a force-field System with that bond plan,
  rejects incompatible sulfur chemistry and invalid charge sums, and records
  `neutralization.charge_evaluation` and `net_charge_after_swap`.
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

- `build_amber_system(...)`: openmmforcefields-based topology builder.
  `water_model` and `forcefield` default to `None`: in node mode the water
  model is inherited from the solv node (`parameters.water_model_source`)
  and an explicit different value is `solvation_topology_water_model_mismatch`
  with both ways out; the force field is paired with the water
  (`default_forcefield_for_water`: ff19SB for OPC, ff14SB for TIP3P).
  Outside node mode the defaults are OPC and ff19SB. Builder details:
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
  Every minimization (this node and the ten-iteration relaxation inside both
  topology builders) goes through `mdclaw/simulation/relax.py`:
  `minimize_robustly` runs a capped steepest descent while the largest force
  exceeds 1e5 kJ/mol/nm, then `minimizeEnergy`, and retries once from the
  starting coordinates if L-BFGS diverged. The report lands under
  `minimization.relaxation` (`steepest_descent`, `retried`, `diverged`).
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
- `run_sst2(...)`: one Simulated Solute Tempering 2 (SST2) walker as a
  `prod` node (`mdclaw/simulation/tempering.py`). The solute (an mdtraj
  `solute_selection`, cut at residue boundaries, or `solute_indices_file`)
  is REST2-scaled over `temperatures_kelvin` rungs while the rest stays at
  the reference temperature; rung moves every `exchange_interval_ps` are
  Gibbs draws over all rungs. The tempering runs in a separate process
  (`python -m SST2.driver` from the GPL-2.0 matsunagalab/SST2 fork, branch
  `mdclaw`, bundled in the runtime image and pinned by `MDCLAW_SST2_REVISION`;
  a development checkout is found via `sst2_home` / `MDCLAW_SST2_HOME`);
  MDClaw never imports SST2. Artifacts: `trajectory.dcd`, `energy.dat`, `state.xml`,
  `final_structure.pdb`, `tempering.csv` (step, rung temperature, per-term
  energies, effective weights), `tempering.json` (rung, ladder, running
  averages, weights, provenance), `solute_indices.json`, `sst2_driver.log`.
  Metadata carries `sampling_method: sst2` and a `tempering` summary (rung
  occupancy, rung changes, round trips, weights). `--continue-from` a
  completed `run_sst2` node restarts the same walker: its `tempering_state`
  sidecar and `state` are picked up automatically. `weights_file` (JSON list
  of rung free energies in kJ/mol) switches to a fixed-weight production
  stage; `scale_nonbonded=false` is the gREST dihedral-only mode;
  `pressure_bar` unset runs NVT. Stable codes: `sst2_not_installed`,
  `sst2_solute_required`, `sst2_solute_selection_invalid`,
  `sst2_solute_selection_empty`, `sst2_ladder_invalid`,
  `sst2_restart_missing`, `sst2_requires_pme`, `sst2_driver_failed`.
- `run_metadynamics(...)`: one walker of well-tempered metadynamics on a
  centre-of-mass distance as a `prod` node
  (`mdclaw/simulation/metadynamics.py`, OpenMM's built-in
  `openmm.app.metadynamics`). The coordinate (`distance_cv`: `name`,
  `selection_group1`, `selection_group2`, resolved by
  `restraints.resolve_centroid_groups` with the same raw-coordinate /
  minimum-image rule as `distance_restraints`) is biased on a grid
  `[cv_min_nm, cv_max_nm]` with Gaussians of `bias_width_nm` and
  `bias_height_kj_mol` every `deposition_interval_ps`, well-tempered by
  `bias_factor`; harmonic walls (`wall_force_constant_kj_mol_nm2`) hold the
  coordinate on the grid. Several walkers share their bias through
  `bias_dir` (a directory outside the nodes; the first walker writes
  `metadynamics_manifest.json`, others must match it), exchanging files
  every `save_interval_ps` without synchronisation. Artifacts:
  `trajectory.dcd`, `energy.dat`, `state.xml`, `final_structure.pdb`,
  `collective_variables.csv` (+ `.meta.json`; bias energy and the distance
  per frame), `metadynamics.csv` (distance, bias, Gaussian height per
  deposition), `metadynamics.json` (grid, walker id, loaded walkers,
  visited range), `metadynamics_total_bias.npy`,
  `metadynamics_self_bias.npy`, `free_energy.csv`
  (`F = -(T+dT)/dT V(s)`), `runtime_system.xml`, `integrator.xml`. Metadata
  carries `sampling_method: metadynamics` and a `metadynamics` summary.
  `--continue-from` a completed node rejoins a shared `bias_dir` or, without
  one, starts from the parent's total bias (`restart_bias_file`). Stable
  codes: `metadynamics_cv_invalid`, `metadynamics_grid_invalid`,
  `metadynamics_parameters_invalid`, `metadynamics_shared_bias_mismatch`,
  `metadynamics_restart_missing`, `metadynamics_restart_mismatch`,
  `distance_restraint_exceeds_half_box`.
- `run_production(...)`: production MD with topology-inherited HMR/implicit
  solvent, state/checkpoint persistence,
  DAG restart resolution, and timeline metadata (including `md_seconds`,
  `wall_seconds` and `ns_per_day`: integration time, whole tool call, rate). Refuses a hybrid (alchemical)
  topology ancestor (`hybrid_topology_production_blocked`, node left pending;
  `run_sst2` too) — lambda windows are the `fep` stage. An unbiased run
  refuses a `random_seed` a completed unbiased sibling already ran from the
  same restart ancestor (`production_sibling_seed_collision`, node left
  pending): the effective seed derives from the seed and the ancestor's step
  count, so the run would repeat that trajectory exactly;
  `allow_seed_reuse=True` accepts it for a deliberate bit-exact replay.
  Biased runs (custom force, restraints, PLUMED, steering) integrate a
  different System and are not checked. Accepts an optional custom
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
  mass weights (independent of HMR), raw-coordinate evaluation for a distance
  inside one molecule and minimum-image evaluation between molecules (targets
  above half the box are refused with `distance_restraint_exceeds_half_box`),
  and the same collective-variable artifacts. It is
  mutually exclusive with `custom_force_script`; biased restarts require an
  XML state rather than a binary checkpoint.
  `steering_time_ns` optionally ramps those centers from measured input
  distances, with `steering_update_interval_ps=1` controlling a right-endpoint
  staircase approximation to a linear ramp. Use independent `prod` nodes
  labeled `steered_X` from a common eq, followed by `umbrella_X` via
  `continue_from` without the steering flag. XML + matching `steering.json`
  preserve interrupted progress; repeat the original schedule to resume.
  Metadata records schedule completion separately from actual target errors;
  the CV log includes applied centers. Analysis lineage collection stops at
  the steered/fixed boundary. See `skills/md-production/distance-restraints.md`.
  The same steering flags also work with `custom_force_script`: the unchanged
  `energy(positions, ctx)` function receives read-only `ctx.steering` containing
  `progress`, `initial_positions` and `initial_box`. It defines its own CV and
  schedule shape; no CV registry or callback API is required. The initial input
  is saved as `steering_initial.npz` and hash-checked with the script/parameters
  on restart. Completed custom steering continues with progress fixed at 1,
  including further umbrella extensions (`sampling_role=fixed_bias`). The CV
  log includes reserved `steering_progress`; CV metadata carries the protocol
  separately from user parameters. Ordinary scripts see `ctx.steering=None`.
  See `skills/md-production/custom-force.md` for an angular steering example.
  `plumed_file` selects the mutually exclusive, history-free PLUMED route.
  The original input, resolved runtime input, protocol, COLVAR and native log
  are node artifacts; `continue_from` inherits them without modifying parents.
  PLUMED drives its own per-step schedule; `steering_time_ns` validates its end,
  not a second clock. See `skills/md-production/plumed.md` and
  `docs/developer/plumed.md` for the supported subset and conda/container builds.

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
- `analyze_metadynamics(...)`: convergence of well-tempered metadynamics
  as one number and one figure (`mdclaw/analyze/metadynamics.py`). Parents
  are `run_metadynamics` prod nodes (one walker each, `production_chain`
  pools a parent's `continue_from` chain, `segment` takes the leaf).
  `state_a` / `state_b` are two disjoint ranges of the coordinate in nm; the
  Gaussians of every walker are merged in time, the profile
  `F(s, t) = -(gamma/(gamma-1)) V(s, t)` is rebuilt at `n_time_points` times
  and `dF(t) = -kT ln(int_A e^{-F/kT} / int_B e^{-F/kT})` is written as
  `metadynamics_delta_f.csv` / `.png` and `metadynamics.json`. `verdict` is
  `converged` when both states were visited and the drift of dF over the
  second half is below `drift_tolerance_kj_mol` (default 2.5); the number to
  report is `delta_f_kj_mol` with `drift_second_half_kj_mol`. Warnings:
  `walkers_unequal_residence` (walkers sharing one bias spend very different
  fractions of their time in A: a slow orthogonal motion) and
  `gaussian_height_not_decayed`. Stable codes: `metadynamics_inputs_missing`,
  `metadynamics_report_missing`, `metadynamics_report_invalid`,
  `metadynamics_walkers_incompatible`, `metadynamics_scope_unsupported`,
  `metadynamics_states_invalid`.
- `analyze_tempering(...)`: MBAR over the rungs of one or more `run_sst2`
  walkers (`mdclaw/analyze/tempering.py`). Parents are the walkers' prod
  leaves (one walker per parent; `production_chain` pools each
  `continue_from` chain, `segment` takes the leaf block; `comparison` is
  refused with `tempering_scope_unsupported`). From `tempering.csv` it builds
  the reduced potential of every recorded configuration at every rung,
  `u_m = beta_ref (sum_f lambda_m^f E_f + sqrt(lambda_m) E_pw)` (the terms
  lambda does not scale cancel), runs pymbar MBAR, and writes `weights.json`
  (the rung free energies `f_k` in kJ/mol, ready for `run_sst2
  --weights-file`), `tempering_frames.csv` (one row per DCD frame: walker,
  node, frame index, chain frame index, step, time, rung, temperature,
  log-weight and normalised weight in the reference ensemble), a
  `tempering_mbar.json` summary (`N_k`, `f_k` with errors, per-walker rung
  occupancy, round trips, on-the-fly weights and single-walker MBAR, ESS) and
  `tempering.png`. `verdict` is `weights_converged` when every rung was
  visited, on-the-fly and per-walker weights sit within
  `weights_tolerance_kj_mol` (default 2.5) of the pooled `f_k` and every
  walker made `min_round_trips` (default 5); otherwise `weights_drifting`
  with `verdict_reasons`. `discard_ns` drops a burn-in per walker,
  `fixed_weights_only` keeps only fixed-weight blocks, `row_stride` thins the
  report rows (frame rows are always kept). Walkers on one node must share
  ladder, reference temperature, solute and fractional terms
  (`tempering_walkers_incompatible`). Direct mode takes
  `tempering_report_files` (+ `tempering_state_files`, `output_frequency_ps`).
  The DCD frame count is read from the file header (no mdtraj plugin, whose
  stdout chatter would corrupt the CLI JSON). Codes: `tempering_inputs_missing`,
  `tempering_report_missing`, `tempering_report_invalid`,
  `tempering_walkers_incompatible`, `tempering_scope_unsupported`,
  `pymbar_not_installed`.
- `analyze_rmsd(...)`, `analyze_distance(...)`, and `analyze_q_value(...)`:
  write a CSV `time_ns` column only when a DAG-resolved `frame_times_ns`
  artifact exists. Direct and legacy inputs without it produce frame-only CSVs
  instead of assuming a fixed output cadence.
- `analyze_rmsd(...)`, `analyze_distance(...)`, and `analyze_q_value(...)`:
  write a CSV `time_ns` column only when a DAG-resolved `frame_times_ns`
  artifact exists. Direct and legacy inputs without it produce frame-only CSVs
  instead of assuming a fixed output cadence.

## `fep/`

Hybrid-topology free energy perturbation for one point mutation, pure OpenMM
(no Perses/OpenFE dependency). Research notes and the design rationale are in
`docs/research/fep-references.md`; the agent procedure is `skills/md-fep/`.

- `build_hybrid_system(mutation, ...)` (`topo` node, `mdclaw/fep/build.py`):
  parses `[CHAIN:]<wt><resseq><mut>` against the prep PDB
  (`fep/mutant.py`), models the mutant side chain (HPacker, PDBFixer
  fallback) and splices only that residue into a copy of the WT PDB, builds
  both end states with `build_amber_system` (under
  `artifacts/endstates/`; `endstate_builder="openmm"` uses
  `build_openmm_system` with `forcefield_xml` instead, for force fields
  outside the Amber catalog — the solvation box is written as a CRYST1
  record with the same 2 Å padding `build_amber_system` applies; prepared
  ligands are not supported on that path). For a charge-changing mutation in
  a periodic box, `charge_correction="coalchemical_ion"` (default,
  `fep/coion.py`) rewrites one bulk water of the mutant end state into a
  counter-ion (parameters copied from an ion already in the System) before
  the merge, so the builder interpolates it through `fep_core`, the end-point
  check covers it, and both end states carry the same box charge; the
  molecule is tethered in force group 4. The result's `charge_correction`
  block carries `lambda_range` (= `phase_bounds`, where `fep_core` moves) and
  the `window_indices` inside it. Refuses with
  `fep_coion_parameters_unavailable` / `fep_coion_box_too_small` /
  `fep_coion_unsupported` rather than fall back; `"none"` runs uncorrected
  with a warning. `estimate_ddg` requires both legs to agree on it. Maps atoms (`fep/mapping.py`: backbone + CB core,
  everything else in the residue is a dummy in one state), fuses the two
  Systems (`fep/hybrid.py`), relaxes the appearing atoms at state B with the
  rest frozen, and checks that the hybrid reproduces both end-state energies
  at λ=0/1 (`endpoint_tolerance_kj_mol`, default 1; `platform` /
  `device_index` pick the OpenMM platform for these energies, `auto` = the
  fastest available). Writes the ordinary XML
  triple (the hybrid `topology.pdb` keeps the end states' Amber
  protonation-state / water names: `PDBFile` normalises them on load, so
  `restore_topology_resnames_from_pdb` puts them back on the loaded
  Topologies before the hybrid is derived) plus `hybrid_manifest.json` — the single record of the build
  (mapping, force counts, end-state files, relaxation and validation
  energies; the tool result and node metadata carry only a summary) — and
  `fep_protocol.json` (`n_windows` / explicit `lambda_schedule`, a strictly
  increasing list of lambdas from 0 to 1; five global parameters `fep_elec_old`,
  `fep_sterics_old`, `fep_core`, `fep_sterics_new`, `fep_elec_new`; phase
  bounds default λ=0.25/0.75, `phase_bounds="p1,p2"` moves the end of the
  decharge phase and the end of the steric swap, e.g. a longer decharge for a
  charge-changing mutation). Downstream `min`/`eq` treat the hybrid as a normal
  topology (λ=0 is the wild type); a `prod` node under it is refused at input
  resolution (`hybrid_topology_production_blocked`, the node stays pending —
  sampling on a hybrid is the `fep` stage). Charge-changing mutations pass
  with a warning. Codes: `fep_mutation_spec_invalid`,
  `fep_mutation_residue_not_found`, `fep_mutation_residue_ambiguous`,
  `fep_mutant_model_failed`, `fep_endstate_build_failed`,
  `fep_environment_mismatch`, `fep_mapping_failed`, `fep_unsupported_force`,
  `fep_hybrid_build_failed`, `fep_endpoint_validation_failed`,
  `fep_protocol_invalid`.
- `run_fep(...)` (`fep` node; parents `eq` or `fep`, `mdclaw/fep/run.py`):
  for each selected window (`lambda_indices`: all, `"0-6"`, `"0,3,7"`)
  restarts from the eq state, minimises briefly at the window's lambda (the
  appearing atoms were ghosts during eq, so solvent may overlap them), then
  runs `equilibration_time_ns` and `sampling_time_ns`; every
  `sample_interval_ps` it evaluates the reduced potential at **all** protocol
  windows (`u_kn` in kT, NPT adds pV) into `window_XX/energies.npz`. A NaN
  is retried at a halved timestep (`nan_retry.py`). `fep_windows.json`
  indexes windows → segment chains and is written before the first window and
  after every window (`"complete": false` until the last), so a sampling
  failure always reports an existing partial index (`indexed_windows` = what
  it lists, `sampled_windows` = what this node finished). No artifact holds an
  absolute path: the index and each `window_XX/window.json` are relative to
  their own directory, so a job directory can be moved. Extension (`fep`
  parent, exactly one)
  continues the parent's per-window states, chains its segments, and only
  accepts the parent's windows, ensemble and temperature; `restart_windows_file`
  does the same from an explicit (possibly partial) index, which is how a
  killed node's finished windows are recovered under a new node. Parent
  windows that are not re-sampled are carried into the new index unchanged
  (`carried_over_windows`; they need only their `energies.npz`, a `state.xml`
  is required only for windows that are continued), so a chain's leaf always
  lists every window.
  Ensemble follows the eq node (NPT pressure inherited, NVT stays NVT;
  `pressure_bar` 0 forces NVT); timestep/HMR follow the topology.
  `restraint_atoms` (`solute_heavy`, `CA`, `backbone`, `heavy`) with
  `restraint_force_constant` (kJ/mol/nm², default 100) adds one harmonic
  positional restraint to the equilibrated coordinates that is identical in
  every window of the leg (it cancels in the reduced-potential differences
  but changes the ensemble, e.g. to hold a fold the mutation would loosen);
  an extension inherits the parent's restraint and refuses a different one,
  the index records it, and `analyze_fep` never pools restrained with
  unrestrained windows. Argument
  and input-resolution errors are reported before the node starts (it stays
  pending). A fresh window with `equilibration_time_ns` below 0.05 gets a
  warning (the start minimisation cools the box). Trajectories are off by default
  (`trajectory_interval_ps`). Declared `lambda_indices` conditions are
  matched against the flag's own spelling. Codes:
  `fep_hybrid_topology_required`, `fep_lambda_index_invalid`,
  `fep_protocol_invalid`, `fep_windows_missing`, `fep_windows_incompatible`,
  `fep_parent_ambiguous`, `invalid_parameter_value`, `restraint_selection_empty`,
  `fep_sampling_failed`.
- `analyze_fep(...)` (`analyze` node whose parents are all `fep`, created
  with `analysis_data_scope: alchemical`; `mdclaw/fep/analysis.py`): merges
  the parents' `fep_windows.json` (protocols compared by content; temperature,
  pressure/ensemble and the positional restraint must match because `u_kn`
  carries pV/kT and the restraint is part of the Hamiltonian; same index
  across parents = pooled replicas; a segment listed by both a fep node and
  its extension child is counted once, with a warning), drops
  `discard_fraction` (0.1) of every
  segment, subsamples each segment separately to independent frames with
  `pymbar.timeseries` (segments are separate trajectories), pools, and runs
  MBAR. Writes
  `fep_result.json` with `dG_kj_mol` / `dG_error_kj_mol` (and kcal/mol),
  cumulative dG per window, per-phase contributions, neighbour overlaps
  (warning below 0.03), the overlap matrix and per-window sample statistics.
  The result and `fep_result.json` also repeat the leg's Hamiltonian record:
  `restraint` and `charge_correction` (`method`, `charge_change_e`, and for a
  co-alchemical ion its `lambda_range` / `window_indices`, copied from
  `hybrid_manifest.json`; `null` when no manifest is readable), so a leg can
  be judged on its own and an overlap dip can be placed inside or outside the
  co-ion windows.
  Direct mode: `fep_windows_files`. Codes: `fep_windows_missing`,
  `fep_windows_incomplete` (lists the unsampled indices),
  `fep_windows_incompatible`, `fep_analysis_failed`, `pymbar_not_installed`.
- `extract_tripeptide(mutation, ...)` (`prep` node whose parent is the
  protein's `prep` node, `mdclaw/fep/tripeptide.py`): the unfolded-state
  model. Cuts residues `i-flank..i+flank` of the mutated chain from the parent
  prep's `merged_pdb` (chain id, numbering and protonation variants kept),
  caps it with ACE/NME through `clean_protein` (`preserve_input_protonation`,
  `protonation_method` default `no-prediction`) and completes the node with the
  artifacts the `solv` / `topo` resolvers read from a prep (`merged_pdb`,
  `chain_identity_map`, `disulfide_bonds`, plus `fragment_pdb`). Metadata
  `leg_role = "unfolded"` (and `unfolded_model`, `derived_from_prep_node_id`)
  is how `estimate_ddg` and the envelope tell the legs apart. Warns on chain
  ends, peptide-bond breaks and CYX/CYM without their partner. Codes:
  `fep_fragment_prep_required` (no prep parent), `fep_mutation_*`,
  `fep_tripeptide_extraction_failed`, `fep_tripeptide_cap_failed`.
- `estimate_ddg(...)` (`analyze` node with
  `analysis_data_scope: comparison` over the two legs' `analyze_fep` nodes,
  `mdclaw/fep/analysis.py`): `ddG = dG(reference leg) − dG(comparison leg)`,
  errors in quadrature. `cycle="folding"` (default) names the legs `folded` /
  `unfolded` (positive = destabilising); `cycle="binding"` names them
  `complex` / `apo` (positive = weaker binding). The comparison leg is the
  parent whose prep ancestry carries `leg_role` (`extract_tripeptide` writes
  `"unfolded"`); otherwise `analysis_subjects` with the cycle's two leg names
  in parent order decide (`fep_leg_role_ambiguous` when neither). Before
  subtracting it checks mutation, lambda protocol (`protocols_equivalent`),
  force field, water model, HMR, temperature and pressure of both legs
  (`fep_legs_incompatible`; the node stays pending) — a positional restraint
  may differ between legs and is reported per leg.
  Writes `artifacts/ddg.json` and records `analysis = "fep_ddg"` with ddG on
  the node; appends a study-log decision when the job's params carry
  `study_dir`. Direct (Python) mode with `folded` / `unfolded` result files
  writes to `output_file`, else `<study_dir>/evidence/`, else `outputs/`.
  Codes: `fep_ddg_scope_invalid`, `fep_ddg_parents_invalid`,
  `fep_leg_role_ambiguous`, `fep_legs_incompatible`, `fep_result_invalid`,
  `invalid_parameter_value`.
- Reporting (`mdclaw/evidence/reporting.py`): `fep` nodes are sampling
  stages like `prod` (`sampling_node_ids`, `fep_node_ids`, and the
  `production_frontier` used for replica checks include them), each subject
  carries an `alchemical` block (hybrid topologies, fep nodes, MBAR legs,
  ddG — recorded metadata only), and `citations.py` selects
  `Gapsys2015pmx` / `Beutler1994SoftCore` (hybrid topo), `Shirts2008MBAR` /
  `Klimovich2015Guidelines` / `Chodera2007Timeseries` /
  `Chodera2016Equilibration` (MBAR leg) and `Seeliger2010Thermostability`
  (folding cycle) from node metadata.

Absolute binding free energy of a ligand (`fep/abfe.py`, `fep/decouple.py`,
`fep/boresch.py`; procedure `skills/md-abfe/`, design notes
`docs/research/abfe-references.md`) reuses `run_fep` / `analyze_fep` unchanged:

- `build_decoupled_system(ligand, ...)` (`topo` node): one
  `build_amber_system` build, then `decouple_ligand` rewrites the ligand's
  nonbonded terms under two of the five hybrid parameters — charges and the
  charge product of its 1-4 exceptions scale with `fep_elec_old`
  (electrostatics annihilated), ligand x environment LJ moves to a Beutler soft
  core under `fep_sterics_old`, ligand x ligand LJ stays at full strength in a
  second `CustomNonbondedForce` (sterics decoupled). `validate_decoupling`
  checks that the coupled state reproduces the built System and that the
  decoupled energy does not change when the whole ligand is moved onto another
  atom. The leg follows from the content: ligand alone -> `solvent`, with
  `fep_protocol.json` (18 windows: `--elec-lambdas`, `--sterics-lambdas`);
  anything else present -> `complex`, **without** a protocol
  (`metadata.fep.restraint_required`), so a `fep` node under it resolves to
  `abfe_restraint_required`. Refuses charged ligands before building
  (`abfe_charged_ligand_unsupported`, from the prep record, again from the
  assigned charges), covalent ones (`abfe_ligand_covalent`) and ambiguous
  selections (`abfe_ligand_ambiguous` with `ligand_candidates`). The manifest
  is registered under the `hybrid_manifest` artifact key (`kind:
  abfe_decouple`).
- `add_boresch_restraint(...)` (`topo` node whose parent is the complex
  leg's `eq`; `_ALLOWED_PARENT_TYPES["topo"]` gained `eq` and `["fep"]` gained
  `topo`, neither auto-resolved; the leg's `fep` nodes are children of this
  node and start from that `eq` state, and a `fep` node with no equilibrated
  ancestor resolves to `fep_equilibration_required`): runs 200 ps of the coupled complex from the eq state,
  `select_boresch_restraint` scores (N, C, CA) receptor triples against bonded
  heavy-atom ligand triples by the six coordinates' fluctuation in thermal
  widths, excludes theta outside [40, 140] degrees, refuses a loose pose
  (`abfe_restraint_unstable`). Ligand bonds come from the System
  (`bonded_pairs`), since `topology.pdb` has no CONECT records. Re-issues the
  XML triple with one `CustomCompoundBondForce` scaled by the global
  `fep_restraint` (default 1) and writes the 23-window protocol (`restrain`,
  `decharge`, `decouple_sterics`; `--restraint-lambdas`). Protocols now name
  their parameters (`global_parameters`, `protocol_parameter_names`) and may
  name their `phases`.
- `extract_ligand(ligand)` (`prep` child of the complex's `prep`): the
  ligand's atoms by residue name and number (merge relabels chains; `--ligand`
  accepts the author chain), its `ligand_chemistry` record, `leg_role =
  solvent`.
- `estimate_binding_dg(...)` (`comparison` analyze over two `analyze_fep`
  nodes; legs identified from each leg's manifest): `dG_bind = dG_solvent -
  dG_complex + dG_restraint - kT ln(sigma)` with the analytic Boresch term
  (`standard_state_restraint_free_energy`, 1 M) and
  `--ligand-symmetry-number`. `abfe_legs_invalid` / `abfe_legs_incompatible`
  otherwise. Writes `artifacts/binding_dg.json`, `analysis = "abfe_binding"`.

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

- `inspect_cluster(...)`: discover partitions, GPUs, and local policy. A
  partition that mixes GPU models reports `gpu_type: null` and lists them in
  `gpu_types` / `gpu_inventory` (nodes and GPUs per model) / `node_gres`
  (raw GRES and, when sinfo knows it, GresUsed per node) so a caller can pin
  `--gres gpu:<model>:N`; the text fallback (sinfo without the JSON
  serializer plugin) is announced in `warnings`. Every GPU entry of a node's
  GRES counts (`gpu:3090:1,gpu:a5000:1` is two models on one node,
  `node_gres[].gpu_models`). `gpu_inventory` and `node_gres` also sit at the
  top level, aggregated per physical node (a node in several partitions is
  counted once, as is `total_gpus`), with `gpus_total` / `gpus_used` /
  `gpus_free` per model and per node (`null` when the site reports no
  GresUsed); the mixed-partition warning carries the per-model summary.
  The output does not grow with the machine: above 32 nodes, host lists fold
  into Slurm ranges capped at 8 entries (`rk[0001-3000]`, `+N more`),
  `node_gres` becomes one row per distinct GRES string with node count and
  summed usage (`node_gres_grouped: true`), and `gpu_inventory` adds
  `nodes_with_free_gpus` / `free_node_list` so "where is something free" stays
  answerable without a per-node table. A 3000-node site returns < 6 kB.
- `submit_job(...)`: submit one SLURM job and link it to an optional DAG node.
  For a linked node and a literal `mdclaw ... run_production` (or
  `python -m mdclaw._cli ...`) command, `condition_preflight` reports the
  declaration/argument check before sbatch, using CLI defaults and the runtime
  comparator. Inherited conditions are deferred; shell scripts/wrappers on a
  `prod` node that cannot be checked are explicitly marked `skipped` (a
  warning), not validated; a node of another type, or a literal command that
  runs another tool, is `not_applicable` (no warning). The runtime guard
  remains authoritative. Neither check changes requested conditions.
  When the run command requests a GPU OpenMM platform (`--platform CUDA`/
  `OpenCL`) but no `--gpus`/`--gres` is given, it auto-sets `--gpus 1` (warning
  emitted) so a CUDA run is never scheduled on a CPU-only node. Container
  runtime commands in the payload are refused unless `allow_container_command`
  is explicitly set; `configure_container` normally owns that wrapper.
  Before sbatch, the container runtime is resolved to an absolute path on the
  worker search path (`MDCLAW_SLURM_PATH`, else the launcher's host PATH when
  readable, else PATH) and written into the script
  (`resolve_container_runtime`); when it cannot be resolved (a submission from
  inside the image), the script keeps the bare name and its preamble sources
  the node's `/etc/profile` (and the module init) first
  (`_container_runtime_preamble`), with a warning on the result. Only a
  configured absolute `--runtime` that does not exist is refused
  (`container_runtime_not_found`). `configure_container --runtime` pins the
  binary or its command name.
- `submit_array_job(...)`: submit one SLURM array where each task maps to a DAG
  node command. The array parent id is returned as both `parent_job_id` and
  `slurm_job_id` (the latter is what `submit_job` returns, so
  `--dependency afterok:<slurm_job_id>` reads the same field for either).
  Shares the same `--platform`-driven GPU autodetection as
  `submit_job`; a single GPU-platform task command flips the whole array to
  `--gpus 1`, and it applies the same container-command guard.
  Each task also receives the same production condition preflight before any
  task is submitted; results are indexed by `task_index`.
- `submit_mps_job(...)`: run several DAG nodes concurrently on one GPU
  allocation under NVIDIA MPS (Multi-Process Service). Takes the same
  `tasks` list as `submit_array_job`; one sbatch job holds `gpus` whole GPUs
  (default 1), starts a per-job MPS control daemon (private
  `CUDA_MPS_PIPE_DIRECTORY` under `$TMPDIR`, logs next to the job's), exports
  `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE = 200 / tasks_per_gpu` (NVIDIA's rule
  for OpenMM, overridable), launches every task in the background round-robin
  over `CUDA_VISIBLE_DEVICES`, waits, stops the daemon, and exits non-zero if
  any task failed. Every task command must say `--platform CUDA`
  (`mps_task_requires_cuda_platform`); more than 16 tasks per GPU is refused
  (`mps_tasks_per_gpu_exceeded`), more than 8 warned. `cpus_per_task` defaults
  to `cpus_per_sim` (2) per task. Each node is stamped with the shared
  `slurm_job_id`, `slurm_parent_job_id` and its `slurm_mps_slot`, and its own
  `<job_name>_<id>.task<slot>.out/.err`; the tracker carries one record per
  node under the same job id (`mps_slot`, `job_stdout_log`/`job_stderr_log`
  for the wrapper's logs). `check_job` reflects the job state onto every
  packed node, using each slot's own stderr as failure evidence, and
  `list_tracked_jobs --sync` queries Slurm once per job id. Container wrapping
  binds each task's `job_dir` plus `$CUDA_MPS_PIPE_DIRECTORY`. Same policy,
  partition, container and production-preflight handling as `submit_array_job`.
  Rationale and GB200 measurements: `docs/memo.md` (2026-09-16).
- `check_job(...)`: query squeue → scontrol → sacct, sync SLURM state and
  reflect failures into linked nodes. Returns `state_source` and `checked_at`.
  Missing/expired records return `slurm_status_unavailable`, never inferred
  completion; `last_observation`, when present, is historical, not current.
  For a `RUNNING` job linked to a `fep` node it adds `time_budget`
  (`fep_time_budget`: windows done / total, mean measured wall time per window
  from `fep_windows.json`, estimated remaining vs. time limit left, from the
  tracker's `time_limit` and Slurm's elapsed time) and a `time_limit_risk:`
  warning when the remaining windows will not fit; `list_tracked_jobs --sync`
  repeats that warning.
  When squeue answers, no longer lists the job, and a non-terminal node still
  carries that job id, the code is `slurm_job_vanished` with `stranded_nodes`, `stderr_tail`, and a `--clear-slurm-metadata` `next_action`;
  the node is reported, never sealed on that inference.
  `reason` carries Slurm's pending/held reason (`Priority`, `Resources`,
  `JobHeldUser`, `launch_failed_requeued_held`, ...) from `squeue --json`,
  the text fallback (`%r`) or `scontrol` (`Reason=`), so a caller can tell a
  held job from one that is merely waiting.
- `list_jobs(...)`, `cancel_job(...)`, `check_job_log(...)`: operational
  helpers.
- `set_policy(...)`, `show_policy(...)`: resource policy management.
- `list_tracked_jobs(...)`: read `.mdclaw_jobs.jsonl` history and optionally
  sync state. Records are replicated to the cwd, `output_dir`, and `job_dir`
  trackers (or the single `MDCLAW_JOBS_FILE`); reads de-duplicate and updates
  touch every copy. `--sync` returns `stranded_jobs` / `warnings` for
  `slurm_job_vanished` jobs.
- Submitters append a `container_not_configured:` warning when an `mdclaw`
  payload has no container config and no `environment` while the submitting
  mdclaw itself runs from an image (`uncontained_mdclaw_warning`).
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
  Python drivers that create many nodes under one naming rule pass the
  private `_node_id` (a structured id `<type>_<scope>_<letter><4 digits>...`,
  e.g. `prod_h3flip_r0013_w0050`; `node_id_invalid` / `node_id_exists`);
  it is never a CLI flag. Parent candidates in `parent_required` are capped
  at `ID_LIST_CAP` entries (`candidate_parent_count` carries the total).
- `inspect_job(...)`: read-only summary of node statuses, leaves, unfinished-node
  claims/open needs, warnings, and the progress index for weak-agent re-entry.
- `wait_node(...)`: read-only polling helper for long-running nodes. It waits
  for a node to reach `completed` or `failed` and reports timeout with a
  structured `node_wait_timeout` code instead of encouraging duplicate branches.
- `explain_node(...)`: read-only node details plus execution-context validation
  and auto-resolved inputs for a candidate node. Top-level `blocking_codes`
  unions `validation.blocking_codes` with the input-resolution code
  (`hybrid_topology_production_blocked`, ...); when the context is valid but
  inputs cannot be assembled, `next.action` is `blocked` rather than `run`.
- `trace_failure(...)`: read-only failed-node
  diagnosis. Reads `metadata.errors`, the latest failure artifact, recent
  events, and parent/dependency status, then returns `recovery_options` and
  `next_commands` for explicit branch creation.
- `update_workflow_state(...)`: unified writer for node status (`--node-id` +
  `--status`) and/or job-level params (`--params`, e.g. `execution_mode`). Merges
  the former `update_node_status` and `update_job_params` tools; the underlying
  `update_node_status` / `update_job_params` functions remain importable. Direct
  terminal updates are rejected; producer/failure helpers seal nodes only after
  recording their evidence. `--clear-slurm-metadata` frees a non-terminal node
  from a dead submission (drops `slurm_*` metadata, status -> `pending`, writes
  a `slurm_metadata_cleared` event; `slurm_job_still_active` while squeue lists
  the job). `--abandon [--reason]` seals a never-run `pending` node without a
  SLURM job or live children as `failed` / `node_abandoned`
  (`node_abandon_refused` otherwise).
- `manage_node_need(...)`: manage a node's open needs behind an `--action`
  selector (`add` / `clear` / `record_attempt`). Merges the former
  `add_node_need` / `clear_node_need` / `record_node_need_attempt` tools.

## `study/`

- `init_study(...)`: create a study directory used by both direct runs and
  campaigns.
- `bootstrap_md_workflow(...)`: create or reuse the canonical
  `study_dir/study.json` + `study_plan.json` + `jobs/<job_id>/progress.json`
  layout for any MD workflow, including simple one-system direct runs. Default
  `workflow_steps` are written for every job the `plan` declares;
  `sampling_stage` (`prod` default, `fep` for hybrid-topology FEP) names the
  stage after `eq`. A `job_id` the plan does not declare is refused with
  `job_not_in_study_plan` (`planned_job_ids`, and a `next_action` naming
  `record_study_plan --overwrite true`).
- `add_study_job(...)`: register existing or planned jobs.
- `list_study_jobs(...)`, `summarize_study(...)`: inspect study state.
- `record_study_plan(...)`, `get_study_plan(...)`, `list_study_plans(...)`:
  persist and inspect a lightweight scientific-question-to-MD-plan record
  (`plan` is the stored record, so the plan body is `plan.plan`; `job_ids` is
  the flat list of declared jobs; an existing plan id without `overwrite`
  is `study_plan_exists`). The
  plan is study-level intent only; job DAGs remain the execution source of truth.
- `record_study_log(...)`: append study-level JSONL logs behind a
  `--record-type` selector (`decision` / `question` / `token_usage`). Merges the
  former `record_study_decision` / `record_study_question` / `record_token_usage`
  tools.

## `rounds/`

Round-driven sampling on the job DAG: a *scheme* runs a batch of `prod`
segments (one node each), a *policy* plans the next batch, and the driver
creates it as the next round. Seed-varied replicas (built-in `replicas`
policy: every replica continues), a weighted ensemble (`we_resample`) and any
analyze tool that writes `next_round.json` share the loop. Segments are
ordinary `prod` nodes run by the scheme's stage tool (`run_production` by
default; `run_sst2` accepted), so restart resolution,
`concat_trajectory` over a lineage and `trace_failure` work on them
unchanged. Design notes: `docs/research/weighted-ensemble-plan.md`.

- `setup_rounds(job_dir, scheme, overwrite=False)`: validate and record a
  scheme under `progress.json.params.sampling_schemes[<scheme_id>]`. `scheme`
  is a JSON object: `scheme_id` (`[a-z][a-z0-9]{0,15}`), `policy`
  (`replicas` or an analyze-stage tool), `policy_args`, `stage_tool` (a
  prod-stage tool), `stage_args` (its arguments; never `job_dir`, `node_id`,
  `random_seed` or restart paths), `start` (`node_ids`: completed `eq` /
  `prod` nodes with a `state`, cycled over `n_replicas`), `initial_weights`
  (`uniform`, one number per replica, or none; weighted policies default to
  `uniform`), `segment_conditions` (declared on every segment), `seed` (base
  of every segment's `random_seed`), `max_rounds`. A `we_resample` policy is
  checked here, before any segment runs: the arguments resolve, the CVs
  compile on the scheme's topology, each start structure's pcoord is
  evaluated (`scheme.start_pcoords`) and must lie outside the target
  (`we_start_in_target`), and an intermolecular target stays below half the
  box. Codes: `rounds_scheme_invalid`, `rounds_scheme_exists`,
  `rounds_tool_invalid`, `rounds_start_node_invalid`, plus the `we_*` / `cv_*`
  codes of the policy check.
- `run_rounds(job_dir, scheme_id, max_rounds=None, max_aggregate_ns=None,
  max_wall_hours=None, executor="local", platform=None, device_index=None,
  mps_tasks_per_gpu=8, mps_gpus=1, mps_segments_per_task=None,
  mps_max_jobs=8, mps_time_limit="04:00:00", mps_poll_seconds=30.0,
  slurm_output_dir=None)`:
  advance the scheme. Per round: run the pending segments (`executor=local`:
  one after another in this process; `executor=mps`: submitted as
  `submit_mps_job` tasks, `mps_tasks_per_gpu x mps_gpus` segments per job,
  each task `mdclaw --job-dir .. --node-id .. <stage_tool> <stage_args as
  flags> --random-seed .. --platform CUDA`, waited for with `check_job`;
  a failed replica is retried as a `_t000N` sibling with a
  new seed, `rounds_replica_unstable` after three), plan the next round
  (`replicas`, or the policy tool on `analyze_<scheme>_r<round>` whose parents
  are the round's completed segments and whose dependency is the previous
  policy node), create it (`prod_<scheme>_r<round>_w<replica>`,
  `continue_from` the parent segment or `parent_node_ids=[start_node_id]`,
  `dependency_node_ids=[policy node]`, `metadata.scheme` with round, replica,
  seed, weight and lineage, `conditions.random_seed`; the whole round goes
  into the index in one write through `lifecycle._create_nodes_bulk`, with
  no per-node `explain_node` preflight — one node at a time cost 0.22 s per
  node on a 26k-node index, the bulk path 0.003 s). Returns at a round
  boundary on `max_rounds`, `max_aggregate_ns`, `max_wall_hours` or the
  policy's `stop`; rerun to continue — the state is read from the DAG. Codes:
  `rounds_scheme_missing`, `rounds_job_dir_unreachable` (the job directory
  does not exist from this process — inside a container, not bound),
  `rounds_executor_invalid`, `rounds_round_in_progress`, `rounds_segment_refused`,
  `rounds_replica_unstable`, `rounds_policy_failed`, `rounds_plan_invalid`,
  `rounds_create_failed`, `rounds_submit_failed` (`submit_mps_job` refused
  the round; its code is in the message), `rounds_slurm_unavailable`
  (`check_job` could not read the queue 20 polls in a row),
  `rounds_scheme_closed` (closed by `close_rounds`). Every node the
  driver runs in-process (local segments, in-process policy runs) carries an
  owner record while it runs (`nodes/<id>/owner.json`: host, pid, the
  driver's `SLURM_JOB_ID`, a heartbeat touched every 30 s;
  `mdclaw/rounds/owner.py`). A `running` node whose owner is gone — its
  process dead on this host, or its heartbeat older than 5 min from another
  host — is stale: `run_rounds` seals it `rounds_owner_lost`, retries it
  and lists it under `recovered`; a live owner answers
  `rounds_round_in_progress` naming host, pid, Slurm job and heartbeat age
  plus the manual release (`update_workflow_state --clear-slurm-metadata`).
  Nodes without a record (run by hand, mps tasks) are never judged stale.
  When the record names the owner's Slurm job and this host has `squeue`,
  a job that has ended (or that the controller no longer knows) makes the
  owner stale at once instead of after the heartbeat timeout (WE-16b).
- `executor=mps` runs the driver on the host: the launcher routes
  `run_rounds --executor mps` to the host Python (it needs `sbatch`), the
  policy runs through the launcher — inside the container — when the driver's
  interpreter has no OpenMM and in-process otherwise, and the Slurm scripts
  and logs go to `slurm_output_dir` (default `<job_dir>/slurm`). A segment
  whose job ended without sealing it is failed by `check_job`'s node sync
  (`slurm_completed_without_node_completion`; `slurm_job_vanished` when the
  queue kept no record) and retried like any failed replica. Segments left
  queued or running by an earlier driver are waited for, never resubmitted
  (the local executor answers `rounds_round_in_progress` for them). The MPS
  jobs run `mps_segments_per_task` segments per task one after another in
  one process (`run_segment_batch`, so the ~11 s container/Python/CUDA
  start-up is paid once per task; the task's first node is the one Slurm
  tracks, the others carry owner records while they run; segments a task
  never reached stay pending and go out again with the same seed). The
  default chooses it per round so the round still fills `mps_max_jobs`
  jobs — `ceil(pending / (tasks_per_gpu x gpus x mps_max_jobs))`, at most
  8 — because a task's segments run in series on one GPU and a large value
  with few segments would leave GPUs idle; `mps_time_limit` must cover that
  many packed segments (`slurm_jobs[].segments_per_task` reports the value
  used). The MPS
  jobs are submitted `--no-requeue`, so a node-side launch failure ends the
  job (FAILED / NODE_FAIL → segments failed and retried) instead of leaving
  it requeued and held; a job that is nevertheless PENDING with a held
  `reason` (`check_job` now reports Slurm's pending reason) is released
  twice for `launch_failed_requeued_held`, then cancelled so its segments
  are retried, while a user/admin hold is reported in `warnings` and waited
  for.
- `inspect_rounds(job_dir, scheme_id)`: read-only rounds: per-round status
  counts, the policy node and its ledger summary (`policy_summary`: walkers
  in / out, recycling events and weight, target weight, weight range),
  running nodes, `stale` (running nodes whose owner is gone: rerun
  `run_rounds` to recover them) with the owners' liveness reasons, totals
  (`aggregate_ns` = completed segments x `stage_args.simulation_time_ns`
  when the scheme states it — `aggregate_ns_source`; reading every
  segment's node.json took 298 s on a 13,300-segment scheme —
  `flux_events_total`) and
  `next_action` — `wait` while a round has running or queued nodes (a
  `run_rounds` process owns them), otherwise `run_rounds`; a closed scheme
  reports `closed` and `next` = `done`. Each round also lists `retired`
  replicas (failed with `node_abandoned`: never retried, the round completes
  without them — a weighted policy then refuses on the weight sum).
- `run_segment_batch(job_dir, node_ids, stage_tool="run_production",
  stage_args=None, platform=None, device_index=None)`: the MPS task command
  behind `mps_segments_per_task`: runs the listed pending (or queued: the
  task's tracked node) segments one after another in this process with the
  scheme's stage tool, each with its recorded seed and an owner record +
  heartbeat; a failed or refused segment does not stop the batch (the driver
  retries / resubmits). Returns per-segment `results` and `completed` /
  `failed` / `refused` / `skipped` counts; `rounds_batch_invalid` for a
  non-segment or missing node, `rounds_tool_invalid` for an unknown stage
  tool.
- `close_rounds(job_dir, scheme_id, reason=None)`: end a scheme on purpose
  (`scheme.closed = {at, reason}` in `progress.json`): `run_rounds` answers
  `rounds_scheme_closed`, `inspect_rounds` and the envelope's `next` of every
  node of the scheme say `done`; running segments finish, pending ones stay
  pending (retire them with `update_workflow_state --abandon`). A scheme with
  rounds is never replaced: continue under a new `scheme_id`.
- `next_round.json` (the policy contract, `mdclaw/rounds/plan.py`): a policy
  tool registers artifact `next_round` on its analyze node with
  `children[] = {replica, parent_node_id | start_node_id, weight?,
  random_seed?, conditions?, extra?}` and optional `stop` / `stop_reason`.
  Parents must be completed segments of the round; starts must be completed
  `eq` / `prod` nodes with a `state`.
- Every node of a scheme carries `metadata.scheme`, and the result envelope's
  `next` for such a node is `run_rounds` (never a single-node command). The
  scheme-level results (`setup_rounds`, `run_rounds`, `inspect_rounds`) carry
  a `message` and their own `next` (`scheme_next`: `run` with the
  `run_rounds` command, `wait` while another driver owns a round, `done`
  once the policy stopped the scheme).

## `we/`

Weighted ensemble (Huber & Kim 1996) as the policy of a `rounds` scheme:
walkers are the scheme's segments (`prod` nodes carrying
`metadata.scheme.weight`), the round's analyze node resamples them, and the
terminal analyze node turns the recycled flux into a rate. Design notes:
`docs/research/weighted-ensemble-plan.md`.

- `we_resample(job_dir, node_id, pcoord=None, bins=None, walkers_per_bin=None,
  target=None, recycle=None, basis_node_ids=None, extend_bins=None, chunk=1000)`:
  the policy tool (`scheme.policy = "we_resample"`; arguments default to the
  scheme's `policy_args`). Evaluates `pcoord` (CV specs of `analyze/cv.py`:
  `distance`, `rmsd`, `dihedral`, `q`) over every parent segment's
  trajectory, bins the last frame on `bins.edges` (outer bins run to
  infinity unless `extend_bins` is false), recycles walkers inside
  `target.pcoord_ranges` to `basis_node_ids` (default: the scheme's start
  nodes) with their weight when `recycle` is on (default: a target is given),
  merges the lightest pair and splits the heaviest walker until every bin
  holds `walkers_per_bin` (default 5). Artifacts: `next_round` (the `rounds`
  contract), `we_round` (walkers with weight, pcoord, bin, fate; bin ledger;
  flux; `target_weight`), `we_pcoords` (every frame). Merged-away and
  recycled walkers get `we_merged` / `we_recycled` events. Codes:
  `we_policy_args_invalid`, `we_weights_invalid`, `we_pcoord_out_of_bins`,
  `we_target_exceeds_half_box` (an intermolecular distance is a minimum-image
  distance), `we_inputs_missing`, `we_scope_unsupported`, and the CV codes
  `cv_spec_invalid`, `cv_selection_invalid`, `cv_box_missing`,
  `cv_trajectory_empty`. Argument errors leave the node pending.
- `analyze_we(job_dir, node_id, tau_ns=None, burn_in_rounds=None,
  temperature_kelvin=300.0, n_bootstrap=200, min_events=10,
  drift_tolerance_kt=1.0)`: terminal analysis over
  `we_resample` policy nodes (the latest round is enough; the chain is
  followed back through `metadata.scheme.previous_policy_node_id`). With
  recycling: the per-round flux `F(t)` is fitted with `F_ss (1 - exp(-t/tau))`,
  the rate is the mean over the rounds after a burn-in of two relaxation
  times (never less than the last quarter; `window` in the result; Hill
  relation). `verdict` is `flux_steady` when the window mean agrees with a
  determined plateau (`f_ss_err / f_ss < 0.5`), the relaxation is shorter
  than half the run — or, when the fit cannot pin the relaxation down (a
  spiky fast flux fits an exponential badly), when the flux has a stationary
  stretch at the end of the run: no Mann-Kendall trend (`p >= 0.05`) over at
  least a quarter of the run holding `min_events` events, the earliest such
  start after the first recycling event (a burst in the middle of a flat
  flux fails the test only for starts right at the burst and does not
  shorten the window; `window.stationarity`: test, start, p-value,
  candidates tested); the window is then that whole stretch and the
  bootstrap block its integrated autocorrelation time. Either way the window
  must be level (`window.level_check`: its first quarter against the rest,
  block-bootstrap intervals or 20 %), the fit path's burn-in is counted from
  the first recycling event, and `window.sensitivity` reports the mean for
  the start pushed later by quarters of the window (WE-24: an overshoot
  after the first arrivals and a rise a trend test missed moved rates by
  1.5x);
  `flux_undersampled` when the window holds fewer than
  `min_events` recycling events (default 10; `rate` is null — the window
  mean is a fluctuation, not a bound); `flux_transient` otherwise (enough
  events but still rising: a lower bound); `no_target_events` when nothing
  was recycled. A steady window is not yet a converged rate:
  `kinetics.convergence` follows the rate the analysis would have reported
  had the run stopped after each round (`rate_history`: `steady_state_rate`
  on the rounds up to that one, window choice included; every round of the
  second half, the first half thinned) and takes its range over the second
  half of the run, `ln(max k / min k)` in kT of barrier (`history_drift`;
  `analyze_metadynamics` takes the range of dF(t) the same way); at
  `drift_tolerance_kt` (1 kT = a factor of e) or more, or with no estimate
  yet at the middle of the run, `flux_steady` becomes `rate_not_converged`
  (`next_rounds_suggested` = half the run, 10-50). Every non-steady verdict
  carries `next_rounds_suggested`
  (two fitted relaxation times, or the rounds that fill the window with
  `min_events` at the observed event rate; 10-50) and the result
  envelope's `next` is then `run_rounds`. A
  moving-block bootstrap (block = the relaxation time in rounds, capped so
  at least five blocks fit the window — `window.block_capped` marks an
  optimistic interval instead of a zero-width one) gives the interval,
  `mfpt_ns = 1/k`, and, when the
  pcoord holds an intermolecular distance and the box volume is known, the
  rate per molar (`k_on` if the target is a bound state). Without
  recycling: the target population is fitted with the two-state relaxation
  (`k_ab`, `k_ba`). Several schemes as parents are analysed separately and
  pooled (mean, SEM); the node's `verdict` is the least settled scheme's
  (`verdict_scheme_id`; `next` extends `next_scheme_id`), and schemes whose
  rates differ by the drift tolerance or more draw a `schemes_disagree`
  warning. Artifacts: `we_kinetics` (JSON, with the per-stop history),
  `we_iterations`, `we_bins` (weighted bin populations after burn-in,
  `-kT ln P`), `we_frames` (every frame with its walker weight — the input
  of any weighted observable), `we_plot` (flux and distribution),
  `we_convergence` (CSV: scheme, round, rate and interval had the run
  stopped there) and `we_convergence_plot` (`we_convergence.png`, one panel
  per scheme, the second half shaded green / red).
- `analyze/cv.py` (no tool): `normalize_cv_specs`, `compile_cvs`,
  `evaluate_cvs` — the CV evaluators shared by `we_resample` and future
  adaptive schemes. A `distance` inside one molecule is measured on raw
  coordinates, between molecules with the minimum image; `rmsd` may align on
  one selection (`align_selection`) and measure another; `dihedral` is in
  degrees; `q` is the Best-Hummer-Eaton fraction of native contacts.

## `evidence/`

- `generate_md_report(...)`: deterministic, read-only report for selected DAG
  targets, replacing `generate_md_evidence_report` and
  `generate_study_evidence_report` (removed, with CLI migration hints).
  Pass **one** of `--job-dir`, `--study-dir` (optional `--plan-id`), or
  `--targets '[{"job_dir":"/path/jobs/r1","node_id":"prod_001","label":"r1"}, ...]'`.
  With no explicit targets, a unique leaf is selected. Multiple leaves, including
  failed/pending ones, return `report_selection_required` without writing files:
  ask which to combine as replicas, separate, or omit, then supply explicit
  targets and `--grouping replicas|separate`. Labels and target identities must
  be unique. Shared ancestors (including a common production prefix before
  branching) are reported explicitly, never counted as independent samples.
  Ancestor/descendant targets or two analyses of the same production frontier
  cannot be grouped as replicas. Independence of distinct branches is not certified.
  Each subject retains its parent/dependency history, declared conditions,
  recorded metadata/results, artifact bases and node-file hashes. Runtime
  integrator/System XML attributes and canonical per-Force definition hashes
  are read without importing OpenMM or running MD, with separate artifact hashes.
  Force hashes include nested bond/particle/group parameters. Attributes retain
  OpenMM serialization names and units (e.g. integrator `stepSize` is ps). Comparison separates
  declarations from recorded settings, including runtime restraints, custom
  forces, steering and PLUMED protocols, and compares stage occurrences, not
  trajectory frames. Steering progress and its file locator remain in history.
  Missing versus null are distinct. The output never
  certifies equal physical conditions, independence or convergence.
  JSON is returned on stdout; `--output-dir <new-directory>` also writes
  `report.json` and `references.bib`. Re-running into the same directory
  refreshes those two files and lists them under `files.replaced`; paths
  inside node directories are rejected. No DAG changes, trajectory conversion,
  pooling, Methods prose or MDDB upload are performed.
  Citation selection currently covers explicit OpenMM 8, LF-middle/BAOAB,
  MC barostats, HMR, ff14SB/ff19SB and selected water-model records. It distinguishes
  official method, related/base method and documentation-only evidence.
  Other force fields, preparation/analysis/custom methods and constraint-solver
  identity remain explicit unresolved items, not fabricated references.
- `export_mddb(output_dir, ...)`: create an **offline** MDDB-workflow bundle with
  the same target/grouping contract. Each project has `inputs.yaml`; each MD has
  a paired `system.pdb` / `trajectory.dcd`. Replicas remain separate `mds`, never
  concatenated; differing selected atom/bond identities require `separate`
  projects. A completed analysis target resolves its nearest unique parent-lineage
  DCD, recording both identities. Existing `combined_trajectory` artifacts use
  their recorded `reference_pdb`; no new concatenation or analysis is performed.
  Supply `--metadata '<JSON>'` with `name`, `authors`, `contact`, `license`,
  `linkcense` (official spelling), and `method`. These are exporter safeguards,
  not claims about mandatory website fields. Missing values return
  `mddb_metadata_required`; never invent identity, licensing or MD methods.
  Recorded settings cannot be overridden by conflicting metadata. MDDB units:
  frame spacing in ns, timestep in fs, temperature in K. Missing source frame
  spacing may be supplied as `metadata.framestep` before applying `stride`.
  CSV/frame-time artifacts must have regular spacing and match DCD frame counts.
  Default filtering removes water and standard Na/K/Cl counter ions, retaining
  lipids, ligands and other ions/metals. Optional `selection` uses MDTraj syntax
  and must exclude water. Chunked conversion preserves source PDB atom/residue
  identifiers and paired order; omitted residue gaps receive TER records, and
  reloaded PDB bonds must match the selected source topology. No imaging or
  fitting occurs. `manifest.json`
  records source/output hashes, retained atom indices, frame counts and labels;
  `report.json` and `references.bib` accompany the bundle. PDB is a structure
  fallback (`input_topology_filepath: 'no'`), **not** a force-field topology.
  Output must be new and outside node directories. No upload, ingestion,
  scientific QA or MDDB acceptance is implied. The minimal template follows the
  [official template at pinned commit 4e6dceee](https://github.com/mmb-irb/MDDB-workflow/blob/4e6dceeee67ce83650eed4aa2cfffe10107e2564/mddb_workflow/resources/inputs_file_template.yml).
  `skills/md-report/` routes reviews, Methods/BibTeX, and this export through
  deterministic CLIs without reconstructing facts in LLM-generated scripts.

Topology validation distinguishes `input_conservation` (prepared heavy-atom
identities across loading) from `core.atom_count_preserved` (output artifact
consistency). S–S validation checks specific atom pairs in Topology and System
(bond force or constraints), and checks CYX connectivity even without a plan.
`disulfide_chemistry_conflict` fails before System generation. Inspection and
split share AMBER sequence aliases; `cap_count` reports ACE/NME separately
from `sequence_length`.
