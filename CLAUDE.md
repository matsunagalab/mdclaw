# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**MDZen** (MD + 膳/禅) is an AI-powered system for generating molecular dynamics (MD) input files optimized for the Amber/OpenMM ecosystem. It combines:
- **Google Agent Development Kit (ADK)** for multi-phase workflow orchestration
- **FastMCP** server integration for specialized MD tools
- **Boltz-2** for AI-driven structure prediction
- **AmberTools** for topology generation and parameterization
- **OpenMM** for production-ready MD simulations

## Development Commands

### Environment Setup

```bash
# Create conda environment with scientific packages
conda create -n mdzen python=3.11
conda activate mdzen
conda install -c conda-forge openmm rdkit mdanalysis biopython pandas numpy scipy openblas pdbfixer
conda install -c conda-forge ambertools packmol smina

# Install project in editable mode
git clone https://github.com/matsunagalab/mdzen.git
cd mdzen
pip install -e .

# Optional: Boltz-2 (for Phase 2-3)
pip install 'boltz[cuda]' --no-deps
pip install torch hydra-core pytorch-lightning einops einx mashumaro modelcif wandb
```

### CLI Usage (main.py)

```bash
# Interactive mode (recommended)
python main.py run "Setup MD for PDB 1AKE"

# Non-interactive mode (like claude -p)
python main.py run -p "Setup MD for PDB 1AKE in explicit water, 1 ns at 300K"

# Resume session (like claude -r)
python main.py run -r job_abc12345

# Show available MCP servers
python main.py list-servers

# Show help
python main.py --help
python main.py info
```

### Development Workflow

```bash
# Test individual MCP servers with MCP Inspector
mcp dev servers/structure_server.py
mcp dev servers/genesis_server.py
mcp dev servers/solvation_server.py
mcp dev servers/amber_server.py
mcp dev servers/md_simulation_server.py

# Code quality checks
ruff check src/mdzen/       # Format checking
ruff check src/mdzen/ --fix # Auto-fix format issues
pytest tests/                     # Run tests
```

## Architecture

### 3-Phase Workflow

The system follows a **3-phase workflow pattern** using Google ADK's SequentialAgent:

1. **Phase 1: Clarification**
   - Pattern: **LlmAgent** with MCP tools
   - Tools: `download_structure`, `get_alphafold_structure`, `inspect_molecules`, `search_proteins`, `get_protein_info` (from research_server)
   - Outputs: Structured `SimulationBrief` via `generate_simulation_brief` FunctionTool
   - Implementation: `src/mdzen/agents/clarification_agent.py`

2. **Phase 2: Setup**
   - Pattern: **LlmAgent** with all MCP tools
   - Fixed workflow: prepare_complex → solvate → build_topology → run_simulation
   - Tracks progress via `session.state["completed_steps"]`
   - Implementation: `src/mdzen/agents/setup_agent.py`

3. **Phase 3: Validation**
   - QC checks, format validation, report generation
   - Implementation: `src/mdzen/agents/validation_agent.py`

### SequentialAgent Orchestration

```python
from google.adk.agents import SequentialAgent, LlmAgent

full_agent = SequentialAgent(
    name="full_md_agent",
    sub_agents=[
        create_clarification_agent(),  # output_key="simulation_brief"
        create_setup_agent(),           # reads session.state
        create_validation_agent(),      # output_key="validation_result"
    ],
)
```

### Session State Management

State flows through `session.state` between agents:

```python
session.state["simulation_brief"]   # Phase 1 output
session.state["completed_steps"]    # Phase 2 progress tracking
session.state["outputs"]            # Generated file paths
session.state["validation_result"]  # Phase 3 output
```

### Directory Structure

