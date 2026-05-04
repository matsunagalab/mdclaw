# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

**MDClaw** is an AI-powered system for generating molecular dynamics (MD) input files optimized for the Amber/OpenMM ecosystem. It uses:
- **CLI Tools** (`mdclaw <tool>`) for specialized MD operations
- **Skills** (domain knowledge prompts) for workflow guidance
- **Boltz-2** for AI-driven structure prediction
- **AmberTools** for topology generation and parameterization
- **OpenMM** for production-ready MD simulations

## Architecture

```
skills/                    # Domain knowledge (platform-agnostic .md)
  boltz-predict/SKILL.md    # AI structure prediction (Boltz-2)
  md-prepare/SKILL.md       # Structure → cleaning → solvation → topology
  md-equilibration/SKILL.md  # Energy min → NVT heating → NPT density
  md-production/SKILL.md     # Production MD runs
  md-analyze/SKILL.md        # Trajectory analysis
  hpc-run/SKILL.md           # HPC/SLURM job management

.claude/commands/           # Dev-only: local slash commands (/md-prepare etc.)
  md-prepare.md             #   Thin wrappers that read skills/*/SKILL.md
  md-equilibration.md       #   Not needed when installed as plugin
  md-production.md
  md-analyze.md
  hpc-run.md

.claude-plugin/              # Plugin marketplace metadata
  plugin.json
  marketplace.json

bin/                         # Plugin CLI wrapper (auto-added to PATH)
  mdclaw                     # Delegates to SIF or local install

hooks/                       # Plugin lifecycle hooks
  hooks.json                 # SessionStart -> auto-download SIF

scripts/                     # Setup & maintenance scripts
  setup-container.sh         # Download versioned SIF from GHCR

mdclaw/                    # All Python code consolidated here
  __init__.py               # __version__ + package marker
  _common.py                # Shared utilities (logging, BaseToolWrapper, errors, timeouts, guardrails, CANONICAL_WATER_MODELS)
  _registry.py              # Tool registry (SERVER_REGISTRY dict)
  _lock.py                  # File-based locking (fcntl.flock) for concurrent access
  _event.py                 # Append-only event log (one JSON file per event)
  _node.py                  # Node-based job graph management (schema v3)
  _cli.py                   # CLI entry point (mdclaw), enforces --job-dir/--node-id for workflow nodes
  research_server.py        # PDB/AlphaFold/UniProt retrieval, inspection
  structure_server.py       # Structure cleaning & parameterization
  genesis_server.py         # Boltz-2 structure prediction
  solvation_server.py       # Water box & membrane embedding
  amber_server.py           # Amber topology generation
  md_simulation_server.py   # OpenMM MD execution
  literature_server.py      # PubMed search
  metal_server.py           # Metal ion parameterization
  slurm_server.py           # SLURM job submission & management
  node_server.py            # Node management tools (create_node)

container/                  # Docker/Singularity containerization
  Dockerfile                # Multi-stage build (mambaforge -> conda-pack -> slim)
  scripts/entrypoint.sh     # Container entrypoint (conda activate + exec)
  scripts/test-container.sh # Container verification script

tests/                      # 4-level test suite
  conftest.py               # Shared fixtures (small_pdb, etc.)
  test_mcp_server.py        # Level 1: Unit tests (config, registry)
  test_cli.py               # Level 1: CLI unit tests
  test_guardrails.py        # Level 1: Structured guardrails (ff/water, OpenMM fallback, SLURM policy)
  test_server_smoke.py      # Level 2: Server smoke tests
  test_pipeline_prod_continue_dag.py # Level 3: Full 1AKE DAG + prod continuation integration
  test_literature_server.py  # PubMed server tests
  test_research_server_structure_analysis.py  # Structure analysis tests
  test_slurm_server.py      # SLURM server mock tests
  test_ligand_pathway.py    # Ligand parameterization tests (L1-L3)
  test_node.py              # Node system unit tests (lifecycle, IDs, events)
  test_event.py             # Event system tests
  manual_checklist.md       # Level 4: Manual Claude Code tests
```

## Development Workflow

### Daily Development Cycle

```
1. Edit code in mdclaw/
2. Lint:    ruff check mdclaw/
3. Test:    pytest tests/test_mcp_server.py tests/test_cli.py tests/test_guardrails.py tests/test_slurm_server.py -v
4. Smoke:   pytest tests/test_server_smoke.py -v        (if touching tool logic)
5. Commit
```

### Adding a New Tool

1. Add the function in the appropriate `mdclaw/*_server.py` as a plain Python function
2. Add the function to the `TOOLS` dict at the bottom of that server file
3. Add unit test in `tests/test_mcp_server.py` (tool registration check)
4. Add smoke test in `tests/test_server_smoke.py` (actual execution)
5. Run `mdclaw --list` to verify CLI auto-discovery
6. Update CLAUDE.md server section with the new tool signature

### Adding a New Server

1. Create `mdclaw/new_server.py` with tool functions and a `TOOLS` dict
2. Register in `mdclaw/_registry.py` (`SERVER_REGISTRY`)
3. Add to `mdclaw/__init__.py` `__all__`
4. Add smoke tests in `tests/test_server_smoke.py`
5. Update CLAUDE.md architecture diagram and server section

