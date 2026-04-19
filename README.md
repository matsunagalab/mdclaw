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

`bin/mdclaw` chooses the runtime in this order:
- `MDCLAW_RUNTIME=singularity|apptainer|docker` if you set it explicitly
- otherwise `singularity` if a `.sif` is available
- otherwise `apptainer` if a `.sif` is available
- otherwise `docker`

If no container runtime is available, `bin/mdclaw` falls back to a local
`mdclaw` on your `PATH` (for example from a conda environment or `pip install -e .`).

The session-start hook downloads the container automatically:
- on HPC, it prefers a `.sif` for Singularity/Apptainer
- on desktop, it falls back to pulling the Docker image

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
> /mdclaw:md-production job_a1b2c3d4, 10 ns
> /mdclaw:md-production job_a1b2c3d4, 100 ns with seed 42
```

HPC:
```
> /mdclaw:hpc-run submit 100 ns MD of 1AKE to GPU partition on node gpu01
> /loop 15m /mdclaw:hpc-run check job 12345 and report when done
```

You can also call `mdclaw <tool>` directly. For DAG workflow tools
(`download_structure`, `prepare_complex`, `solvate_structure`,
`build_amber_system`, `run_equilibration`, `run_production`, etc.),
create a node first and then pass both `--job-dir` and `--node-id`.
See `mdclaw --list`.

### Execution Mode

MDClaw tracks **how much it asks** the user during a skill:

- `execution_mode=autonomous` (default): proceed with user-specified values and
  repo defaults. Ask only when a required choice is missing, the target is
  ambiguous, or a structured failure needs a user decision.
- `execution_mode=human_in_the_loop`: stop at each decision checkpoint and ask
  before continuing.

The mode is stored in `progress.json.params`, so later skills on the same
`job_dir` reuse the same behavior without re-inferring it from chat history.

Skill sequencing is always **user-initiated**: `/md-prepare` →
`/md-equilibration` → `/md-production` → `/md-analyze`. Each skill stops at
the end of its stage and tells the user the next command to run. There is
no automatic end-to-end chaining — you run the next stage yourself when you
are ready.

### Defaults

ff19SB + OPC water, 15 Å buffer, 0.15 M NaCl, 300 K, 1 bar (NPT),
LangevinMiddleIntegrator with HMR (4 fs timestep, hydrogenMass=4 amu),
HBonds constraints, PME for explicit water. Equilibration uses CA
positional restraints (100 kJ/mol/nm²) for NVT (2500 steps, 4 fs) +
NPT (5000 steps, 4 fs).

### Output Structure

Each pipeline step is an independent **node** with its own directory,
state file, and artifacts. Parent-child relationships form a DAG:

```
job_a1b2c3d4/
  progress.json                ← thin index of nodes + cached summaries
  nodes/
    fetch_001/                 ← structure acquisition root
      node.json
      artifacts/
        1AKE.pdb
    prep_001/                  ← structure preparation
      node.json                ← node state, artifacts, metadata
      artifacts/
        split/ merge/
    solv_001/                  ← solvation
      node.json
      artifacts/
        solvated.pdb
    topo_001/                  ← topology (shared by all eq/prod)
      node.json
      artifacts/
        system.parm7 system.rst7
    eq_001/                    ← equilibration at 300K
      node.json
      artifacts/
        equilibrated.chk
    prod_001/                  ← production 300K
      node.json
      artifacts/
        trajectory.dcd
    eq_002/                    ← equilibration at 310K (branch)
      ...
    prod_002/                  ← production 310K (branch)
      ...
  events/                      ← append-only audit log
```

The same topology node can be reused for multiple equilibrations at
different temperatures. Each equilibration can branch into multiple
productions (different seeds, lengths, etc.).

One `job_dir` represents one physical system. Keep a single `fetch` root per
job and use branching after `prep` to explore preparation, equilibration, and
production variants.

### State Management

- **Skills** decide what to run (orchestration only, no state mutation)
- **Tools** execute and self-record results via `begin_node`/`complete_node`/`fail_node`
- Input files are **auto-resolved from the DAG**: e.g., `run_equilibration`
  finds `parm7`/`rst7` from its `topo` ancestor automatically

| File | Scope | Updated by |
|------|-------|------------|
| `progress.json` | Job-level: node index (type, status, parents), cached system/params | Tools (automatic) |
| `node.json` | Per-node: artifacts, metadata, conditions, warnings | Tools (automatic) |
| `events/*.json` | Audit trail: one file per event (no locking needed) | Tools (automatic) |

This means:
- **Resume** works by reading `progress.json` — even across sessions or agents
- **Branching** is natural: create new nodes with different parents
- **Parallel agents** can work on different nodes concurrently (lock files prevent conflicts)
- **Direct CLI use** still updates state correctly as long as workflow tools run with `--job-dir` and `--node-id`

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
3. pytest tests/test_mcp_server.py tests/test_cli.py tests/test_guardrails.py tests/test_slurm_server.py -v
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