```
mdzen/
├── main.py                # CLI entry point (Typer)
│
├── src/mdzen/        # Google ADK implementation
│   ├── agents/
│   │   ├── clarification_agent.py  # Phase 1 LlmAgent
│   │   ├── setup_agent.py          # Phase 2 LlmAgent
│   │   ├── validation_agent.py     # Phase 3 LlmAgent
│   │   └── full_agent.py           # SequentialAgent
│   ├── cli/
│   │   └── commands.py             # run, info commands
│   ├── prompts/                    # Prompt templates (external files)
│   │   ├── clarification.md        # Phase 1 instruction
│   │   ├── setup.md                # Phase 2 instruction (full)
│   │   ├── validation.md           # Phase 3 instruction
│   │   └── steps/                  # Step-specific prompts
│   │       ├── prepare_complex.md
│   │       ├── solvate.md
│   │       ├── build_topology.md
│   │       └── run_simulation.md
│   ├── state/
│   │   └── session_manager.py      # SessionService setup
│   ├── tools/
│   │   ├── mcp_setup.py            # McpToolset factory
│   │   ├── custom_tools.py         # FunctionTools + progress tracking
│   │   └── state_wrappers.py       # ToolContext state extraction (direct functions)
│   ├── config.py                   # Settings + LiteLLM conversion
│   ├── prompts.py                  # Prompt loader + get_step_instruction()
│   ├── schemas.py                  # Pydantic models
│   ├── workflow.py                 # Workflow step definitions (single source of truth)
│   └── utils.py                    # Utilities
│
├── servers/               # FastMCP servers (7 servers)
│   ├── research_server.py          # PDB/AlphaFold/UniProt retrieval, structure inspection
│   ├── literature_server.py        # PubMed literature search via NCBI E-utilities
│   ├── structure_server.py         # Structure cleaning, ligand parameterization
│   ├── genesis_server.py           # Boltz-2 structure prediction
│   ├── solvation_server.py         # Solvent/membrane embedding
│   ├── amber_server.py             # Amber topology generation
│   └── md_simulation_server.py     # OpenMM MD execution & analysis
│
├── common/                # Shared utilities
│   ├── base.py            # BaseToolWrapper (delegates timeouts to config.py)
│   ├── errors.py          # Error handling
│   └── utils.py           # Common utilities (generate_job_id, etc.)
│
└── notebooks/             # Testing and demos

# Job directories created at runtime:
# ./job_XXXXXXXX/          # All outputs for a single job
#    ├── session.db        # Session persistence (SQLite)
#    ├── session_info.json # Job metadata
#    ├── chat_history.md   # Conversation log
#    ├── *.pdb, *.cif      # Downloaded/generated structures
#    └── ...               # Other workflow outputs
```

### FastMCP Servers

The system uses **7 independent FastMCP servers**, each providing specialized MD preparation tools:

1. **research_server.py** - External database integration (mirrors Augmented-Nature MCP servers)
   - `download_structure()`: Download from RCSB PDB
   - `get_alphafold_structure()`: Get predicted structure from AlphaFold DB
   - `inspect_molecules()`: Analyze structure, classify chains (gemmi-based)
   - `search_proteins()`: Search UniProt database
   - `get_protein_info()`: Get protein details from UniProt

2. **literature_server.py** - PubMed literature search via NCBI E-utilities
   - `pubmed_search()`: Search PubMed with Boolean queries
   - `pubmed_fetch()`: Get detailed article info with abstracts

3. **structure_server.py** - Structure preparation and parameterization
   - `clean_protein()`: PDBFixer + pdb2pqr/propka (pH-aware protonation)
   - `clean_ligand()`: Template matching + geometry optimization
   - `run_antechamber_robust()`: GAFF2 + AM1-BCC parameterization
   - `prepare_complex()`: All-in-one preparation pipeline

4. **genesis_server.py** - AI structure prediction
   - Boltz-2 integration for FASTA → PDB conversion

5. **solvation_server.py** - Solvation and membrane setup
   - packmol-memgen for solvent/membrane embedding

6. **amber_server.py** - Topology generation
   - tleap for prmtop and inpcrd file generation

7. **md_simulation_server.py** - MD execution
   - OpenMM simulation and trajectory analysis

Each server is **independently testable** using `mcp dev servers/<server_name>.py`.

## Key Technical Patterns

### LlmAgent with MCP Tools