### Modifying Skills

Skills in `skills/*/SKILL.md` reference tools via CLI (`mdclaw <tool> ...`). When changing tool signatures, update the corresponding SKILL.md examples.

### Skill Workflow & Job Directory Structure

User flow: `/md-prepare` -> `/md-equilibration` -> `/md-production` -> `/md-analyze`

**Schema v3 (node-based, only supported schema):**

```
job_XXXXXXXX/
  progress.json                       # schema v3: thin index of nodes + cached summaries
  progress.lock                       # flock for concurrent writes
  nodes/
    source_001/                        # DAG root: structure acquisition
      node.json                       # records source_type/source_id/sha256/source_url
      node.lock
      artifacts/
        1AKE.pdb                      # downloaded / copied structure
        inspection.json               # optional: recorded by inspect_molecules
    prep_001/
      node.json                       # node state, artifacts, metadata
      node.lock
      artifacts/
        split/                        # split_molecules output
        merge/merged.pdb              # merge_structures output
        residue_mapping.json          # source→merged nucleic residue mapping
        ligand_params.json
    prep_002/                         # optional branch: modified nucleic prep
      artifacts/
        modified_nucleic.pdb
        modxna_params.json
        residue_mapping.json
    solv_001/
      node.json
      node.lock
      artifacts/
        solvated.pdb
        box_dimensions.json
    topo_001/
      node.json
      node.lock
      artifacts/
        system.parm7
        system.rst7
    eq_001/
      node.json
      node.lock
      artifacts/
        equilibrated.pdb
        equilibrated.xml            # saveState (preferred by downstream load)
        equilibrated.chk            # saveCheckpoint (kept for bit-identical reproduction)
    prod_001/                         # branch 1 from eq_001
      node.json
      artifacts/
        trajectory.dcd
        final_structure.pdb
        state.xml                   # saveState (end-of-run + periodic)
        checkpoint.chk              # saveCheckpoint (end-of-run + periodic)
        energy.dat
    prod_002/                         # branch 2 from eq_001
      node.json
      artifacts/
        trajectory.dcd
  events/
    <ISO8601>_<node_id>_<event_type>.json
```

**Design principles:**
- `skill = what to run` (orchestration only, no state mutation)
- `tool = run + record` (execution + state via `_node.py` helpers)
- Each node is independent: own directory, `node.json`, lock, `artifacts/`
- Parent-child relationships form a DAG (`parent_node_ids` list in node.json)
- `progress.json` is a thin index: `nodes` dict + cached `system`/`preparation`/`params`
- Events are append-only files in `events/` (no JSON array race conditions)
- Workflow tools receive `job_dir` + `node_id`, call `begin_node`/`complete_node`/`fail_node`
- CLI `--job-dir`/`--node-id` global flags inject these into tool kwargs, and workflow nodes require both flags

### Pre-commit Checklist

```bash
ruff check mdclaw/                                                                           # lint
pytest tests/test_mcp_server.py tests/test_cli.py tests/test_guardrails.py tests/test_slurm_server.py -v  # unit tests
pytest tests/test_server_smoke.py -v                                                          # smoke tests (if applicable)
```

### Release Workflow (Version Tag Sync)

Skills (plugin) and tools (SIF) are distributed through separate channels but must stay in sync via version tags.

```
Plugin (GitHub repo)              SIF (GHCR)
├── skills/SKILL.md               ├── mdclaw/*.py (baked in)
├── bin/mdclaw (wrapper)          ├── mdclaw CLI
├── hooks/ (auto-downloads SIF)   └── AmberTools, OpenMM, PyTorch
└── .claude-plugin/plugin.json
         ↕ version tag keeps them in sync ↕
```

**Release steps:**

```bash
# 1. Update version in all 4 locations (must match)
#    - mdclaw/__init__.py         __version__ = "X.Y.Z"
#    - pyproject.toml              version = "X.Y.Z"
#    - .claude-plugin/plugin.json  "version": "X.Y.Z"
#    - .claude-plugin/marketplace.json  "version": "X.Y.Z"

# 2. Commit, tag, push
git add -A && git commit -m "release: vX.Y.Z"
git tag vX.Y.Z
git push origin main --tags

# 3. Build, test, and push Docker image with version tag
docker build -f container/Dockerfile -t mdclaw:latest .
docker run --rm --gpus all -v $(pwd)/container/scripts/test-container.sh:/work/test.sh:ro \
    mdclaw:latest bash /work/test.sh
docker tag mdclaw:latest ghcr.io/matsunagalab/mdclaw:X.Y.Z
docker tag mdclaw:latest ghcr.io/matsunagalab/mdclaw:latest
docker push ghcr.io/matsunagalab/mdclaw:X.Y.Z
docker push ghcr.io/matsunagalab/mdclaw:latest

# 4. Users update via:
#    /plugin update mdclaw@mdclaw          (skills + bin/mdclaw wrapper)
#    SessionStart hook auto-pulls new SIF  (on next session start)
```

**Version locations** (keep in sync):

| File | Field |
|------|-------|
| `mdclaw/__init__.py` | `__version__` |
| `pyproject.toml` | `version` |
| `.claude-plugin/plugin.json` | `version` |
| `.claude-plugin/marketplace.json` | `metadata.version` and `plugins[0].version` |

