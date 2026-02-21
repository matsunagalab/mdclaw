# MDZen: End-to-End Molecular Dynamics Setup in Minutes

**From PDB ID to Production-Ready Simulation - Automated**

MDZen transforms any PDB structure, FASTA sequence, or ligand-SMILES into a production-ready Amber/OpenMM simulation setup through AI-powered tools and domain knowledge.

**Architecture**: MCP servers (tools) + Skills (domain knowledge prompts) - works with Claude Code, Cursor, Windsurf, or any MCP-compatible AI assistant.

## Quick Start

### 1. Install

```bash
# Conda environment (scientific packages)
conda create -n mdzen python=3.11
conda activate mdzen
conda install -c conda-forge openmm rdkit mdanalysis biopython pandas numpy scipy openblas pdbfixer
conda install -c conda-forge ambertools packmol smina

# Install mdzen
git clone https://github.com/matsunagalab/mdzen.git
cd mdzen && pip install -e .
```

### 2. Use with Claude Code

```bash
# Start Claude Code in the mdzen directory
claude

# Run MD preparation (interactive)
> /md-prepare PDB 1AKE

# Run MD preparation (autonomous - all defaults)
> /md-prepare PDB 1AKE, chain A, no ligands, run end-to-end with defaults

# Production MD run
> /md-run resume job_XXXXXXXX, extend to 10 ns

# Analyze trajectory
> /md-analyze job_XXXXXXXX
```

### 3. Use with Other AI Assistants

Any MCP-compatible tool can use the unified MCP server:

```bash
# Start the MCP server
mdzen-mcp

# Or test with MCP Inspector
mcp dev src/mdzen/mcp_server.py
```

## Architecture

```
skills/                    # Domain knowledge (platform-agnostic .md)
  md-prepare/SKILL.md      # Structure -> Solvation -> Topology -> Quick MD
  md-run/SKILL.md           # Production MD runs
  md-analyze/SKILL.md       # Trajectory analysis

.claude/commands/           # Claude Code slash commands
  md-prepare.md             # /md-prepare
  md-run.md                 # /md-run
  md-analyze.md             # /md-analyze

src/mdzen/
  mcp_server.py             # Unified MCP entry point (mdzen-mcp)
  config.py                 # Timeout & server path settings

servers/                    # FastMCP servers (8 independent tools)
  _common.py                # Shared utilities (logging, tool wrappers, errors)
  research_server.py        # PDB/AlphaFold/UniProt retrieval
  structure_server.py       # Structure cleaning & parameterization
  genesis_server.py         # Boltz-2 structure prediction
  solvation_server.py       # Water box & membrane embedding
  amber_server.py           # Amber topology generation
  md_simulation_server.py   # OpenMM MD execution
  literature_server.py      # PubMed search
  metal_server.py           # Metal ion parameterization

tests/                      # 4-level test suite
  conftest.py               # Shared fixtures
  test_mcp_server.py        # Level 1: Unit tests
  test_server_smoke.py      # Level 2: Server smoke tests
  test_pipeline_1ake.py     # Level 3: Full 1AKE pipeline
  manual_checklist.md       # Level 4: Manual Claude Code tests
```

## MCP Servers

| Server | Tools | Description |
|--------|-------|-------------|
| research | `download_structure`, `get_alphafold_structure`, `inspect_molecules`, `search_proteins` | Structure retrieval and inspection |
| structure | `prepare_complex`, `clean_protein`, `clean_ligand`, `split_molecules` | Structure cleaning and preparation |
| genesis | `boltz2_protein_from_seq`, `rdkit_validate_smiles` | AI structure prediction |
| solvation | `solvate_structure`, `embed_in_membrane` | Solvent/membrane setup |
| amber | `build_amber_system` | Amber topology generation |
| md_simulation | `run_md_simulation` | OpenMM MD execution |
| literature | `pubmed_search`, `pubmed_fetch` | Literature search |
| metal | `parameterize_metal_ion`, `detect_metal_ions` | Metal ion handling |

### Testing Individual Servers

```bash
mcp dev servers/research_server.py
mcp dev servers/structure_server.py
mcp dev servers/solvation_server.py
mcp dev servers/amber_server.py
mcp dev servers/md_simulation_server.py
```

## Testing

4-level test suite covering unit tests through full pipeline integration.

```bash
# Level 1: Unit tests (fast, no external deps)
pytest tests/test_mcp_server.py -v

# Level 1 + existing tests
pytest tests/ -v -m "not slow and not integration"

# Level 2: Server smoke tests (requires conda env)
pytest tests/test_server_smoke.py -v

# Level 3: Full 1AKE pipeline (requires network + all tools, ~1-2 min)
pytest tests/test_pipeline_1ake.py -v

# All tests
pytest tests/ -v

# Keep pipeline artifacts for inspection
pytest tests/test_pipeline_1ake.py -v --basetemp=./test_output
```

| Level | File | Tests | Requirements |
|-------|------|-------|-------------|
| 1 | `test_mcp_server.py` | 15 | None (pure Python) |
| 2 | `test_server_smoke.py` | 15 | conda env (ambertools, openmm, rdkit) |
| 3 | `test_pipeline_1ake.py` | 7 | Network + full conda env |
| 4 | `manual_checklist.md` | - | Claude Code interactive |

## Configuration

Settings via `MDZEN_` environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MDZEN_OUTPUT_DIR` | `.` | Output directory |
| `MDZEN_DEFAULT_TIMEOUT` | `300` | Default timeout (seconds) |
| `MDZEN_SOLVATION_TIMEOUT` | `600` | Solvation timeout |
| `MDZEN_MEMBRANE_TIMEOUT` | `7200` | Membrane building timeout |
| `MDZEN_MD_SIMULATION_TIMEOUT` | `3600` | MD execution timeout |

## Troubleshooting

### packmol-memgen numpy compatibility

If `packmol-memgen` fails with `AttributeError: module 'numpy' has no attribute 'float'`:

```bash
SITE_PACKAGES=$(python -c "import site; print(site.getsitepackages()[0])")
sed -i.bak "s/np\.float)/float)/g; s/np\.int)/int)/g" \
    "$SITE_PACKAGES/packmol_memgen/lib/pdbremix/v3numpy.py"
```

## License

MIT License

## Citations

### Boltz-2
```
S. Passaro et al., Boltz-2: Towards Accurate and Efficient Binding Affinity Prediction.
bioRxiv (2025). doi:10.1101/2025.06.14.659707
```

### AmberTools
```
D. A. Case et al., AmberTools, J. Chem. Inf. Model. 63, 6183 (2023).
```

### OpenMM
```
P. Eastman et al., OpenMM 8: Molecular Dynamics Simulation with Machine Learning Potentials,
J. Phys. Chem. B 128, 109 (2024).
```