```python
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

# Create McpToolset for MCP server
toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=python_exe,
            args=[server_path],
        ),
        timeout=300,
    )
)

# Create agent with tools
agent = LlmAgent(
    name="setup_agent",
    model=LiteLlm(model="anthropic/claude-sonnet-4-20250514"),
    instruction=SETUP_INSTRUCTION,
    tools=[toolset, custom_tools],
    output_key="setup_result",
)
```

### Runner Execution

```python
from google.adk.runners import Runner
from google.genai import types

runner = Runner(
    app_name="mdzen",
    agent=agent,
    session_service=session_service,
)

user_message = types.Content(
    role="user",
    parts=[types.Part(text=request)],
)

async for event in runner.run_async(
    user_id="default",
    session_id=session_id,
    new_message=user_message,
):
    if event.is_final_response():
        print(event.content)
```

### FunctionTool Definition

```python
from google.adk.tools import FunctionTool

def generate_simulation_brief(
    pdb_id: str | None = None,
    ligand_smiles: str | None = None,
    # ...
) -> dict:
    """Generate SimulationBrief from gathered requirements.

    Args:
        pdb_id: PDB ID to fetch
        ligand_smiles: Ligand SMILES string
    """
    return {"pdb_id": pdb_id, "ligand_smiles": ligand_smiles, ...}

brief_tool = FunctionTool(generate_simulation_brief)
```

### Workflow Step Tracking (Phase 2)

All workflow step definitions are centralized in `src/mdzen/workflow.py`:

```python
from mdzen.workflow import SETUP_STEPS, STEP_CONFIG

# Ordered list of workflow steps
SETUP_STEPS = ["prepare_complex", "solvate", "build_topology", "run_simulation"]

# Centralized configuration for each step (single source of truth)
STEP_CONFIG = {
    "prepare_complex": {
        "tool": "prepare_complex",
        "inputs": "Requires: PDB ID or structure file",
        "servers": ["research", "structure", "genesis"],
        "allowed_tools": ["prepare_complex", "download_structure", "get_alphafold_structure", "predict_structure"],
        "estimate": "1-5 minutes",
    },
    # ... other steps
}

# Access step properties directly from STEP_CONFIG
tool_name = STEP_CONFIG["prepare_complex"]["tool"]
servers = STEP_CONFIG["prepare_complex"]["servers"]
```

**Note:** Always import workflow definitions (`SETUP_STEPS`, `STEP_CONFIG`, `validate_step_prerequisites`, etc.) directly from `mdzen.workflow`.

### Session State Serialization

ADK serializes state values as JSON strings. Use safe helpers:

```python
from mdzen.utils import safe_dict, safe_list

# Handles both dict and JSON string inputs
outputs = safe_dict(session.state.get("outputs", {}))
completed = safe_list(session.state.get("completed_steps", []))
```

### Timeout Configuration

Timeout configuration is centralized in `src/mdzen/config.py`:

```python
from mdzen.config import get_timeout

timeout = get_timeout("solvation")  # MDZEN_SOLVATION_TIMEOUT (600s)
timeout = get_timeout("default")    # MDZEN_DEFAULT_TIMEOUT (300s)
timeout = get_timeout("membrane")   # MDZEN_MEMBRANE_TIMEOUT (1800s)
```

### Step-Specific Tool Loading

The `get_step_tools()` function loads only the MCP servers needed for each step:

```python
from mdzen.tools.mcp_setup import get_step_tools

# Server mappings are defined in STEP_CONFIG["step"]["servers"]
# "prepare_complex": ["structure", "genesis"]
# "solvate": ["solvation"]
# "build_topology": ["amber"]
# "run_simulation": ["md_simulation"]

# Get toolsets for a specific step
toolsets = get_step_tools("solvate")  # Returns only solvation_server toolset
```

**Benefits:**
- Reduced token usage (1-2 servers per step vs all 5)
- Clearer tool selection for LLM (fewer options = better choices)
- Easier debugging (step-specific agents with focused prompts)

### Prompt Management

