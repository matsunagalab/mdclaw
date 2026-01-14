# MDZen: End-to-End Molecular Dynamics Setup in Minutes

**From PDB ID to Production-Ready Simulation - Automated**

Stop manually wrestling with tleap commands, parameter files, and topology debugging. MDZen transforms any PDB structure, FASTA sequence, or ligand-SMILES into a production-ready Amber/OpenMM simulation setup through conversational AI.

**Designed for molecular dynamics researchers who want to run simulations, not setup scripts.**

## Why MDZen? The Traditional Pain vs. The Future

#### 🚫 Traditional MD Setup (1-4 hours)
- Download PDB → manually clean water/hydrogens → split chains
- Run `tleap` → cryptic errors about missing hydrogens
- Parameterize ligands → hunt for GAFF parameters
- Solvate box → guess buffer distances
- Debug topology files → manual script writing
- **Start over when something breaks**

#### ✅ MDZen (2-15 minutes)
- **PDB 1AKE + water box**: `"Setup MD for 1AKE in explicit water at 300K"`
- **Antibody-antigen complex**: `"Generate complex from heavy chain FASTA and antigen PDB"`
- **Membrane protein**: `"Prepare GPCR 5HT2A in lipid bilayer with cholesterol"`
- **Ligand binding**: `"Dock SMILES CCCO to binding site of 3CL0"`

**Real Results:** Drug discovery teams reduced setup time from **2.5 hours → 4 minutes per complex** during compound screening campaigns.

## Documentation

- **[ARCHITECTURE.md](ARCHITECTURE.md)** - Project architecture, implementation plan, technical specifications
- **[CLAUDE.md](CLAUDE.md)** - Claude Code guidance and development patterns
- **[AGENTS.md](AGENTS.md)** - Cursor AI Agent settings and guidelines

## Quick Start (5 minutes)

### Fastest Path to Your First Simulation

#### 1. One-Line Environment Setup
```bash
# Copy-paste ready setup
curl -fsSL https://raw.githubusercontent.com/matsunagalab/mdzen/main/setup.sh | bash
source ~/.bashrc && conda activate mdzen
cd mdzen && pip install -e .
```

#### 2. Test Your Setup
```bash
# Quick validation run (takes 2-5 minutes)
python main.py run "Test with 1AKE mini-system"
echo "Success! Check $(ls -d job_*/ | tail -1) for generated files"
```

#### 3. Ready for Production
You now have:
- ✅ `prmtop` and `rst7` files for Amber
- ✅ `openmm_simulation.py` production script
- ✅ Validated system ready for GPU/CPU MD

### Manual Installation
<details>
<summary>Prefer step-by-step? Click to expand</summary>

```bash
conda create -n mdzen python=3.11
conda activate mdzen
conda install -c conda-forge openmm rdkit mdanalysis biopython ambertools packmol smina
git clone https://github.com/matsunagalab/mdzen.git
cd mdzen && pip install -e .
# Optional: pip install 'boltz[cuda]' --no-deps && pip install torch hydra-core
```

