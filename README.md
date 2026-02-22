# MDClaw: Your personal MD assistant

**From PDB ID to Production-Ready Simulation - Automated**

MDClaw transforms any PDB structure, FASTA sequence, or ligand-SMILES into a production-ready Amber/OpenMM simulation setup through AI-powered tools and domain knowledge.

**Architecture**: CLI tools + Skills (domain knowledge prompts) - works with Claude Code, Cursor, Windsurf, or any AI coding assistant.

## Installation as Claude Code Plugin

Install MDClaw skills directly via the Claude Code plugin marketplace:

```
/plugin marketplace add matsunagalab/mdclaw
/plugin install mdclaw@mdclaw
```

After installation, the following skills become available:
- `/mdclaw:md-prepare` — MD simulation preparation
- `/mdclaw:md-run` — Production MD execution
- `/mdclaw:md-analyze` — Trajectory analysis
- `/mdclaw:hpc-run` — HPC/SLURM job submission and management

### Tool Setup

To use CLI tools (structure retrieval, parameterization, simulation execution), set up the environment:

```bash
conda env create -f environment.yml
conda activate mdclaw
pip install -e .
```

## Quick Start

### 1. Install

#### Local / PC Cluster (Recommended)

```bash
git clone https://github.com/matsunagalab/mdclaw.git
cd mdclaw
conda env create -f environment.yml
conda activate mdclaw
```

#### HPC (Singularity Container)

Pull the pre-built Docker image and convert to Singularity SIF:

```bash
# On a machine with Singularity installed
singularity pull mdclaw.sif docker://ghcr.io/matsunagalab/mdclaw:latest

# Transfer to your cluster
scp mdclaw.sif user@cluster:/opt/containers/

# Run with GPU
singularity exec --nv mdclaw.sif mdclaw --list
```

Or build the Docker image locally and convert:

```bash
# Build Docker image
docker build -f container/Dockerfile -t mdclaw:latest .

# Convert to Singularity SIF
singularity pull mdclaw.sif docker-daemon://mdclaw:latest
```

Configure MDClaw to use the container for SLURM jobs:

```bash
mdclaw configure_container \
  --image /opt/containers/mdclaw.sif \
  --bind-paths /scratch /data \
  --extra-flags "--nv"
```

After configuration, `submit_job` automatically wraps commands with `singularity exec`.

#### pip Only (No AmberTools/OpenMM)

```bash
git clone https://github.com/matsunagalab/mdclaw.git
cd mdclaw && pip install -e .
```

Only the research, literature, and genesis tools will work. The conda environment is required for structure preparation and MD execution.

### 2. Use with Claude Code

```bash
# Start Claude Code in the mdclaw directory
claude

# Run MD preparation (interactive)
> /md-prepare PDB 1AKE

# Run MD preparation (autonomous - all defaults)
> /md-prepare PDB 1AKE, chain A, no ligands, run end-to-end with defaults

# Production MD run
> /md-run resume job_XXXXXXXX, extend to 10 ns

# Analyze trajectory
> /md-analyze job_XXXXXXXX

# Submit MD simulation to HPC cluster via SLURM
> /hpc-run submit 100ns MD simulation of 1AKE to GPU partition

# Check job status / recover from errors
> /hpc-run check job 12345 and restart if timed out
```

### 3. CLI Usage

```bash
# List all tools
mdclaw --list

# Run a tool (output is JSON on stdout)
mdclaw download_structure --pdb-id 1AKE --format pdb
mdclaw inspect_molecules --structure-file 1AKE.pdb
mdclaw solvate_structure --pdb-file merged.pdb --dist 15.0 --salt --saltcon 0.15
```

### 4. HPC/SLURM Usage

MDClaw provides generic SLURM tools for submitting and managing batch jobs on HPC clusters.

```bash
# Discover cluster partitions, GPUs, and time limits
mdclaw inspect_cluster

# Submit a job (command string or script file)
mdclaw submit_job \
  --script "mdclaw run_md_simulation --prmtop-file sys.parm7 --inpcrd-file sys.rst7 --platform CUDA --hmr --timestep-fs 4.0" \
  --partition gpu --gpus 1 --time-limit "24:00:00" --memory "64G"

# Submit an existing script file
mdclaw submit_job --script run_md.sh --partition gpu --gpus 1 --time-limit "24:00:00"

# Check job status
mdclaw check_job --job-id 12345

# List your jobs
mdclaw list_jobs

# Read job logs (stderr/stdout)
mdclaw check_job_log --job-id 12345 --log-type stderr --tail-lines 100

# Cancel a job
mdclaw cancel_job --job-id 12345
```

#### Resource Policy

On shared clusters, set resource limits to avoid overuse:

```bash
# Set allowed partitions and resource limits
mdclaw set_policy \
  --allowed-partitions gpu cpu-small \
  --max-gpus-per-job 2 \
  --max-cpus-per-task 16 \
  --max-nodes 1 \
  --max-time-limit "24:00:00" \
  --max-memory "128G" \
  --default-account myproject \
  --default-qos normal

# View current policy
mdclaw show_policy
```

Policy is stored in the `policy` section of `.mdclaw_cluster.json`.
When set, `submit_job` rejects requests that exceed the limits.
All fields are optional — omitted fields have no restriction.