## Development Commands

### Environment Setup

```bash
conda env create -f environment.yml
conda activate mdclaw
```

### CLI Usage

```bash
# List all tools
mdclaw --list

# Show version
mdclaw --version

# Tool help
mdclaw fetch_structure --help

# Run a tool (output is JSON on stdout)
mdclaw fetch_structure --source pdb --pdb-id 1AKE          # CIF by default
mdclaw inspect_molecules --structure-file 1AKE.cif
mdclaw solvate_structure --pdb-file merged.pdb --dist 15.0 --salt --saltcon 0.15

# Complex parameters via JSON
mdclaw prepare_complex --json-input '{"structure_file": "1AKE.pdb", "select_chains": ["A"]}'
```

Skills (SKILL.md) reference tools via CLI (`mdclaw <tool> ...`).

### Code Quality

```bash
ruff check mdclaw/
ruff check mdclaw/ --fix
```

### Testing

4-level test suite: unit -> smoke -> pipeline -> manual.

```bash
# Level 1: Unit tests (fast, no external deps)
pytest tests/test_mcp_server.py tests/test_cli.py -v

# Level 1 + existing tests (no conda env required)
pytest tests/ -v -m "not slow and not integration"

# Level 2: Server smoke tests (requires conda env with scientific packages)
pytest tests/test_server_smoke.py -v

# Level 3: Full 1AKE DAG + prod continuation integration (network + full conda env)
pytest tests/test_pipeline_prod_continue_dag.py -v

# All tests
pytest tests/ -v

# Keep pipeline artifacts for inspection
pytest tests/test_pipeline_prod_continue_dag.py -v --basetemp=./test_output
```

**Markers**: `slow` (Level 2+), `integration` (Level 3). Configured in `pyproject.toml`.

**Test patterns**:
- Tool functions are called directly: `tool_name(param=value)`
- Shared fixtures (`small_pdb`, `alanine_dipeptide_pdb`) in `tests/conftest.py`
- Pipeline tests use `self.__class__` attributes to pass state between ordered steps

## Tool Modules

### research_server.py
- `fetch_structure(source, pdb_id, uniprot_id, file_path, format, copy, output_dir, job_dir, node_id)` - Preferred structure-acquisition entry point. `source` is `pdb`, `alphafold`, or `local`; in node mode (`source` node), file is written to `nodes/<node_id>/artifacts/` with source-specific provenance metadata.
- `download_structure(pdb_id, format, output_dir, job_dir, node_id)` - Compatibility wrapper for RCSB PDB fetch. In node mode records `source_type=pdb`, `source_id`, `sha256`, `source_url`, `last_modified`, `cache_hit`, `fallback_used`
- `get_structure_info(pdb_id)` - Get PDB entry metadata
- `get_alphafold_structure(uniprot_id, format, output_dir, job_dir, node_id)` - Compatibility wrapper for AlphaFold DB fetch. In node mode, records `source_type=alphafold`, `model_version`, `cached=false` (AlphaFold entries are not locally cached)
- `register_local_structure(file_path, job_dir, node_id, copy)` - Compatibility wrapper for local file fetch. Default copies the file; `--no-copy` symlinks it (fragile).
- `inspect_molecules(structure_file, job_dir, node_id)` - Analyze chains, nucleic acids, glycans, ligands, ions. Standard DNA/RNA polymers are classified as `nucleic` (not ligand) with `summary.nucleic_label_ids` and `summary.nucleic_subtypes`; common glycan residues / carbohydrate entities are classified as `glycan` with `summary.glycan_label_ids`; modified nucleotides are detected as unsupported for the standard NA path. With `job_dir`/`node_id`, writes `inspection.json` under the node and emits an `inspection_completed` event (read-only — node status unchanged). `summary.ptm_residues` lists any SEP/TPO/PTR sites present (chain ids are the **source** chain ids, before any `merge_structures` reassignment), and `notes.ptm_handling` carries the handoff message to `phosphorylate_residues`.
- `detect_ptm_sites(structure_file)` - Helper used by `prepare_complex` and `build_amber_system` to scan a PDB/CIF for SEP/TPO/PTR sites; returns `[{"chain","resnum","name"}, ...]`.
- `search_structures(query)` - Search PDB database
- `search_proteins(query)` / `get_protein_info(uniprot_id)` - UniProt
- `analyze_structure_details(structure_file, ph)` - HIS/SS-bond analysis