Prompts are stored as **external Markdown files** in `src/mdzen/prompts/`:

```
prompts/
├── clarification.md   # Phase 1: Requirements gathering
├── setup.md           # Phase 2: MD workflow execution (full)
├── validation.md      # Phase 3: QC and report generation
└── steps/             # Step-specific prompts (Best Practice #3)
    ├── prepare_complex.md
    ├── solvate.md
    ├── build_topology.md
    └── run_simulation.md
```

**Benefits of external prompt files:**
- Non-engineers can edit prompts without touching Python code
- Prompt changes are clearly visible in git diffs
- Easy to A/B test different prompts

**Usage:**

```python
from mdzen.prompts import get_clarification_instruction, get_setup_instruction

# Load and format prompt with today's date
instruction = get_clarification_instruction()

# Or get raw template
from mdzen.prompts import get_raw_prompt
template = get_raw_prompt("setup")  # Returns contents of setup.md

# Step-specific prompts (for Best Practice #3)
from mdzen.prompts import get_step_instruction
step_prompt = get_step_instruction("prepare_complex")  # Falls back to setup.md if not found
```

**Template variables:**
- `{date}` - Current date (auto-substituted)

## Code Quality Standards

### Format Configuration

- **Line length**: 100 characters (ruff/black)
- **Python target**: 3.11+
- **Pydantic version**: 2.12+
- **Docstring format**: D212 (summary on first line)
- **Import order**: stdlib → third-party → local

### Quality Checklist

After editing Python files in `src/mdzen/`:
1. Run `ruff check src/mdzen/`
2. Fix any linting errors directly in the Python file
3. Test with CLI: `python main.py run "Setup MD for PDB 1AKE"`
4. Verify imports work correctly

## Configuration

### Environment Variables

All settings can be configured via `MDZEN_` prefixed environment variables:

```bash
# .env or shell exports
# Output directory defaults to current working directory (.)
# Set explicitly if you want a different location:
export MDZEN_OUTPUT_DIR="./outputs"
export MDZEN_CLARIFICATION_MODEL="anthropic:claude-haiku-4-5-20251001"
export MDZEN_SETUP_MODEL="anthropic:claude-sonnet-4-20250514"
export MDZEN_DEFAULT_TIMEOUT=300
export MDZEN_SOLVATION_TIMEOUT=600
export MDZEN_MEMBRANE_TIMEOUT=1800
export MDZEN_MD_SIMULATION_TIMEOUT=3600
export MDZEN_LOG_LEVEL=WARNING  # DEBUG, INFO, WARNING, ERROR
```

Note: Model format uses `anthropic:model-name` which is automatically converted to LiteLLM format (`anthropic/model-name`).

### API Keys

```bash
ANTHROPIC_API_KEY=...
```

### Model Configuration

Models are **auto-detected** based on available API keys (in order of priority):

| API Key | Clarification | Setup |
|---------|---------------|-------|
| `ANTHROPIC_API_KEY` | claude-haiku | claude-sonnet |
| `OPENAI_API_KEY` | gpt-4o-mini | gpt-4o-mini |
| `GOOGLE_API_KEY` | gemini-2.0-flash | gemini-2.0-flash |

**Override via CLI:**
```bash
python main.py run -m gpt-4o "Setup MD for PDB 1AKE"
python main.py run -m claude-sonnet "Setup MD for PDB 1AKE"
```

**Override via environment:**
```bash
export MDZEN_CLARIFICATION_MODEL=openai:gpt-4o
export MDZEN_SETUP_MODEL=anthropic:claude-opus-4-5-20251101
```

**Model aliases:** `gpt-4o`, `gpt-4o-mini`, `claude-opus`, `claude-sonnet`, `claude-haiku`, `gemini-flash`, `gemini-pro`

### Logging Configuration

Log verbosity is controlled via `MDZEN_LOG_LEVEL` environment variable.

**Default behavior** (no env var set):
- Server loggers (structure_server, amber_server, etc.): **INFO** - shows tool calls and operations
- Third-party loggers (mcp.server, httpx, etc.): **WARNING** - suppressed to reduce noise

