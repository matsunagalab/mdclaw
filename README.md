# MDClaw: Your personal MD assistant

**From PDB ID to production-ready MD simulation — automated.**

MDClaw turns any PDB structure, FASTA sequence, or ligand SMILES into an
Amber/OpenMM simulation through AI-powered tools and domain knowledge.
It works with Claude Code, Cursor, Windsurf, or any AI coding assistant.

---

## For Users

### Install

```
/plugin marketplace add matsunagalab/mdclaw
/plugin install mdclaw@mdclaw
```

The container (~4.6 GB) downloads automatically on first session start.

**Requirements:**
- Container runtime: Singularity/Apptainer (HPC) or Docker (macOS/desktop)
- GPU (optional): NVIDIA driver 530+ (CUDA 12.1+)

`bin/mdclaw` auto-detects the runtime. Override with `MDCLAW_RUNTIME=docker` or `MDCLAW_RUNTIME=singularity`.

### Skills

| Command | Purpose |
|---------|---------|
| `/mdclaw:md-prepare` | Structure → cleaning → solvation → topology |
| `/mdclaw:md-equilibration` | Energy minimization → NVT heating → NPT density |
| `/mdclaw:md-production` | Production MD (NPT/NVT, HMR, checkpoint restart) |
| `/mdclaw:md-analyze` | RMSD, RMSF, energy, hydrogen bonds |
| `/mdclaw:hpc-run` | SLURM job submission, monitoring, restart |

### Examples

```
> /mdclaw:md-prepare 1AKE chain A, no ligands, explicit water, defaults
> /mdclaw:md-equilibration job_a1b2c3d4
> /mdclaw:md-production job_a1b2c3d4 run_001_300K, 10 ns
```

Batch:
```
> /mdclaw:md-prepare 1AKE, 4AKE chain A, explicit water
> /mdclaw:md-equilibration batch_a1b2c3d4
> /mdclaw:md-production batch_a1b2c3d4, 100 ns on GPU partition
```

HPC:
```
> /mdclaw:hpc-run submit 100 ns MD of 1AKE to GPU partition on node gpu01
> /loop 15m /mdclaw:hpc-run check job 12345 and report when done
```

You can also call `mdclaw <tool>` directly. See `mdclaw --list`.

### Defaults

ff19SB + OPC water, 15 Å buffer, 0.15 M NaCl, 300 K, 1 bar (NPT),
LangevinMiddleIntegrator with HMR (4 fs timestep, hydrogenMass=4 amu),
HBonds constraints, PME for explicit water. Equilibration uses CA
positional restraints (100 kJ/mol/nm²) for NVT (2500 steps, 4 fs) +
NPT (5000 steps, 4 fs).

### Output Structure

```
job_a1b2c3d4/
  progress.json              ← system info, preparation details
  topology/                  ← parm7 + rst7 (shared by all runs)
  runs/
    run_001_300K/
      run.json               ← conditions, energy, trajectory paths
      equilibration/          ← equilibrated.chk
      production/             ← trajectory.dcd
    run_002_310K/             ← same topology, different temperature
      ...
```

The same topology can be reused for multiple runs at different temperatures
or random seeds. Each run is self-contained under `runs/<run_id>/`.

### Reproducibility

`progress.json` and `run.json` are auto-recorded by CLI tools — sufficient
to regenerate the workflow and write a paper Methods section.

---

## For Developers

### Setup

```bash
git clone https://github.com/matsunagalab/mdclaw.git
cd mdclaw
./bin/mdclaw --list             # uses container (Singularity or Docker)
# OR for full local install:
conda env create -f environment.yml && conda activate mdclaw && pip install -e .
```

Skills work directly via `.claude/commands/` when running Claude Code in
the repo — no plugin install needed. In this dev mode, slash commands
have **no `mdclaw:` prefix**: use `/md-prepare`, `/md-equilibration`,
`/md-production`, `/md-analyze`, `/hpc-run` (the `/mdclaw:*` form only
exists when installed as a plugin).

### Daily Cycle

```
1. Edit code in mdclaw/ or skills/
2. ruff check mdclaw/
3. pytest tests/test_mcp_server.py tests/test_cli.py tests/test_slurm_server.py -v
4. Test skills in a new Claude Code conversation
5. Commit
```

See **CLAUDE.md** for: tool list, architecture details, adding tools/servers,
test levels, container build internals, and full configuration reference.

### Release

```bash
# 1. Bump version in 4 files (must match):
#    mdclaw/__init__.py, pyproject.toml,
#    .claude-plugin/plugin.json, .claude-plugin/marketplace.json

# 2. Tag and push
git add -A && git commit -m "release: vX.Y.Z"
git tag vX.Y.Z && git push origin main --tags

# 3. Build, test, push to GHCR
docker build -f container/Dockerfile -t mdclaw:latest .
docker tag mdclaw:latest ghcr.io/matsunagalab/mdclaw:X.Y.Z
docker tag mdclaw:latest ghcr.io/matsunagalab/mdclaw:latest
docker push ghcr.io/matsunagalab/mdclaw:X.Y.Z
docker push ghcr.io/matsunagalab/mdclaw:latest
```

Users update via `/plugin update mdclaw@mdclaw`. SessionStart hook
re-downloads the container on the next session.

---

## License

MIT

## Citations

- **Boltz-2**: S. Passaro et al., bioRxiv (2025). doi:10.1101/2025.06.14.659707
- **AmberTools**: D. A. Case et al., J. Chem. Inf. Model. 63, 6183 (2023).
- **OpenMM**: P. Eastman et al., J. Phys. Chem. B 128, 109 (2024).