### structure_server.py
- `prepare_complex(structure_file, output_dir, ..., job_dir, node_id)` - Full preparation pipeline. In node mode, `structure_file` auto-resolves from the job's single `source` ancestor. Standard DNA/RNA chains pass through unchanged as `nucleics` and are merged with cleaned proteins / parameterized ligands; glycan chains pass through unchanged as `glycans` and are not sent through ligand cleaning or antechamber. Writes `residue_mapping.json` for nucleic residues, plus `glycan_metadata.json` / `glycan_linkages.json` for glycoprotein GLYCAM topology and LINK-derived protein-glycan bonds. Chain-ID rule for `select_chains`: **pass the short chain ID as it appears in your input file** — `chain_id` (label_asym_id) for mmCIF, `author_chain` (auth_asym_id, = column 22) for PDB. See "Chain ID mapping" in `skills/md-prepare/setup.md` for why the two can disagree in mmCIF (e.g. 7QVK label `B` ↔ author `BBB`) and why gemmi's PDB `chain_id` is an internal artifact (`Axp` / `Ax1` / `Axw`) you never type
- `clean_protein(pdb_file, ...)` - PDBFixer + pdb2pqr protonation
- `clean_ligand(pdb_file, ...)` - Ligand parameterization
- `split_molecules(structure_file, select_chains, include_types)` - Extract components (`protein`, `nucleic`, `glycan`, `ligand`, `ion`, `water`; same "pass what's in your file" chain-ID rule as `prepare_complex`)
- `merge_structures(pdb_files, output_name)` - Merge PDB files
- `run_antechamber_robust(mol2_file, ...)` - Ligand parameterization: metal pre-check → amber_geostd → GAFF2 fallback
- `download_amber_geostd(output_dir, force)` - Download curated ligand parameter database (~28k entries)
- `create_mutated_structure(pdb_file, sequence, seq_file, name, output_dir, job_dir, node_id)` - In-silico mutagenesis via FASPR side-chain packing. Pass exactly one of `sequence` (FASPR-format string, lowercase=keep, uppercase=mutate) or `seq_file`. Designed to run AFTER `prepare_complex` as a `prep`-type node: in node mode, `pdb_file` auto-resolves from the nearest prep ancestor's `merged_pdb` artifact, and the mutated PDB is registered as both `merged_pdb` and `mutated_pdb` so the downstream `solv` resolver picks it up automatically. DAG: source → prep (clean) → prep (mutate) → solv → topo → eq → prod.
- `prepare_modified_nucleic(modifications, modxna_dir, job_dir, node_id)` - Generate modXNA parameters on a branched `prep` node after `prepare_complex`. Node mode only: input PDB auto-resolves from the nearest prep ancestor's `merged_pdb`, and source-coordinate targets resolve through ancestor `residue_mapping`. Each modification needs `chain`, `resnum`, `source_resname`, and explicit `backbone` / `sugar` / `base` fragment IDs. The tool runs `modxna.sh -i in.modxna`, records generated `.lib` + `frcmod.modxna` as `modxna_params`, renames only the resolved residue in `modified_nucleic.pdb`, and registers that file as downstream `merged_pdb`. Terminal 5′/3′ modifications are blocked in the initial implementation.
- `phosphorylate_residues(pdb_file, sites, sites_str, restore_from_detection, allow_partial, name, output_dir, job_dir, node_id)` - Apply phosphorylation (SER→SEP / THR→TPO / TYR→PTR) on a branched `prep` node after `prepare_complex`. Three input modes (mutually exclusive): `restore_from_detection=True` reads `metadata.detected_ptm_residues` from the nearest prep ancestor (re-introduces PTMs that the source PDB carried but PDBFixer stripped); `sites=[{"chain","resnum","target"}, ...]` for explicit sites; `sites_str="A:65:SEP,A:178:TPO"` for the same as a CLI string. Each site's current residue must be the standard counterpart of the target — mismatches return a structured error. Sites that cannot be located in the input PDB (typo, chain-id drift) are **fatal by default**; pass `allow_partial=True` to convert to a warning. The tool renames the residue and strips the hydroxyl H (`HG`/`HG1`/`HH`), keeping `OG`/`OG1`/`OH`; `build_amber_system` then rebuilds the phosphate atoms via `leaprc.phosaa*`. The phosphorylated PDB is registered as both `merged_pdb` and `phosphorylated_pdb` for downstream auto-resolve. DAG: source → prep (clean) → prep (phosphorylate) → solv → topo → eq → prod.

### genesis_server.py
- `boltz2_protein_from_seq(amino_acid_sequence_list, smiles_list, affinity, num_models, output_dir, msa_path, job_dir, node_id)` - Boltz-2 structure prediction
- `rdkit_validate_smiles(smiles)` - SMILES validation and canonicalization
- `pubchem_get_smiles_from_name(chemical_name)` - Get SMILES from compound name
- `analyze_plip_interactions(pdb_file)` - Protein-ligand interaction analysis

### solvation_server.py
- `solvate_structure(pdb_file, output_dir, water_model, dist, salt, saltcon, job_dir, node_id)` - Water box. In node mode, `pdb_file` auto-resolved from `prep` ancestor's `merged_pdb`
- `embed_in_membrane(pdb_file, output_dir, lipids, ratio, ..., job_dir, node_id)` - Membrane. In node mode, `pdb_file` auto-resolved from `prep` ancestor's `merged_pdb`
- `list_available_lipids()` - Available lipid types