**Example output** (default INFO level for servers):
```
__main__ - INFO - Fetching 1AKE from PDB
__main__ - INFO - Downloaded 1AKE to outputs/session_xxx/1ake.cif
__main__ - INFO - Cleaning protein structure: outputs/session_xxx/1ake_A.pdb
__main__ - INFO - Successfully cleaned protein structure
__main__ - INFO - Running tleap...
__main__ - INFO - Successfully created Amber files
```

**Log format**: `%(name)s - %(levelname)s - %(message)s` (no timestamp for cleaner output)

```bash
# Normal operation (default) - shows tool operations, suppresses noise
python main.py run "Setup MD for PDB 1AKE"

# Debug mode - verbose output for troubleshooting
MDZEN_LOG_LEVEL=DEBUG python main.py run "Setup MD for PDB 1AKE"

# Quiet mode - warnings and errors only
MDZEN_LOG_LEVEL=WARNING python main.py run "Setup MD for PDB 1AKE"
```

The logging system automatically suppresses noisy third-party loggers (mcp.server, httpx, httpcore, openai, anthropic) while keeping useful tool operation logs visible.

## Known Issues and Fixes

### packmol-memgen numpy compatibility (NumPy 1.24+)

**Issue**: When running solvation with `packmol-memgen`, you may see:
```
AttributeError: module 'numpy' has no attribute 'float'.
`np.float` was a deprecated alias for the builtin `float`.
```

**Cause**: `packmol-memgen` uses deprecated `np.float` which was removed in NumPy 1.24+.

**Fix**: Patch the v3numpy.py file:
```bash
# Find and patch the file
SITE_PACKAGES=$(python -c "import site; print(site.getsitepackages()[0])")
sed -i.bak "s/np\.float)/float)/g; s/np\.int)/int)/g" \
    "$SITE_PACKAGES/packmol_memgen/lib/pdbremix/v3numpy.py"
```

