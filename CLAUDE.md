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
  md-prepare/SKILL.md      # Full MD preparation workflow
  md-run/SKILL.md           # Production MD runs
  md-analyze/SKILL.md       # Trajectory analysis
  hpc-run/SKILL.md          # HPC/SLURM job management

.claude/commands/           # Claude Code slash commands
  md-prepare.md             # /md-prepare -> reads SKILL.md
  md-run.md                 # /md-run
  md-analyze.md             # /md-analyze
  hpc-run.md                # /hpc-run

.claude-plugin/              # Plugin marketplace metadata
  plugin.json
  marketplace.json

servers/                    # All Python code consolidated here
  __init__.py               # __version__ + package marker
  _common.py                # Shared utilities (logging, BaseToolWrapper, errors, timeouts)
  _registry.py              # Tool registry (SERVER_REGISTRY dict)
  _cli.py                   # CLI entry point (mdclaw)
  research_server.py        # PDB/AlphaFold/UniProt retrieval, inspection
  structure_server.py       # Structure cleaning & parameterization
  genesis_server.py         # Boltz-2 structure prediction
  solvation_server.py       # Water box & membrane embedding
  amber_server.py           # Amber topology generation
  md_simulation_server.py   # OpenMM MD execution
  literature_server.py      # PubMed search
  metal_server.py           # Metal ion parameterization
  slurm_server.py           # SLURM job submission & management

tests/                      # 4-level test suite
  conftest.py               # Shared fixtures (small_pdb, etc.)
  test_mcp_server.py        # Level 1: Unit tests (config, registry)
  test_cli.py               # Level 1: CLI unit tests
  test_server_smoke.py      # Level 2: Server smoke tests
  test_pipeline_1ake.py     # Level 3: Full 1AKE pipeline integration
  test_literature_server.py  # PubMed server tests
  test_research_server_structure_analysis.py  # Structure analysis tests
  test_slurm_server.py      # SLURM server mock tests
  manual_checklist.md       # Level 4: Manual Claude Code tests