### amber_server.py
- `build_amber_system(pdb_file, ligand_params, modxna_params, metal_params, box_dimensions, forcefield, water_model, nucleic_forcefield, glycan_forcefield, is_membrane, job_dir, node_id)` - tleap. In node mode, `pdb_file` auto-resolves from `solv` ancestor's `solvated_pdb` or nearest prep `merged_pdb`, and `modxna_params` / `glycan_metadata` / `glycan_linkages` auto-resolve from the nearest prep ancestor. Standard DNA/RNA residues auto-load `leaprc.DNA.OL15` and/or `leaprc.RNA.OL3` before `loadpdb` (`nucleic_forcefield="auto"` by default). Glycan residues auto-load `leaprc.GLYCAM_06j-1` (`glycan_forcefield="auto"` by default), and LINK-derived protein-glycan bonds are emitted after `loadpdb`. modXNA `.lib/.off` and `frcmod` records are fail-fast validated against PDB residue names and inserted as `loadamberparams` / `loadoff` before `loadpdb`; modified nucleic-like residues only remain blocking when no matching modXNA params are loaded. Ligand params are fail-fast validated before tleap, `UNL` residue repair is only allowed for a single unambiguous ligand residue name, and ligand charge/contact diagnostics are recorded without changing the equilibration protocol. Metal params are fail-fast validated before tleap (mol2 exists, frcmod name/path is loadable, residue name is present in the topology input PDB, and mol2 atom types match Amber ion style); invalid records return `invalid_metal_parameters`. PTM auto-load: scans the input PDB for SEP/TPO/PTR via `detect_ptm_sites`, and when present sources `leaprc.phosaa19SB` (ff19SB) / `leaprc.phosaa14SB` (ff14SB) immediately after the protein leaprc line. A forcefield with no paired phosaa library while PTMs are present returns guardrail code `phospho_forcefield_unsupported`. The chosen library and the residue list are stamped on the topo node as `metadata.phosaa_library` / `metadata.ptm_residues`.

### md_simulation_server.py
- `run_equilibration(prmtop_file, inpcrd_file, temperature_kelvin, pressure_bar, nvt_steps, npt_steps, restraint_atoms, restraint_force_constant, ..., job_dir, node_id)` - Standard staged minimization + low-temperature NVT warmup + NVT, followed by NPT when applicable. The staged minimization/warmup prelude is used for all NVT equilibration runs (explicit, implicit, apo, ligand-bound) without branching on ligand risk metadata. In node mode, `prmtop_file`/`inpcrd_file` auto-resolved from `topo` ancestor
- `run_production(prmtop_file, inpcrd_file, simulation_time_ns, ..., platform, device_index, restart_from, hmr, random_seed, job_dir, node_id)` - Production MD (HMR + 4fs default). In node mode: `prmtop_file`/`inpcrd_file` resolve from the `topo` ancestor; `restart_from` walks the DAG upward and **prefers the ancestor's `state` artifact (XML, cross-node portable); falls back to `checkpoint` (binary, same-GPU-only) for legacy DAGs that predate the saveState migration**. Resolution picks the nearest `prod` ancestor with a `state`/`checkpoint` artifact (extension case), falling through to the `eq` ancestor if no prod ancestor has one (fresh production). On load, `.xml` triggers `Simulation.loadState` and `simulation.currentStep` is restored from `node.json.metadata.final_step` of that ancestor (saveState does not persist the step counter); `.chk` triggers `Simulation.loadCheckpoint` which carries the step counter itself. Both formats are saved at end-of-run and periodically (via two `CheckpointReporter` instances, one with `writeState=True`); the `.chk` remains on disk as an escape hatch for bit-identical reproduction cases (committor / sensitivity analyses), but no code path reads it when a `.xml` is also present. `simulation_time_ns` is the time to run **in this call** — it is added on top of the restart state's `currentStep`, so `eq→prod` keeps its "full production length" meaning (the eq state is written with `final_step=0` by design) while `prod→prod` cleanly extends by the requested duration. The node records `start_step` / `start_time_ns` / `final_step` so analysis tools can place each segment on the right timeline, and DCDs are never appended across nodes

### literature_server.py
- `pubmed_search(query, retmax, sort)` - Search PubMed
- `pubmed_fetch(pmids)` - Fetch article details

### metal_server.py
- `detect_metal_ions(pdb_file)` - Find metal ions
- `parameterize_metal_ion(pdb_file, output_dir, metal_resname, metal_charge, water_model, ion_parameter_set, job_dir, node_id)` - Metal ion parameters. Default is `water_model="opc"` and `ion_parameter_set="normal"` (Amber Manual 12-6 normal usage set, e.g. `frcmod.ionslm_126_opc`). `ion_parameter_set="iod"` and `"hfe"` are explicit alternatives; `"12_6_4"` is recognized but blocked with `metal_1264_requires_parmed` until MDClaw implements and validates the required ParmEd `add12_6_4` topology post-processing. `opc3` is supported for Li/Merz frcmod selection.

