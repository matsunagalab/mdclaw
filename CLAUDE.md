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
  md-prepare/SKILL.md      # Structure → cleaning → solvation → topology
  md-equilibration/SKILL.md # Energy min → NVT heating → NPT density
  md-production/SKILL.md    # Production MD runs
  md-analyze/SKILL.md       # Trajectory analysis
  hpc-run/SKILL.md          # HPC/SLURM job management

.claude/commands/           # Dev-only: local slash commands (/md-prepare etc.)
  md-prepare.md             #   Thin wrappers that read skills/*/SKILL.md
  md-equilibration.md       #   Not needed when installed as plugin
  md-production.md          #   (plugin users get /mdclaw:md-prepare etc.)
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
  _progress.py              # Legacy progress tracking (schema v2, auto-updates progress.json)
  _cli.py                   # CLI entry point (mdclaw), supports --job-dir/--node-id
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
  test_pipeline_1ake.py     # Level 3: Full 1AKE pipeline integration
  test_literature_server.py  # PubMed server tests
  test_research_server_structure_analysis.py  # Structure analysis tests
  test_slurm_server.py      # SLURM server mock tests
  test_ligand_pathway.py    # Ligand parameterization tests (L1-L3)
  test_node.py              # Node system unit tests (lifecycle, IDs, events)
  test_event.py             # Event system tests
  test_progress.py          # Legacy progress.json (schema v2) tests
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

**Schema v3 (node-based, current):**

```
job_XXXXXXXX/
  progress.json                       # schema v3: thin index of nodes + cached summaries
  progress.lock                       # flock for concurrent writes
  nodes/
    fetch_001/                        # DAG root: structure acquisition
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
        ligand_params.json
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
        equilibrated.chk
    prod_001/                         # branch 1 from eq_001
      node.json
      artifacts/
        trajectory.dcd
        final_structure.pdb
        checkpoint.chk
        energy.dat
    prod_002/                         # branch 2 from eq_001
      node.json
      artifacts/
        trajectory.dcd
  events/
    <ISO8601>_<node_id>_<event_type>.json
```

**Schema v2 (legacy, still supported):**

```
job_XXXXXXXX/
  progress.json                       # schema 2.0: monolithic state
  split/  merge/  solvate/  topology/
  runs/run_001_300K/
    run.json
    equilibration/
    production/
```

**Design principles:**
- `skill = what to run` (orchestration only, no state mutation)
- `tool = run + record` (execution + state via `_node.py` helpers)
- Each node is independent: own directory, `node.json`, lock, `artifacts/`
- Parent-child relationships form a DAG (`parent_node_ids` list in node.json)
- `progress.json` is a thin index: `nodes` dict + cached `system`/`preparation`/`params`
- Events are append-only files in `events/` (no JSON array race conditions)
- Tools receive `job_dir` + `node_id`, call `begin_node`/`complete_node`/`fail_node`
- CLI `--job-dir`/`--node-id` global flags inject these into tool kwargs

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
mdclaw download_structure --help

# Run a tool (output is JSON on stdout)
mdclaw download_structure --pdb-id 1AKE --format pdb
mdclaw inspect_molecules --structure-file 1AKE.pdb
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

# Level 3: Full 1AKE pipeline integration (network + full conda env, ~1-2 min)
pytest tests/test_pipeline_1ake.py -v

# All tests
pytest tests/ -v