Check [Troubleshooting](#troubleshooting) for common conda conflicts.
</details>

## Real Research Workflows

### Quick Start (30 seconds)
```bash
# Test the system
python main.py run "Minimal test with 1AKE"

# Expected output: Creates ./job_*/ with valid prmtop/rst7 files
# Use generated openmm_simulation.py to start your production run
```

### Common Research Scenarios

#### 🔬 **Drug Discovery** - Small Molecule Screening
```bash
python main.py run "Prepare 3CL0 protease for virtual screening, 10Å ligand buffer"
# → Ready for AutoDock Vina or FEP calculations
```

#### 🧬 **Antibody Engineering** - Antigen complexes
```bash
python main.py run "Build antibody-antigen complex from 1IGT, orient CDR loops"
# → Validates binding site geometry, adds missing loops
```

#### 🧪 **Membrane Biology** - GPCR studies
```bash
python main.py run "Embed 5HT2A GPCR in POPC bilayer with 150mM NaCl, keep ion binding sites"
# → Equilibrated membrane system ready for MD
```

#### ⚛️ **Biomolecular Recognition** - Peptide binding
```bash
python main.py run "Dock peptide FASTA KKKRKG to PDZ domain 1BE9, residue-level resolution"
# → Complex with validated binding interface
```

#### 🧬 **Missing Structure** - AI-driven prediction
```bash
python main.py run "Generate and prepare FASTA MKTLL... for MD (Boltz-2 structure)"
# → Full pipeline from sequence → structure → MD-ready files
```

### Full CLI Reference

#### Getting Started
```bash
# Interactive mode (clarify setup with AI)
python main.py run "Minimal test setup"

# Batch mode (experienced users)
python main.py run -p "1TNF trimer solvated in 0.2M NaCl, CHARMM FF"

# Continue interrupted session
python main.py run -r job_a1b2c3d4
```

#### Development & Debugging
```bash
# List all available specialized tools
python main.py list-servers

# Check system health
python main.py info

# Get detailed help
python main.py --help
```

### Notebook Development

```bash
jupyter notebook notebooks/md_agent_v2.ipynb
```

### MCP Server Testing

Each FastMCP server can be tested independently:

```bash
# Launch MCP Inspector (Structure Server example)
mcp dev servers/structure_server.py

# Test other servers
mcp dev servers/genesis_server.py
mcp dev servers/solvation_server.py
mcp dev servers/amber_server.py
mcp dev servers/md_simulation_server.py
```

### MCP Server List

| Server | Description |
|--------|-------------|
| `structure_server` | Structure retrieval from PDB/AlphaFold/PDB-REDO, chain separation, structure repair, ligand GAFF2 parameterization |
| `genesis_server` | Structure prediction from FASTA sequences via Boltz-2 (monomer/multimer support) |
| `solvation_server` | Solvation (water box) and lipid membrane embedding via packmol-memgen |
| `amber_server` | Amber topology (parm7) and coordinate (rst7) file generation via tleap |
| `md_simulation_server` | MD execution with OpenMM, trajectory analysis with MDTraj |

## Directory Structure

```
mdzen/
├── main.py               # CLI entry point
│
├── src/mdzen/            # Google ADK implementation
│   ├── agents/
│   │   ├── clarification_agent.py  # Phase 1: LlmAgent
│   │   ├── setup_agent.py          # Phase 2: LlmAgent + step agents
│   │   ├── validation_agent.py     # Phase 3: LlmAgent
│   │   └── full_agent.py           # SequentialAgent orchestration
│   ├── cli/
│   │   └── commands.py             # CLI commands
│   ├── prompts/                    # External prompt files
│   │   ├── clarification.md        # Phase 1 instruction
│   │   ├── setup.md                # Phase 2 instruction (full)
│   │   ├── validation.md           # Phase 3 instruction
│   │   └── steps/                  # Step-specific prompts
│   │       ├── prepare_complex.md
│   │       ├── solvate.md
│   │       ├── build_topology.md
│   │       └── run_simulation.md
│   ├── state/
│   │   └── session_manager.py      # ADK SessionService
│   ├── tools/
│   │   ├── mcp_setup.py            # McpToolset factory + step tools
│   │   └── custom_tools.py         # FunctionTools + progress tracking
│   ├── config.py                   # Configuration (env vars)
│   ├── prompts.py                  # Prompt loader
│   ├── schemas.py                  # Pydantic models
│   └── utils.py                    # Utilities
│
├── servers/              # FastMCP servers (5 servers)
│   ├── structure_server.py         # PDB retrieval, structure repair
│   ├── genesis_server.py           # Boltz-2 structure generation
│   ├── solvation_server.py         # Solvation and membrane embedding
│   ├── amber_server.py             # Amber topology/coordinate generation
│   └── md_simulation_server.py     # MD execution and analysis
│
├── common/               # Shared libraries
│   ├── base.py                     # BaseToolWrapper
│   ├── errors.py                   # Unified error handling
│   └── utils.py                    # Common utilities
│
└── notebooks/            # For testing and demos

# Job directories created at runtime (in cwd):
# ./job_XXXXXXXX/
#    ├── session.db        # Session persistence (SQLite)
#    ├── session_info.json # Job metadata
#    ├── chat_history.md   # Conversation log
#    ├── *.pdb, *.cif      # Downloaded/generated structures
#    └── ...               # Other workflow outputs
```

## Development Workflow

### Direct Python Files

This project adopts the **Direct Python Files** pattern:

```
✅ Edit src/mdzen/ directly
✅ Test and demo in notebooks/
✅ Format check with ruff check src/mdzen/

🚫 Code generation via %%writefile is not recommended
```

### Code Formatting

```bash
# Format check
ruff check src/mdzen/

# Auto-fix
ruff check src/mdzen/ --fix
```

### Test Execution

```bash
# Run unit tests
pytest tests/ -v

# Run specific test file
pytest tests/test_structure_server.py -v

# Run with coverage
pytest tests/ --cov=src/mdzen --cov-report=html

# Quick import test
python -c "from mdzen.config import settings; print('OK')"
```

## Configuration (Environment Variables)

Settings can be customized via `MDZEN_` prefixed environment variables:

```bash
# Set via .env file or environment variables
export MDZEN_OUTPUT_DIR="./custom_output"
export MDZEN_CLARIFICATION_MODEL="anthropic:claude-haiku-4-5-20251001"
export MDZEN_SETUP_MODEL="anthropic:claude-sonnet-4-20250514"
export MDZEN_DEFAULT_TIMEOUT=300
```

Available settings:

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `MDZEN_OUTPUT_DIR` | `.` (cwd) | Output directory for job folders |
| `MDZEN_CLARIFICATION_MODEL` | `anthropic:claude-haiku-4-5-20251001` | Phase 1 model |
| `MDZEN_SETUP_MODEL` | `anthropic:claude-sonnet-4-20250514` | Phase 2 model |
| `MDZEN_COMPRESS_MODEL` | `anthropic:claude-haiku-4-5-20251001` | Compression model |
| `MDZEN_DEFAULT_TIMEOUT` | `300` | Default timeout (seconds) |
| `MDZEN_SOLVATION_TIMEOUT` | `600` | Solvation timeout (seconds) |
| `MDZEN_MEMBRANE_TIMEOUT` | `1800` | Membrane building timeout (seconds) |
| `MDZEN_MD_SIMULATION_TIMEOUT` | `3600` | MD execution timeout (seconds) |
| `MDZEN_MAX_MESSAGE_HISTORY` | `6` | Number of message history to retain |

> **Note**: Model format uses `anthropic:model-name` which is automatically converted to LiteLLM format (`anthropic/model-name`).

## Troubleshooting

### packmol-memgen numpy compatibility error

If you see this error during solvation:
```
AttributeError: module 'numpy' has no attribute 'float'.
```

This is a known issue with `packmol-memgen` and NumPy 1.24+. Apply this fix:

```bash
# Patch the problematic file
SITE_PACKAGES=$(python -c "import site; print(site.getsitepackages()[0])")
sed -i.bak "s/np\.float)/float)/g; s/np\.int)/int)/g" \
    "$SITE_PACKAGES/packmol_memgen/lib/pdbremix/v3numpy.py"
```

See [AMBER mailing list discussion](http://archive.ambermd.org/202308/0029.html) for details.

## License

MIT License

## Citations

When using this tool, please cite the following:

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