### slurm_server.py
- `inspect_cluster(output_file)` - Discover partitions, GPUs, save config (preserves existing policy)
- `submit_job(script, job_name, partition, nodes, ntasks, cpus_per_task, gpus, gres, time_limit, memory, nodelist, dependency, output_dir, account, qos, extra_sbatch, environment, job_dir, node_id)` - Submit SLURM batch job (validates against policy). `--job-dir` + `--node-id` link the SLURM job to a DAG node: the tool stamps `slurm_job_id` / log paths onto `node.json.metadata`, advances `node.status = "queued"`, and records the linkage in `.mdclaw_jobs.jsonl`. Both must be passed together; `--node-id` alone is rejected.
- `submit_array_job(tasks, job_name, partition, cpus_per_task, gpus, gres, time_limit, memory, max_concurrent, dependency, output_dir, account, qos, extra_sbatch, environment)` - Submit a SLURM job array where each task maps 1:1 to a DAG node. `tasks` is a non-empty `list[dict]` with `{job_dir, node_id, command}` per entry. Generates a single sbatch with `#SBATCH --array=0-N-1[%max_concurrent]` and a `case $SLURM_ARRAY_TASK_ID` dispatcher; every target `node.json` is stamped with `slurm_job_id=<parent>_<task>` and `slurm_array_task_id`. Each task's command is wrapped with `singularity exec` per-task when a container is configured, binding that task's `job_dir`.
- `check_job(job_id)` - Check job status (squeue/sacct). Also reflects SLURM state onto the linked DAG node when one is recorded: RUNNING → `queued→running`, FAILED/TIMEOUT/OUT_OF_MEMORY/CANCELLED → `failed` via `fail_node` + `slurm_stderr_tail` in metadata. COMPLETED is intentionally left alone — the tool running inside the job is the sole writer for `node.status = "completed"`.
- `list_jobs(all_users)` - List user's SLURM jobs
- `cancel_job(job_id)` - Cancel a SLURM job
- `check_job_log(job_id, log_type, tail_lines)` - Read job log files
- `set_policy(allowed_partitions, denied_partitions, max_gpus_per_job, max_cpus_per_task, max_nodes, max_time_limit, max_memory, default_partition, default_account, default_qos)` - Set resource policy
- `show_policy()` - Show current resource policy
- `list_tracked_jobs(sync, job_dir, node_id)` - List all tracked jobs from `.mdclaw_jobs.jsonl` (full history); `--sync` updates status from SLURM; `--job-dir` / `--node-id` filter records to a specific DAG or node.
- `configure_container(image, bind_paths, extra_flags, disable)` - Configure Singularity container for SLURM jobs

### node_server.py
- `create_node(job_dir, node_type, parent_node_ids, dependency_node_ids, label, conditions, continue_from)` - Create a node in the job graph. `continue_from=<prod_id>` is sugar for `parent_node_ids=[<prod_id>]` restricted to `node_type=prod`; it validates that the referenced node is a prod, rejects mixing with `parent_node_ids`, and stamps `metadata.continued_from` on the new `node.json` to document extension intent. A `job_dir` is limited to one `source` root so a single DAG always describes one physical system; branch from `prep` onward for variants. At runtime, `resolve_node_inputs("prod")` reads `metadata.continued_from` and restarts *only* from that specific prod's `state` / `checkpoint` artifact (no silent fallback); if neither is present, it returns `restart_from_error` and `run_production` fails before touching OpenMM.
- `update_job_params(job_dir, params)` - Merge job-level metadata into `progress.json.params`. Use this to persist workflow-wide settings such as `execution_mode` (`autonomous` / `human_in_the_loop`) without hand-editing progress files.
- `update_node_status(job_dir, node_id, status)` - Update a node's status on both `node.json` (plus `updated_at`) and the `progress.json` index under the proper file locks. This is the single writer-path for status so the DAG index stays in sync for re-entry and monitoring. Callers that only want to merge unrelated metadata (e.g. `slurm_job_id`) can continue to edit `node.json` directly — only the status field needs the cross-file sync.

## CLI Interface

`mdclaw/_cli.py` provides `mdclaw` CLI that auto-discovers all tools from `SERVER_REGISTRY` in `_registry.py` and exposes them as argparse subcommands. Output is always JSON on stdout; logs go to stderr.

**Global flags**: `--job-dir` and `--node-id` provide node-based state tracking (schema v3). Workflow tools require both flags; the CLI injects them into tool kwargs before execution.

**Parameter mapping**: `snake_case` params become `--kebab-case` flags. `bool` uses `--flag`/`--no-flag`. `List[str]` uses `nargs='+'`. `Dict` accepts JSON strings. `--json-input '{...}'` passes all params as JSON.

**Exit codes**: 0 = success, 1 = tool returned `success: False` or exception.

## Key Technical Patterns

### Tool Module Pattern

All server files follow this pattern:
```python
def my_tool(param: str) -> dict:
    """Tool description."""
    return {"result": "..."}

TOOLS = {
    "my_tool": my_tool,
}
```

### Tool Registry

`mdclaw/_registry.py` maps server names to module paths:
```python
SERVER_REGISTRY = {
    "research": "mdclaw.research_server",
    "structure": "mdclaw.structure_server",
    # ...
}
```

`_cli.py` imports each module and collects its `TOOLS` dict for CLI exposure.

### Timeout Configuration

Centralized in `mdclaw/_common.py`:
```python
from mdclaw._common import get_timeout
timeout = get_timeout("solvation")  # MDCLAW_SOLVATION_TIMEOUT (7200s)
```

### Structured Guardrails

Parameter validation shared across tools uses structured guardrail results
defined in `mdclaw/_common.py`:

```python
from mdclaw._common import (
    CANONICAL_WATER_MODELS,        # canonical water-model alias map (shared across servers)
    normalize_choice,              # case-insensitive alias lookup
    create_guardrail_result,       # {field, message, severity, actual, expected, suggested_fix, code}
    split_guardrail_results,       # -> (blocking_errors, warnings)
    create_validation_error_from_guardrails,
    guardrail_messages,
)
```

