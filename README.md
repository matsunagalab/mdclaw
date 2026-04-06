# MDClaw: Your personal MD assistant

**From PDB ID to Production-Ready Simulation - Automated**

MDClaw transforms any PDB structure, FASTA sequence, or ligand-SMILES into a production-ready Amber/OpenMM simulation setup through AI-powered tools and domain knowledge.

**Architecture**: CLI tools + Skills (domain knowledge prompts) — works with Claude Code, Cursor, Windsurf, or any AI coding assistant.

---

# For Users

## Installation (Claude Code Plugin)

Install MDClaw as a Claude Code plugin:

```
/plugin marketplace add matsunagalab/mdclaw
/plugin install mdclaw@mdclaw
```

On first session start, the container (~4.6 GB) is automatically downloaded.

### Container Runtime Requirements

You need **one** of the following runtimes on the host:

| Runtime | Best for | Install |
|---------|---------|---------|
| **Singularity / Apptainer** | HPC clusters | [Singularity](https://docs.sylabs.io/guides/latest/user-guide/quick_start.html) / [Apptainer](https://apptainer.org/docs/user/main/quick_start.html) |
| **Docker** | macOS, Linux desktop | [Docker Desktop](https://docs.docker.com/get-docker/) |

`bin/mdclaw` auto-detects the available runtime (Singularity first, Docker as fallback). You can force one with:
```bash
export MDCLAW_RUNTIME=docker      # force Docker
export MDCLAW_RUNTIME=singularity  # force Singularity
```

### GPU Requirements

- **NVIDIA driver 530+** (CUDA 12.1 or newer)
- Verified GPUs: RTX 2080 Ti, RTX A6000, A100
- CPU-only mode works without GPU (slower, for testing)

## Usage

After installation, four skills (slash commands) become available:

| Command | Purpose |
|---------|---------|
| `/mdclaw:md-prepare` | Structure acquisition → cleaning → solvation → topology → quick MD |
| `/mdclaw:md-run` | Equilibration (NVT + NPT with CA restraints) + production MD |
| `/mdclaw:md-analyze` | Trajectory analysis (RMSD, RMSF, energy, hydrogen bonds) |
| `/mdclaw:hpc-run` | SLURM job submission, monitoring, error recovery |

### Example Conversations

**Single system, end-to-end:**
```
> /mdclaw:md-prepare 1AKE chain A, no ligands, explicit water, defaults

> /mdclaw:md-run run 10 ns production MD
```

**Multiple targets (batch):**
```
> /mdclaw:md-prepare 1AKE, 4AKE chain A, explicit water, defaults

> /mdclaw:md-run batch_a1b2c3d4, 100 ns on GPU partition
```

**HPC workflow:**
```
> /mdclaw:hpc-run submit 100 ns MD of 1AKE to GPU partition on node gpu01

> /mdclaw:hpc-run check job 12345

> /loop 15m /mdclaw:hpc-run check job 12345 and report when done
```

### Direct CLI Usage

You can also invoke mdclaw directly (outside Claude Code):

```bash
# List all tools
mdclaw --list

# Download a structure
mdclaw download_structure --pdb-id 1AKE --format pdb --output-dir job_1AKE

# Run a quick MD
mdclaw run_production \
  --prmtop-file job_1AKE/topology/system.parm7 \
  --inpcrd-file job_1AKE/topology/system.rst7 \
  --simulation-time-ns 0.1 \
  --output-dir job_1AKE
```

`bin/mdclaw` is auto-added to your PATH by the plugin installer.

## Default MD Parameters

MDClaw uses the following defaults, aligned with OpenMM best practices:

| Parameter | Default | Notes |
|-----------|---------|-------|
| Force field | ff19SB | Paired with OPC water |
| Water model | OPC | 4-point, best accuracy |
| Buffer distance | 15 Å | Prevents periodic image interactions |
| Salt | 0.15 M NaCl | Physiological |
| Temperature | 300 K | |
| Pressure | 1 bar (NPT) | |
| Integrator | LangevinMiddleIntegrator | Friction 1/ps |
| Timestep | **4 fs** | With HMR (hydrogenMass=4 amu) |
| Constraints | HBonds | |
| Nonbonded method | PME (explicit) / NoCutoff (implicit) | |
| Equilibration | NVT (10k steps, 1fs) + NPT (10k steps, 2fs) | CA positional restraints (100 kJ/mol/nm²) |

## HPC / SLURM Usage

MDClaw provides SLURM tools for batch job submission, monitoring, and error recovery:

```bash
# Discover cluster partitions, GPUs, nodes
mdclaw inspect_cluster

# Submit a production MD job
mdclaw submit_job \
  --script "mdclaw run_production --prmtop-file /abs/path/sys.parm7 ..." \
  --partition gpu --nodelist gpu01 --gpus 1 \
  --time-limit "24:00:00" --memory "64G"

# Chain equilibration → production via dependency
EQ_JOB=$(mdclaw submit_job --script "mdclaw run_equilibration ..." --gpus 1 ...)
mdclaw submit_job --script "mdclaw run_production ..." \
  --dependency "afterok:${EQ_JOB}" --gpus 1 ...

# Check job status (updates local JSONL tracker)
mdclaw check_job --job-id 12345

# List all tracked jobs (full history)
mdclaw list_tracked_jobs --sync
```

### Resource Policy

Set resource limits on shared clusters to prevent overuse:

```bash
mdclaw set_policy \
  --allowed-partitions gpu cpu-small \
  --max-gpus-per-job 2 \
  --max-time-limit "24:00:00" \
  --max-memory "128G" \
  --default-account myproject

mdclaw show_policy
```

Policy is stored in `.mdclaw_cluster.json`. `submit_job` rejects requests exceeding the limits.

## Reproducibility

Each job directory contains `progress.json` with:

- **commands**: every CLI invocation with timestamps (auto-recorded)
- **software**: versions of mdclaw, OpenMM, AmberTools, PyTorch
- **system**: PDB ID, chains, atom counts, ligands
- **preparation**: protonation method, histidine states, disulfide bonds, missing residues
- **solvation**: water model, box size, salt concentration
- **forcefield**: protein / water / ligand parameters
- **equilibration / production**: full MD conditions (timestep, HMR, integrator, constraints, etc.)

This provides enough information to regenerate the workflow and to write the Methods section of a paper.

---

# For Developers

## Architecture

```
skills/                     # Domain knowledge (platform-agnostic .md)
  md-prepare/
    SKILL.md                # Router (lightweight)
    setup.md                # Structure acquisition & preparation
    explicit-water.md       # Explicit water solvation workflow
    implicit-water.md       # Implicit solvent workflow
    batch.md                # Multi-target batch processing
  md-run/
    SKILL.md
    explicit-water.md
    implicit-water.md
    batch.md
  md-analyze/
    SKILL.md
    analysis.md
    batch.md
  hpc-run/SKILL.md

.claude/commands/           # Dev-only: local slash commands
  md-prepare.md             #   (not needed when installed as plugin)
  md-run.md
  md-analyze.md
  hpc-run.md

.claude-plugin/             # Claude Code plugin metadata
  plugin.json               # Plugin manifest (version, skills path)
  marketplace.json          # Marketplace entry

bin/
  mdclaw                    # CLI wrapper (Singularity/Docker/native routing)

hooks/
  hooks.json                # SessionStart → auto-download container

scripts/
  setup-container.sh        # Pull SIF (Singularity) or image (Docker)

mdclaw/                     # Python package (CLI tools)
  __init__.py
  _cli.py                   # CLI entry point + command logging
  _registry.py              # Tool registry (SERVER_REGISTRY)
  _common.py                # Shared utilities
  research_server.py        # PDB/AlphaFold retrieval
  structure_server.py       # Cleaning & parameterization
  genesis_server.py         # Boltz-2 structure prediction
  solvation_server.py       # Water box / membrane
  amber_server.py           # Amber topology
  md_simulation_server.py   # OpenMM (equilibration + production)
  literature_server.py      # PubMed search
  metal_server.py           # Metal ion parameterization
  slurm_server.py           # SLURM integration

container/
  Dockerfile                # 3-stage build (conda-base → openmm-builder → runtime)
  scripts/
    entrypoint.sh
    test-container.sh

tests/                      # 4-level test suite
  conftest.py
  test_mcp_server.py        # Level 1: Unit tests
  test_cli.py               # Level 1: CLI tests
  test_slurm_server.py      # Level 1: SLURM mock tests
  test_server_smoke.py      # Level 2: Server smoke tests
  test_pipeline_1ake.py     # Level 3: Full 1AKE pipeline
```

## Tool Responsibilities

| Module | Tools |
|--------|-------|
| research | `download_structure`, `get_alphafold_structure`, `inspect_molecules`, `search_proteins`, `analyze_structure_details` |
| structure | `prepare_complex`, `clean_protein`, `clean_ligand`, `split_molecules`, `merge_structures`, `create_mutated_structutre` |
| genesis | `boltz2_protein_from_seq`, `rdkit_validate_smiles`, `pubchem_get_smiles_from_name` |
| solvation | `solvate_structure`, `embed_in_membrane`, `list_available_lipids` |
| amber | `build_amber_system` |
| md_simulation | `run_equilibration`, `run_production`, `analyze_rmsd`, `analyze_rmsf`, `analyze_hydrogen_bonds`, ... |
| literature | `pubmed_search`, `pubmed_fetch` |
| metal | `parameterize_metal_ion`, `detect_metal_ions` |
| slurm | `inspect_cluster`, `submit_job`, `check_job`, `list_jobs`, `list_tracked_jobs`, `cancel_job`, `check_job_log`, `set_policy`, `show_policy`, `configure_container` |

## Development Setup

### Option 1: Work in the repo (recommended)

```bash
git clone https://github.com/matsunagalab/mdclaw.git
cd mdclaw

# Skills and slash commands work directly via .claude/commands/
# (no plugin install needed — Claude Code finds them automatically)

# For CLI tools, either:
# (a) Use the container via bin/mdclaw (Singularity or Docker)
./bin/mdclaw --list

# (b) Install locally via conda (full scientific stack)
conda env create -f environment.yml
conda activate mdclaw
pip install -e .
mdclaw --list
```

### Option 2: Plugin install (user-like testing)

```bash
# From Claude Code in any directory:
/plugin install /path/to/mdclaw
```

## Development Workflow

### Daily Cycle

```
1. Edit code in mdclaw/ or skills/
2. Lint:    ruff check mdclaw/
3. Test:    pytest tests/test_mcp_server.py tests/test_cli.py tests/test_slurm_server.py -v
4. Smoke:   pytest tests/test_server_smoke.py -v        (if touching tool logic)
5. Test skills:  /md-prepare (in Claude Code, new conversation)
6. Commit
```

### Test Levels

| Level | File | Tests | Requirements |
|-------|------|-------|-------------|
| 1 | `test_mcp_server.py`, `test_cli.py`, `test_slurm_server.py` | 130+ | None (stdlib) |
| 2 | `test_server_smoke.py` | 15 | conda env (AmberTools, OpenMM) |
| 3 | `test_pipeline_1ake.py` | 7 | Full conda env + network |
| 4 | `manual_checklist.md` | — | Claude Code interactive |

### Adding a New Tool

1. Add function in the appropriate `mdclaw/*_server.py` as a plain Python function
2. Add the function to the `TOOLS` dict at the bottom of that server file
3. Add unit test in `tests/test_mcp_server.py` (tool registration check)
4. Add smoke test in `tests/test_server_smoke.py` (actual execution)
5. Run `mdclaw --list` to verify CLI auto-discovery
6. Update CLAUDE.md server section with the new tool signature

### Adding a New Server

1. Create `mdclaw/new_server.py` with tool functions and a `TOOLS` dict
2. Register in `mdclaw/_registry.py` (`SERVER_REGISTRY`)
3. Add smoke tests in `tests/test_server_smoke.py`
4. Update `CLAUDE.md` architecture diagram and server section

### Modifying Skills

Skills in `skills/*/SKILL.md` reference tools via CLI (`mdclaw <tool> ...`). When changing tool signatures, update the corresponding SKILL.md examples.

Skills are read by Claude Code at conversation start, so changes take effect in a **new conversation**.

## Release (Version Tag Sync)

Skills (plugin) and tools (container image) are distributed through separate channels, kept in sync via version tags.

```bash
# 1. Bump version in 4 files (must match):
#    mdclaw/__init__.py        __version__ = "X.Y.Z"
#    pyproject.toml            version = "X.Y.Z"
#    .claude-plugin/plugin.json       "version": "X.Y.Z"
#    .claude-plugin/marketplace.json  "version": "X.Y.Z"

# 2. Commit and tag
git add -A && git commit -m "release: vX.Y.Z"
git tag vX.Y.Z
git push origin main --tags

# 3. Build the Docker image (Stage 2 takes 10-15 min for OpenMM source build)
docker build -f container/Dockerfile -t mdclaw:latest .

# 4. Test locally
docker run --rm --gpus all -v $(pwd)/container/scripts/test-container.sh:/work/test.sh:ro \
    mdclaw:latest bash /work/test.sh

# 5. Push to GHCR
gh auth refresh --hostname github.com --scopes write:packages  # if needed
gh auth token | docker login ghcr.io -u <github-username> --password-stdin
docker tag mdclaw:latest ghcr.io/matsunagalab/mdclaw:X.Y.Z
docker tag mdclaw:latest ghcr.io/matsunagalab/mdclaw:latest
docker push ghcr.io/matsunagalab/mdclaw:X.Y.Z
docker push ghcr.io/matsunagalab/mdclaw:latest

# 6. (Optional) Convert to SIF and test on HPC cluster
singularity pull mdclaw.sif docker://ghcr.io/matsunagalab/mdclaw:X.Y.Z
singularity exec --nv mdclaw.sif bash container/scripts/test-container.sh
```

Users receive updates via:
- `/plugin update mdclaw@mdclaw` — updates skills and the `bin/mdclaw` wrapper
- SessionStart hook automatically re-downloads the container on the next session start

## Container Details

The container is built in 3 stages (see `container/Dockerfile`):

1. **conda-base** (`condaforge/mambaforge`): Creates the conda environment with AmberTools, RDKit, PDBFixer, PyTorch (cu121). OpenMM is excluded — built from source in Stage 2.
2. **openmm-builder** (`nvidia/cuda:12.1.1-devel-ubuntu22.04`): Clones OpenMM source and compiles against CUDA 12.1 toolkit. This gives broader driver compatibility (530+) than pre-built pip/conda OpenMM packages.
3. **runtime** (`nvidia/cuda:12.1.1-runtime-ubuntu22.04`): Slim image with the conda env + built OpenMM + CUDA 12.1 NVRTC libraries copied from Stage 2.

Final image size: ~14.5 GB Docker / ~4.6 GB SIF.

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MDCLAW_RUNTIME` | auto | Force `singularity` or `docker` |
| `MDCLAW_SIF` | _(auto)_ | Path to Singularity SIF file |
| `MDCLAW_DOCKER_IMAGE` | `ghcr.io/matsunagalab/mdclaw:latest` | Docker image name |
| `MDCLAW_OUTPUT_DIR` | `.` | Default output directory |
| `MDCLAW_DEFAULT_TIMEOUT` | `300` | Default tool timeout (seconds) |
| `MDCLAW_SOLVATION_TIMEOUT` | `7200` | Solvation timeout |
| `MDCLAW_MD_SIMULATION_TIMEOUT` | `3600` | MD execution timeout |
| `MDCLAW_SLURM_TIMEOUT` | `120` | SLURM command timeout |
| `MDCLAW_MODULE_LOADS` | _(unset)_ | HPC module load commands (e.g., `cuda/12.1`) |
| `MDCLAW_MODULE_INIT` | `/etc/profile.d/modules.sh` | Module system init script path |

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