```

## Development Workflow

### Daily Development Cycle

```
1. Edit code in servers/
2. Lint:    ruff check servers/
3. Test:    pytest tests/test_mcp_server.py tests/test_cli.py -v
4. Smoke:   pytest tests/test_server_smoke.py -v        (if touching tool logic)
5. Commit
```

### Adding a New Tool

1. Add the function in the appropriate `servers/*_server.py` as a plain Python function
2. Add the function to the `TOOLS` dict at the bottom of that server file
3. Add unit test in `tests/test_mcp_server.py` (tool registration check)
4. Add smoke test in `tests/test_server_smoke.py` (actual execution)
5. Run `mdclaw --list` to verify CLI auto-discovery
6. Update CLAUDE.md server section with the new tool signature

### Adding a New Server

1. Create `servers/new_server.py` with tool functions and a `TOOLS` dict
2. Register in `servers/_registry.py` (`SERVER_REGISTRY`)
3. Add to `servers/__init__.py` `__all__`
4. Add smoke tests in `tests/test_server_smoke.py`
5. Update CLAUDE.md architecture diagram and server section

### Modifying Skills

Skills in `skills/*/SKILL.md` reference tools via CLI (`mdclaw <tool> ...`). When changing tool signatures, update the corresponding SKILL.md examples.

### Pre-commit Checklist

```bash
ruff check servers/                                      # lint
pytest tests/test_mcp_server.py tests/test_cli.py -v     # unit tests
pytest tests/test_server_smoke.py -v                      # smoke tests (if applicable)
```

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
ruff check servers/
ruff check servers/ --fix
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
- `download_structure(pdb_id, format)` - Download from RCSB PDB
- `get_structure_info(pdb_id)` - Get PDB entry metadata
- `get_alphafold_structure(uniprot_id, format)` - AlphaFold DB
- `inspect_molecules(structure_file)` - Analyze chains, ligands, ions
- `search_structures(query)` - Search PDB database
- `search_proteins(query)` / `get_protein_info(uniprot_id)` - UniProt
- `analyze_structure_details(structure_file, ph)` - HIS/SS-bond analysis

### structure_server.py
- `prepare_complex(structure_file, output_dir, ...)` - Full preparation pipeline
- `clean_protein(pdb_file, ...)` - PDBFixer + pdb2pqr protonation
- `clean_ligand(pdb_file, ...)` - Ligand parameterization
- `split_molecules(structure_file, select_chains, include_types)` - Extract components
- `merge_structures(pdb_files, output_name)` - Merge PDB files
- `run_antechamber_robust(mol2_file, ...)` - GAFF2 + AM1-BCC
- `create_mutated_structutre(input_pdb, mutation_indices, mutation_residues, name)` - In-silico mutagenesis

### genesis_server.py
- `boltz2_protein_from_seq(amino_acid_sequence_list, smiles_list, affinity)` - Boltz-2
- `rdkit_validate_smiles(smiles)` - SMILES validation
- `pubchem_get_smiles_from_name(chemical_name)` - PubChem lookup
- `pubchem_search_similar(smiles, n_results, threshold)` - Similar compound search
- `rdkit_calc_druglikeness(smiles)` - Drug-likeness assessment

### solvation_server.py
- `solvate_structure(pdb_file, output_dir, water_model, dist, salt, saltcon)` - Water box
- `embed_in_membrane(pdb_file, output_dir, lipid_type, ...)` - Membrane
- `list_available_lipids()` - Available lipid types

### amber_server.py
- `build_amber_system(pdb_file, ligand_params, metal_params, box_dimensions, forcefield, water_model, is_membrane)` - tleap

### md_simulation_server.py
- `run_md_simulation(prmtop_file, inpcrd_file, simulation_time_ns, ..., platform, device_index, restart_from, hmr)` - OpenMM

### literature_server.py
- `pubmed_search(query, retmax, sort)` - Search PubMed
- `pubmed_fetch(pmids)` - Fetch article details

### metal_server.py
- `detect_metal_ions(pdb_file)` - Find metal ions
- `parameterize_metal_ion(pdb_file, metal_name, ...)` - Metal parameters

### slurm_server.py
- `inspect_cluster(output_file)` - Discover partitions, GPUs, save config (preserves existing policy)
- `submit_job(script, job_name, partition, nodes, ntasks, cpus_per_task, gpus, time_limit, memory, output_dir, account, qos, extra_sbatch, environment)` - Submit SLURM batch job (validates against policy)
- `check_job(job_id)` - Check job status (squeue/sacct)
- `list_jobs(all_users)` - List user's SLURM jobs
- `cancel_job(job_id)` - Cancel a SLURM job
- `check_job_log(job_id, log_type, tail_lines)` - Read job log files
- `set_policy(allowed_partitions, denied_partitions, max_gpus_per_job, max_cpus_per_task, max_nodes, max_time_limit, max_memory, default_partition, default_account, default_qos)` - Set resource policy
- `show_policy()` - Show current resource policy

## CLI Interface

`servers/_cli.py` provides `mdclaw` CLI that auto-discovers all 45 tools from `SERVER_REGISTRY` in `_registry.py` and exposes them as argparse subcommands. Output is always JSON on stdout; logs go to stderr.

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

`servers/_registry.py` maps server names to module paths:
```python
SERVER_REGISTRY = {
    "research": "servers.research_server",
    "structure": "servers.structure_server",
    # ...
}
```

`_cli.py` imports each module and collects its `TOOLS` dict for CLI exposure.

### Timeout Configuration

Centralized in `servers/_common.py`:
```python
from servers._common import get_timeout
timeout = get_timeout("solvation")  # MDCLAW_SOLVATION_TIMEOUT (7200s)
```

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
export MDCLAW_MODULE_LOADS="cuda/12.0 amber/24"  # HPC module load commands
export MDCLAW_MODULE_INIT="/etc/profile.d/modules.sh"  # module init script path
```

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