Each guardrail result carries a stable `code` string
(e.g., `forcefield_water_blocked`, `openmm_fallback_unsupported_water_model`,
`metal_unsupported_water_model`, `policy_gpus_exceeded`). Skills and agents
should branch on `code`, not parse human-readable messages.

Current guardrail enforcement points:
- `amber_server.build_amber_system`: forcefield/water canonicalization (always) + explicit-solvent compatibility (when `box_dimensions` is set)
- `solvation_server.solvate_structure`: blocks `opc`/`opc3` on OpenMM fallback (no packmol-memgen)
- `metal_server.parameterize_metal_ion`: Amber Manual ion set validation (`normal`/`hfe`/`iod` allowed; `12_6_4` blocked until ParmEd post-processing is implemented), water-model frcmod mapping, and multi-metal charge override checks
- `slurm_server.submit_job`: policy checks return structured results (partition/gpus/cpus/nodes/time/memory); unparseable time/memory become warnings

## Configuration

### Environment Variables

```bash
export MDCLAW_OUTPUT_DIR="."
export MDCLAW_DEFAULT_TIMEOUT=300
export MDCLAW_SOLVATION_TIMEOUT=600
export MDCLAW_MEMBRANE_TIMEOUT=7200
export MDCLAW_AMBER_TIMEOUT=3600   # tleap wall-time (build_amber_system); raise for very large fusions
export MDCLAW_MD_SIMULATION_TIMEOUT=3600
export MDCLAW_LOG_LEVEL=WARNING
export MDCLAW_SLURM_TIMEOUT=120
export MDCLAW_GEOSTD_DIR="/path/to/amber_geostd"  # curated ligand parameter database
export MDCLAW_MODXNA_DIR="/path/to/modXNA"  # directory containing modxna.sh and dat/frcmod.modxna
export MDCLAW_MODULE_LOADS="cuda/12.0 amber/24"  # HPC module load commands
export MDCLAW_MODULE_INIT="/etc/profile.d/modules.sh"  # module init script path
```

## Container Build, Test & Publish

### Full Workflow (Docker build -> test -> GHCR push -> SIF conversion)

```bash
# 1. Build Docker image (3-stage: mambaforge -> OpenMM source build -> nvidia/cuda slim)
docker build -f container/Dockerfile -t mdclaw:latest .

# 2. Test the container (CPU)
docker run --rm mdclaw:latest bash container/scripts/test-container.sh

# 3. Test the container (GPU, if available)
docker run --rm --gpus all mdclaw:latest bash container/scripts/test-container.sh

# 4. Authenticate to GHCR
gh auth refresh --hostname github.com --scopes write:packages   # if token lacks write:packages
gh auth token | docker login ghcr.io -u <github-username> --password-stdin

# 5. Tag and push to GHCR
docker tag mdclaw:latest ghcr.io/matsunagalab/mdclaw:latest
docker push ghcr.io/matsunagalab/mdclaw:latest

# 6. (First time only) Set package visibility to public
#    Go to: https://github.com/orgs/matsunagalab/packages/container/mdclaw/settings
#    -> Danger Zone -> Change package visibility -> Public

# 7. Convert to Singularity SIF (on HPC or local machine)
singularity pull mdclaw.sif docker://ghcr.io/matsunagalab/mdclaw:latest

# 8. Test SIF
singularity exec --nv mdclaw.sif mdclaw --list
singularity exec --nv mdclaw.sif bash container/scripts/test-container.sh
```

### Key Notes

- **Image size**: ~11.4 GB (Docker, ~4.6 GB SIF), includes CUDA runtime, PyTorch, AmberTools, OpenMM (source-built), Boltz-2
- **GHCR registry**: `ghcr.io/matsunagalab/mdclaw:latest`
- **GPU support**: Minimum **NVIDIA driver 520** (the image ships CUDA 11.8; driver 450+ is the theoretical floor per the CUDA 11.8 release notes, 520+ is what we actively verify). `--nv` (Singularity) or `--gpus all` (Docker) enables GPU passthrough. Runtime stage uses `nvidia/cuda:11.8.0-runtime-ubuntu22.04`.
- **Why CUDA 11.8, not 12.x**: HPC clusters often mix Pascal/Turing-era nodes whose drivers are in the 460-520 range. CUDA 12.x requires driver ≥525, and its forward-compat libs are restricted to datacenter GPUs (Tesla / A100 / H100) — consumer GPUs return `CUDA_ERROR_COMPAT_NOT_SUPPORTED_ON_DEVICE`. CUDA 11.8 is the newest toolkit that works on any driver ≥520 without forward-compat tricks, on any NVIDIA consumer or datacenter GPU from Maxwell through Hopper.
- **OpenMM source build**: OpenMM 8.2.0 is built from source against the CUDA 11.8 toolkit in Stage 2 so PTX emitted at runtime by NVRTC matches the driver floor. NVRTC + nvrtc-builtins from CUDA 11.8 are copied into `/opt/mdclaw/lib/` so the slim runtime image has the JIT compiler available without pulling the devel base.
- **`write:packages` scope**: Required for `docker push` to GHCR; add via `gh auth refresh --scopes write:packages`
- **Singularity pull** requires the GHCR package to be **public** (or `SINGULARITY_DOCKER_USERNAME`/`PASSWORD` to be set)

## Known Issues