| Field | Example | Description |
|-------|---------|-------------|
| `--allowed-partitions` | `gpu cpu-small` | Only these partitions can be used |
| `--max-gpus-per-job` | `2` | Maximum GPUs per job |
| `--max-cpus-per-task` | `16` | Maximum CPUs per task |
| `--max-nodes` | `1` | Maximum nodes per job |
| `--max-time-limit` | `"24:00:00"` | Maximum wall time (HH:MM:SS or D-HH:MM:SS) |
| `--max-memory` | `"128G"` | Maximum memory per node |
| `--default-account` | `myproject` | Default SLURM account |
| `--default-qos` | `normal` | Default quality of service |
| `--default-partition` | `gpu` | Default partition |

The SLURM tools are workload-agnostic: use them for MD simulations, Boltz-2 structure predictions, or any other batch computation. The `/hpc-run` skill provides domain-specific guidance for resource estimation, error recovery, and checkpoint restarts.

## Architecture

```
skills/                    # Domain knowledge (platform-agnostic .md)
  md-prepare/SKILL.md      # Structure -> Solvation -> Topology -> Quick MD
  md-run/SKILL.md           # Production MD runs
  md-analyze/SKILL.md       # Trajectory analysis
  hpc-run/SKILL.md          # HPC/SLURM job management

.claude/commands/           # Claude Code slash commands
  md-prepare.md             # /md-prepare
  md-run.md                 # /md-run
  md-analyze.md             # /md-analyze
  hpc-run.md                # /hpc-run

servers/                    # All Python code consolidated here
  __init__.py               # __version__ + package marker
  _common.py                # Shared utilities (logging, tool wrappers, errors, timeouts)
  _registry.py              # Tool registry (SERVER_REGISTRY dict)
  _cli.py                   # CLI entry point (mdclaw)
  research_server.py        # PDB/AlphaFold/UniProt retrieval
  structure_server.py       # Structure cleaning & parameterization
  genesis_server.py         # Boltz-2 structure prediction
  solvation_server.py       # Water box & membrane embedding
  amber_server.py           # Amber topology generation
  md_simulation_server.py   # OpenMM MD execution
  literature_server.py      # PubMed search
  metal_server.py           # Metal ion parameterization
  slurm_server.py           # SLURM job submission & management

tests/                      # 4-level test suite
  conftest.py               # Shared fixtures
  test_mcp_server.py        # Level 1: Unit tests
  test_server_smoke.py      # Level 2: Server smoke tests
  test_pipeline_1ake.py     # Level 3: Full 1AKE pipeline
  manual_checklist.md       # Level 4: Manual Claude Code tests
```

## Tools

| Module | Tools | Description |
|--------|-------|-------------|
| research | `download_structure`, `get_alphafold_structure`, `inspect_molecules`, `search_proteins` | Structure retrieval and inspection |
| structure | `prepare_complex`, `clean_protein`, `clean_ligand`, `split_molecules` | Structure cleaning and preparation |
| genesis | `boltz2_protein_from_seq`, `rdkit_validate_smiles` | AI structure prediction |
| solvation | `solvate_structure`, `embed_in_membrane` | Solvent/membrane setup |
| amber | `build_amber_system` | Amber topology generation |
| md_simulation | `run_md_simulation` | OpenMM MD execution |
| literature | `pubmed_search`, `pubmed_fetch` | Literature search |
| metal | `parameterize_metal_ion`, `detect_metal_ions` | Metal ion handling |
| slurm | `inspect_cluster`, `submit_job`, `check_job`, `list_jobs`, `cancel_job`, `check_job_log`, `set_policy`, `show_policy`, `configure_container` | SLURM job management |

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
6. Update `CLAUDE.md` server section with the new tool signature

### Adding a New Server

1. Create `servers/new_server.py` with tool functions and a `TOOLS` dict
2. Register in `servers/_registry.py` (`SERVER_REGISTRY`)
3. Add to `servers/__init__.py` `__all__`
4. Add smoke tests in `tests/test_server_smoke.py`
5. Update `CLAUDE.md` architecture diagram and server section

### Pre-commit Checklist

```bash
ruff check servers/                                      # lint
pytest tests/test_mcp_server.py tests/test_cli.py -v     # unit tests
pytest tests/test_server_smoke.py -v                      # smoke tests (if applicable)
```

## Configuration

Settings via `MDCLAW_` environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MDCLAW_OUTPUT_DIR` | `.` | Output directory |
| `MDCLAW_DEFAULT_TIMEOUT` | `300` | Default timeout (seconds) |
| `MDCLAW_SOLVATION_TIMEOUT` | `600` | Solvation timeout |
| `MDCLAW_MEMBRANE_TIMEOUT` | `7200` | Membrane building timeout |
| `MDCLAW_MD_SIMULATION_TIMEOUT` | `3600` | MD execution timeout |
| `MDCLAW_SLURM_TIMEOUT` | `120` | SLURM command timeout |
| `MDCLAW_MODULE_LOADS` | _(unset)_ | HPC module load commands (e.g., `cuda/12.0 amber/24`) |
| `MDCLAW_MODULE_INIT` | `/etc/profile.d/modules.sh` | Module system init script path |

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