# Keep pipeline artifacts for inspection
pytest tests/test_pipeline_1ake.py -v --basetemp=./test_output
```

**Markers**: `slow` (Level 2+), `integration` (Level 3). Configured in `pyproject.toml`.

**Test patterns**:
- Tool functions are called directly: `tool_name(param=value)`
- Shared fixtures (`small_pdb`, `alanine_dipeptide_pdb`) in `tests/conftest.py`
- Pipeline tests use `self.__class__` attributes to pass state between ordered steps

## Tool Modules

### research_server.py
- `download_structure(pdb_id, format, output_dir, job_dir, node_id)` - Download from RCSB PDB. In node mode (`fetch` node), file is written to `nodes/<node_id>/artifacts/` with `source_type=pdb`, `source_id`, `sha256`, `source_url`, `last_modified`, `cache_hit`, `fallback_used`
- `get_structure_info(pdb_id)` - Get PDB entry metadata
- `get_alphafold_structure(uniprot_id, format, output_dir, job_dir, node_id)` - AlphaFold DB. In node mode, records `source_type=alphafold`, `model_version`, `cached=false` (AlphaFold entries are not locally cached)
- `register_local_structure(file_path, job_dir, node_id, copy)` - Register a user-supplied PDB/CIF/ENT as a fetch node artifact. Default copies the file; `--no-copy` symlinks it (fragile).
- `inspect_molecules(structure_file, job_dir, node_id)` - Analyze chains, ligands, ions. With `job_dir`/`node_id`, writes `inspection.json` under the node and emits an `inspection_completed` event (read-only — node status unchanged)
- `search_structures(query)` - Search PDB database
- `search_proteins(query)` / `get_protein_info(uniprot_id)` - UniProt
- `analyze_structure_details(structure_file, ph)` - HIS/SS-bond analysis

### structure_server.py
- `prepare_complex(structure_file, output_dir, ..., job_dir, node_id)` - Full preparation pipeline. In node mode, `structure_file` auto-resolved from a single `fetch` ancestor (multi-fetch parents fall back to explicit `--structure-file`)
- `clean_protein(pdb_file, ...)` - PDBFixer + pdb2pqr protonation
- `clean_ligand(pdb_file, ...)` - Ligand parameterization
- `split_molecules(structure_file, select_chains, include_types)` - Extract components
- `merge_structures(pdb_files, output_name)` - Merge PDB files
- `run_antechamber_robust(mol2_file, ...)` - Ligand parameterization: metal pre-check → amber_geostd → GAFF2 fallback
- `download_amber_geostd(output_dir, force)` - Download curated ligand parameter database (~28k entries)
- `create_mutated_structutre(input_pdb, mutation_indices, mutation_residues, name)` - In-silico mutagenesis

### genesis_server.py
- `boltz2_protein_from_seq(amino_acid_sequence_list, smiles_list, affinity)` - Boltz-2
- `rdkit_validate_smiles(smiles)` - SMILES validation
- `pubchem_get_smiles_from_name(chemical_name)` - PubChem lookup
- `pubchem_search_similar(smiles, n_results, threshold)` - Similar compound search
- `rdkit_calc_druglikeness(smiles)` - Drug-likeness assessment

### solvation_server.py
- `solvate_structure(pdb_file, output_dir, water_model, dist, salt, saltcon, job_dir, node_id)` - Water box. In node mode, `pdb_file` auto-resolved from `prep` ancestor's `merged_pdb`
- `embed_in_membrane(pdb_file, output_dir, lipid_type, ..., job_dir, node_id)` - Membrane
- `list_available_lipids()` - Available lipid types

### amber_server.py
- `build_amber_system(pdb_file, ligand_params, metal_params, box_dimensions, forcefield, water_model, is_membrane, job_dir, node_id)` - tleap. In node mode, `pdb_file` auto-resolved from `solv` ancestor's `solvated_pdb`

### md_simulation_server.py
- `run_equilibration(prmtop_file, inpcrd_file, temperature_kelvin, pressure_bar, nvt_steps, npt_steps, restraint_atoms, restraint_force_constant, ..., job_dir, node_id)` - NVT+NPT equilibration. In node mode, `prmtop_file`/`inpcrd_file` auto-resolved from `topo` ancestor
- `run_production(prmtop_file, inpcrd_file, simulation_time_ns, ..., platform, device_index, restart_from, hmr, random_seed, job_dir, node_id)` - Production MD (HMR + 4fs default). In node mode, `prmtop_file`/`inpcrd_file` from `topo` ancestor, `restart_from` from `eq` parent

### literature_server.py
- `pubmed_search(query, retmax, sort)` - Search PubMed
- `pubmed_fetch(pmids)` - Fetch article details

### metal_server.py
- `detect_metal_ions(pdb_file)` - Find metal ions
- `parameterize_metal_ion(pdb_file, output_dir, metal_resname, metal_charge, water_model)` - Metal ion parameters (default `water_model="opc"`; `opc3` is recognized as canonical but unsupported for ion frcmod)

### slurm_server.py
- `inspect_cluster(output_file)` - Discover partitions, GPUs, save config (preserves existing policy)
- `submit_job(script, job_name, partition, nodes, ntasks, cpus_per_task, gpus, gres, time_limit, memory, nodelist, dependency, output_dir, account, qos, extra_sbatch, environment)` - Submit SLURM batch job (validates against policy)
- `check_job(job_id)` - Check job status (squeue/sacct)
- `list_jobs(all_users)` - List user's SLURM jobs
- `cancel_job(job_id)` - Cancel a SLURM job
- `check_job_log(job_id, log_type, tail_lines)` - Read job log files
- `set_policy(allowed_partitions, denied_partitions, max_gpus_per_job, max_cpus_per_task, max_nodes, max_time_limit, max_memory, default_partition, default_account, default_qos)` - Set resource policy
- `show_policy()` - Show current resource policy
- `list_tracked_jobs(sync)` - List all tracked jobs from `.mdclaw_jobs.jsonl` (full history); `--sync` updates status from SLURM
- `configure_container(image, bind_paths, extra_flags, disable)` - Configure Singularity container for SLURM jobs

### node_server.py
- `create_node(job_dir, node_type, parent_node_ids, dependency_node_ids, label, conditions)` - Create a node in the job graph

## CLI Interface

`mdclaw/_cli.py` provides `mdclaw` CLI that auto-discovers all tools from `SERVER_REGISTRY` in `_registry.py` and exposes them as argparse subcommands. Output is always JSON on stdout; logs go to stderr.

**Global flags**: `--job-dir` and `--node-id` enable node-based state tracking (schema v3). When provided, the CLI injects these into tool kwargs and skips legacy progress updates.

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
- `metal_server.parameterize_metal_ion`: `SUPPORTED_ION_WATER_MODELS` drives the supported set; `opc3` is canonical-but-unsupported
- `slurm_server.submit_job`: policy checks return structured results (partition/gpus/cpus/nodes/time/memory); unparseable time/memory become warnings

## Configuration

### Environment Variables

```bash
export MDCLAW_OUTPUT_DIR="."
export MDCLAW_DEFAULT_TIMEOUT=300
export MDCLAW_SOLVATION_TIMEOUT=600
export MDCLAW_MEMBRANE_TIMEOUT=7200
export MDCLAW_MD_SIMULATION_TIMEOUT=3600
export MDCLAW_LOG_LEVEL=WARNING
export MDCLAW_SLURM_TIMEOUT=120
export MDCLAW_GEOSTD_DIR="/path/to/amber_geostd"  # curated ligand parameter database
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