**Reference**: [AMBER mailing list (Aug 2023)](http://archive.ambermd.org/202308/0029.html)

### MCP Transport in Google Colab

**Issue**: In Google Colab, MCP stdio transport fails with:
```
io.UnsupportedOperation: fileno
```

**Cause**: Colab's `ipykernel.iostream` doesn't support `fileno()` which is required by MCP's stdio transport for subprocess communication.

**Solution**: Use **Streamable HTTP transport** instead of stdio in Colab:

1. **Start MCP servers with HTTP transport** (in Setup cell):
```python
subprocess.Popen([
    "/usr/local/bin/python", f"/content/mdzen/servers/{server}",
    "--http", "--port", str(port)
], env={**os.environ, "PYTHONPATH": "/content/mdzen/src"})
```

2. **Create agents with `transport="http"`**:
```python
agent, mcp_tools = create_clarification_agent(transport="http")
agent, mcp_tools = create_setup_agent(transport="http")
```

**Transport modes**:
- `stdio` (default): Subprocess-based, works locally but **not in Colab**
- `http`: Streamable HTTP via `/mcp` endpoint, **required for Colab**

### FastMCP 2.x API for HTTP Transport

**Issue**: Servers fail with `AttributeError: 'FastMCP' object has no attribute '_deprecated_settings'`

**Cause**: The `_deprecated_settings` API was removed in FastMCP 2.x.

**Fix**: Pass `host` and `port` directly to `mcp.run()`:
```python
# OLD (broken in FastMCP 2.x)
mcp._deprecated_settings.host = "0.0.0.0"
mcp._deprecated_settings.port = args.port
mcp.run(transport="http")

# NEW (correct FastMCP 2.x API)
mcp.run(transport="http", host="0.0.0.0", port=args.port)
```

**Note**: Both Colab and local development now use `fastmcp>=2.0.0` (updated in pyproject.toml). The opentelemetry version conflict warnings with google-adk are harmless.

### FastMCP 2.x: Calling decorated functions internally

**Issue**: When calling `@mcp.tool()` decorated functions from within the same file (e.g., in `prepare_complex`), you get:
```
TypeError: 'FunctionTool' object is not callable
```

**Cause**: In FastMCP 2.x, `@mcp.tool()` returns a `FunctionTool` object that wraps the original function. The object is not directly callable.

**Fix**: Access the underlying function via `.fn`:
```python
# WRONG - FunctionTool object is not callable
result = split_molecules(file, output_dir=out_dir)

# CORRECT - call the underlying function
result = split_molecules.fn(file, output_dir=out_dir)
```

**Affected functions in `structure_server.py`:**
- `split_molecules.fn()`
- `clean_protein.fn()`
- `clean_ligand.fn()`
- `run_antechamber_robust.fn()`
- `merge_structures.fn()`

These are called internally by `prepare_complex` which orchestrates the full structure preparation workflow.

### ADK async issues in Colab/Jupyter

**Issue**: `anyio.WouldBlock` or `asyncio.CancelledError` when using MCP tools in notebooks.

**Cause**: Event loop conflicts between Jupyter's event loop and ADK's async handling.

**Workarounds**:
1. Use `await` directly in notebook cells (Colab has a running event loop)
2. Apply `nest_asyncio.apply()` at startup (already done in Setup cell)
3. Use HTTP transport instead of stdio (avoids subprocess async issues)

**References**:
- [ADK Issue #755](https://github.com/google/adk-python/issues/755) - Event loop conflicts
- [ADK Issue #1267](https://github.com/google/adk-python/issues/1267) - CancelledError with StreamableHTTPConnectionParams

### MCP stdio_client async generator cleanup errors (Fixed)

**Issue**: After a successful run, you may see errors like:
```
an error occurred during closing of asynchronous generator <async_generator object stdio_client at 0x...>
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
```

**Cause**: MCP's `stdio_client` uses async generators that are cleaned up by Python's garbage collector at shutdown. The cleanup happens in a different task context than the generators were created in, causing anyio's cancel scope to fail.

**Fix**: The fix is implemented in `main.py`'s `_run_with_suppressed_cleanup()` function:
1. Disable Python's async generator finalization hooks with `sys.set_asyncgen_hooks(firstiter=None, finalizer=None)`
2. Override `sys.unraisablehook` to ignore MCP-related errors
3. Set a custom exception handler on the event loop
4. Redirect stderr to `/dev/null` during cleanup
5. Use `os._exit(0)` to skip remaining cleanup

**Caveats**:
- `os._exit(0)` skips normal cleanup (atexit handlers, file buffer flushing)
- Errors during cleanup phase are completely hidden (including non-MCP errors)
- Debugging shutdown issues requires temporarily disabling this suppression
- This is a workaround, not a root fix (MCP/anyio library issue)

**Note**: These errors are harmless - they only occur during process shutdown and don't affect the actual workflow execution. All important processing completes before the cleanup phase begins

### Protein Protonation Workflow (clean_protein)

The `clean_protein()` function uses a two-tier protonation strategy:

**Primary: pdb2pqr + propka (pH-aware)**
```
PDBFixer (structure repair, no hydrogens)
    ↓
pdb2pqr --ff AMBER --titration-state-method propka --with-ph <pH>
    ↓
.amber.pdb (tleap compatible)
```

**Fallback: pdb4amber --reduce (geometry-based)**
- Used when pdb2pqr is unavailable
- Warning issued: "pH X.X protonation not applied"
- Histidine states assigned by geometry only

**Benefits of pdb2pqr + propka:**
- pH-dependent histidine assignment (HID/HIE/HIP based on pKa)
- Proper Amber hydrogen naming (H1, H2, H3 for N-termini)
- Handles internal chain breaks (NALA, NVAL, NGLN residues)

**Output fields:**
```python
result = {
    "protonation_method": "pdb2pqr+propka",  # or "pdb4amber+reduce"
    "histidine_states": {"A:126": "HIE", "A:134": "HID", ...},
    # ... other fields
}
```

**Dependencies:** `pdb2pqr>=3.1.0`, `propka>=3.5.0` (in pyproject.toml)
