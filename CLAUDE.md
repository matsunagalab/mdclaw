# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

**MDZen** (MD + 膳/禅) is an AI-powered system for generating molecular dynamics (MD) input files optimized for the Amber/OpenMM ecosystem. It uses:
- **MCP Servers** (FastMCP) for specialized MD tools
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

.claude/commands/           # Claude Code slash commands
  md-prepare.md             # /md-prepare -> reads SKILL.md
  md-run.md                 # /md-run
  md-analyze.md             # /md-analyze

.mcp.json                   # MCP server config for Claude Code

src/mdzen/
  mcp_server.py             # Unified MCP entry point (mdzen-mcp)
  config.py                 # Timeout & server path settings

servers/                    # FastMCP servers (8 independent tools)
  research_server.py        # PDB/AlphaFold/UniProt retrieval, inspection
  structure_server.py       # Structure cleaning & parameterization
  genesis_server.py         # Boltz-2 structure prediction
  solvation_server.py       # Water box & membrane embedding
  amber_server.py           # Amber topology generation
  md_simulation_server.py   # OpenMM MD execution
  literature_server.py      # PubMed search
  metal_server.py           # Metal ion parameterization

common/                     # Shared utilities for servers
  base.py                   # BaseToolWrapper
  errors.py                 # Error handling
  utils.py                  # Common utilities
```

## Development Commands

### Environment Setup

```bash
conda create -n mdzen python=3.11
conda activate mdzen
conda install -c conda-forge openmm rdkit mdanalysis biopython pandas numpy scipy openblas pdbfixer
conda install -c conda-forge ambertools packmol smina
pip install -e .
```

### MCP Server Testing

```bash
# Unified server
mdzen-mcp                              # Start all servers via stdio
mdzen-mcp --servers research,structure  # Selective
mdzen-mcp --http --port 8080           # HTTP transport

# Individual servers with MCP Inspector
mcp dev servers/structure_server.py
mcp dev servers/research_server.py
mcp dev servers/solvation_server.py
mcp dev servers/amber_server.py
mcp dev servers/md_simulation_server.py
```

### Code Quality

```bash
ruff check src/mdzen/
ruff check src/mdzen/ --fix
ruff check servers/
pytest tests/
```

## MCP Servers

### research_server.py
- `download_structure(pdb_id, format)` - Download from RCSB PDB
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

### genesis_server.py
- `boltz2_protein_from_seq(amino_acid_sequence_list, smiles_list, affinity)` - Boltz-2
- `rdkit_validate_smiles(smiles)` - SMILES validation
- `pubchem_get_smiles_from_name(name)` - PubChem lookup

### solvation_server.py
- `solvate_structure(pdb_file, output_dir, water_model, dist, salt, saltcon)` - Water box
- `embed_in_membrane(pdb_file, output_dir, lipid_type, ...)` - Membrane
- `list_available_lipids()` - Available lipid types

### amber_server.py
- `build_amber_system(pdb_file, box_dimensions, forcefield, water_model, is_membrane)` - tleap

### md_simulation_server.py
- `run_md_simulation(prmtop_file, inpcrd_file, simulation_time_ns, ...)` - OpenMM

### literature_server.py
- `pubmed_search(query, max_results)` - Search PubMed
- `pubmed_fetch(pmids)` - Fetch article details

### metal_server.py
- `detect_metal_ions(pdb_file)` - Find metal ions
- `parameterize_metal_ion(pdb_file, metal_name, ...)` - Metal parameters

## Key Technical Patterns

### FastMCP Server Pattern

All servers follow this pattern:
```python
from fastmcp import FastMCP
mcp = FastMCP("Server Name")

@mcp.tool()
def my_tool(param: str) -> dict:
    """Tool description."""
    return {"result": "..."}

if __name__ == "__main__":
    mcp.run(transport="stdio")  # or "http"
```

### Unified MCP Server

`src/mdzen/mcp_server.py` imports all servers via `FastMCP.import_server()`:
```python
mcp = FastMCP("mdzen")
mcp.import_server("research", research_mcp)
mcp.import_server("structure", structure_mcp)
# ...
```

### Calling Decorated Functions Internally

In FastMCP 2.x, `@mcp.tool()` returns a FunctionTool object. Call `.fn` for internal use:
```python
result = split_molecules.fn(file, output_dir=out_dir)  # NOT split_molecules(...)
```

### Timeout Configuration

Centralized in `src/mdzen/config.py`:
```python
from mdzen.config import get_timeout
timeout = get_timeout("solvation")  # MDZEN_SOLVATION_TIMEOUT (600s)
```

## Configuration

### Environment Variables

```bash
export MDZEN_OUTPUT_DIR="."
export MDZEN_DEFAULT_TIMEOUT=300
export MDZEN_SOLVATION_TIMEOUT=600
export MDZEN_MEMBRANE_TIMEOUT=7200
export MDZEN_MD_SIMULATION_TIMEOUT=3600
export MDZEN_LOG_LEVEL=WARNING
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