- **Image size**: ~14.5 GB (Docker), includes CUDA runtime, PyTorch, AmberTools, OpenMM (source-built), Boltz-2
- **GHCR registry**: `ghcr.io/matsunagalab/mdclaw:latest`
- **GPU support**: Requires **NVIDIA driver 530+** (CUDA 12.1). Runtime stage uses `nvidia/cuda:12.1.1-runtime-ubuntu22.04`; `--nv` (Singularity) or `--gpus all` (Docker) enables GPU passthrough
- **OpenMM source build**: OpenMM is built from source against CUDA 12.1 toolkit in Stage 2, avoiding the driver 560+ requirement of pre-built pip/conda packages. NVRTC from CUDA 12.1 is bundled in the image.
- **CUDA forward-compat**: `LD_LIBRARY_PATH` includes `/usr/local/cuda/compat` so older host drivers can run newer CUDA toolkit
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

### Boltz-2 as a `fetch` node source

`boltz2_protein_from_seq` is the third structure-acquisition path (alongside
PDB / AlphaFold / local) but was not wired into the `fetch` node in v1.
Apply the same pattern: add `job_dir`/`node_id` to the tool, write the
predicted CIF/PDB into `nodes/<fetch_id>/artifacts/`, and call
`_complete_fetch_node` with `source_type="boltz2"` plus prediction metadata
(model version, sequence(s), SMILES list, affinity flag, sha256).

### Multi-fetch → `prep` (combine multiple sources)

`prep` currently auto-resolves `structure_file` only when it has a single
`fetch` ancestor. Extending to multi-source merges (e.g., two AlphaFold
monomers, or protein from one PDB + ligand pose from another) requires:
- A new structured artifact `structure_files` (list, mirroring
  `ligand_params`) that `resolve_node_inputs` returns when multiple
  `fetch` ancestors exist
- `prepare_complex` (or a dedicated merge tool) to consume the list and
  merge before chain processing

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

### Skill execution modes: autonomous vs human-in-the-loop

Currently skills have informal mode detection (keywords like "end-to-end",
"全部やって") and ad-hoc checkpoint logic scattered across SKILL.md files.
Formalize this into two well-defined modes:

1. **Autonomous mode**: skill runs the full pipeline without pausing.
   All decision checkpoints (chain selection, ligand inclusion, solvation
   params, etc.) use defaults or user-specified values. Errors are handled
   by retry/fallback logic, not by asking the user. Suitable for batch
   processing, scheduled agents, and e2e workflows.

2. **Human-in-the-loop mode** (default): skill pauses at each decision
   checkpoint to present options and wait for user input. Errors are
   reported with structured context for the user to decide next steps.

Implementation considerations:
- Add an explicit `mode` parameter to skills (or detect from `conditions`
  in the eq/prod node, or from a job-level `params.execution_mode` field)
- Define a standard set of checkpoints per skill with clear skip/ask rules
- Autonomous mode should still log decisions to `events/` for auditability
- Consider a hybrid mode where only critical checkpoints pause (e.g.,
  ligand failure) while routine steps proceed automatically

### hpc-run skill audit and node-based integration

The `hpc-run` skill (`skills/hpc-run/SKILL.md`) was written before the
node-based architecture. Audit and update:

1. **Node awareness**: `submit_job` should accept `--job-dir`/`--node-id`
   so that SLURM jobs are tracked as part of the DAG. The submitted script
   should propagate these flags to the inner `mdclaw` command.
2. **Job tracking**: link SLURM job IDs to node IDs in `node.json` metadata
   (e.g., `metadata.slurm_job_id`). `check_job` results should update the
   node status (running/completed/failed).
3. **Container config**: verify `configure_container` works with the
   node-based layout (bind paths need to include `nodes/` subdirectories).
4. **Monitoring loop**: `/loop` + `hpc-run check` should read node status
   to determine when to stop polling.
5. **Multi-node submission**: support submitting multiple prod nodes as
   a SLURM job array (one array task per prod node from the same eq parent).