### packmol-memgen numpy compatibility (NumPy 1.24+)

```bash
SITE_PACKAGES=$(python -c "import site; print(site.getsitepackages()[0])")
sed -i.bak "s/np\.float)/float)/g; s/np\.int)/int)/g" \
    "$SITE_PACKAGES/packmol_memgen/lib/pdbremix/v3numpy.py"
```

### Protein Protonation (clean_protein)

Two-tier strategy:
1. **Primary**: pdb2pqr + propka (pH-aware) -> `.amber.pdb`
2. **Fallback**: pdb4amber + reduce (geometry-based)

## TODO

### Single-source DAG principle

Each `job_dir` should contain one physical system with exactly one `source`
root. Variant exploration happens by branching from `prep`, `solv`, `topo`,
`eq`, or `prod` nodes inside that same DAG. Supporting multiple independent
source roots in one job is intentionally out of scope because it makes input
resolution and system identity ambiguous.

### PTM coverage

v1 covers SEP / TPO / PTR (phospho-Ser/Thr/Tyr). The flow is:
`prepare_complex` detects them via `detect_ptm_sites` and PDBFixer strips
them as a normal nonstandard-residue replacement; `phosphorylate_residues`
then reapplies them on a branched prep node (either from the detected
list via `--restore-from-detection` or from explicit `--sites`).
`build_amber_system` auto-loads `phosaa19SB` (ff19SB) / `phosaa14SB`
(ff14SB) immediately after the protein leaprc.

Deferred — extends naturally from the residue-list-driven design but
each item needs its own library choice and atom-rewrite logic:

- **Phospho-histidine** (`H1D` / `H2D` / `HEP`; Amber names vary by
  library generation). Different parameterization story than
  Ser/Thr/Tyr.
- **Other PTMs**: O-GlcNAc, acetylation (`ALY`), methylation (`M3L`,
  etc.), ubiquitination, lipidation (myristoyl / palmitoyl). Each
  needs its own residue library and atom set.
- **Alternate phospho protonation states** (SEP `−1` vs `−2`). v1
  accepts the Amber library default; user-selectable states would need
  parallel library entries or per-residue overrides.
- **Crystallographic phosphate-coordinate preservation**. v1 destroys
  and rebuilds phosphate atoms via tleap templates; OK for MD setup
  (minimization absorbs it) but a regression for users who care about
  the original phosphate orientation. A future "preserve_atoms" path
  could keep the source coords by reading them off the merged.pdb
  before the strip step in `phosphorylate_residues` and re-injecting
  after rename.
- **Per-chain breakdown / PTM-aware roundtrip validation** in
  `inspect_molecules`. The `summary.ptm_residues` field exists today
  but is structure-wide; per-chain summaries and a roundtrip-validation
  block would make multi-chain PTM workflows easier to audit.

### MMDB database integration

Support metadata exchange with an MMDB (Molecular Modeling Database) system:

1. **Read**: populate `node.json` metadata from MMDB entries (forcefield
   recommendations, known issues, reference parameters for specific systems)
2. **Write**: register completed job results (system composition, simulation
   conditions, trajectory metadata) back to MMDB for cataloging
3. **Agentic workflow**: agents query MMDB to decide simulation parameters,
   compare results across systems, and auto-register new entries

Schema consideration: `node.json` may need an `mmdb` section for external
IDs and sync status. `progress.json` may need a top-level `mmdb_id` field.

### hpc-run skill audit and node-based integration

Status (2026-04-19): landed. `skills/hpc-run/SKILL.md` is now rewritten
for the node-based workflow, and `slurm_server` tools are DAG-aware:

1. **Node awareness** — `submit_job` / `submit_array_job` accept
   `--job-dir` / `--node-id` (single node) or a `tasks` list (array).
   DAG nodes are stamped with `slurm_job_id` / `slurm_array_task_id` on
   successful `sbatch`.
2. **Job tracking** — `.mdclaw_jobs.jsonl` rows carry `job_dir` /
   `node_id` / `parent_job_id` / `array_task_id`. `check_job` syncs
   RUNNING → `queued→running`, FAILED/TIMEOUT → `failed` + stderr tail.
   COMPLETED is intentionally not written back; the tool inside the job
   owns that transition.
3. **Container config** — `submit_job` auto-extracts `--*-file` /
   `--*-dir` paths (including `--job-dir`) into Singularity bind paths.
   `submit_array_job` wraps per-task commands individually so each
   container invocation binds only that task's `job_dir`.
4. **Monitoring loop** — `/loop` + `list_tracked_jobs --sync
   [--job-dir X] [--node-id Y]` is the recommended poll pattern. Terminal
   states in the tracker also reflect into `node.status`.
5. **Multi-node submission** — `submit_array_job` emits one sbatch with
   `#SBATCH --array=0-N-1[%max_concurrent]` and a case-statement
   dispatcher. Matches the common fan-out cases: N replicates from one
   `eq_001`, or one `prod_001` each across N job directories.

Remaining follow-ups (nice-to-have):
- Propagate SLURM state back into `progress.json` summary so the skill
  can surface "active SLURM jobs" at a glance without iterating
  tracker rows.
- Optional `check_job --poll` that blocks until terminal (mirror of
  `/loop` but without the outer scheduler).
