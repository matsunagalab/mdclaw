# MDClaw: Your personal MD assistant

**From PDB ID to production-ready MD simulation — automated.**

MDClaw turns any PDB structure, FASTA sequence, or ligand SMILES into an
Amber/OpenMM simulation through AI-powered tools and domain knowledge.
It works with Claude Code, Cursor, Windsurf, or any AI coding assistant.

---

## For Users

### Install

Claude Code plugin:

```
/plugin marketplace add matsunagalab/mdclaw
/plugin install mdclaw@mdclaw
```

The container (~4.6 GB) downloads automatically on first session start.

Pi:

```bash
pi install git:github.com/matsunagalab/mdclaw@main
```

OpenCode, Codex, and other repo-local agents:

```bash
git clone https://github.com/matsunagalab/mdclaw
cd mdclaw
scripts/install-agent-skills.sh
scripts/mdclaw-doctor.sh
```

`scripts/install-agent-skills.sh` creates `.agents/skills/<name>` entries for
portable Agent Skills discovery. Use `scripts/install-agent-skills.sh --copy`
instead if your agent does not follow symlinks. `scripts/mdclaw-doctor.sh`
checks the CLI runtime, OpenMM platforms, AmberTools executables, container
runtime detection, and skill installation.

**Requirements:**
- Container runtime: Singularity/Apptainer (HPC) or Docker (macOS/desktop)
- GPU (optional): NVIDIA driver 520+ (the image ships CUDA 11.8; driver
  450+ is the theoretical floor, 520+ is what we actively verify)

`bin/mdclaw` chooses the runtime in this order:
- `MDCLAW_RUNTIME=conda|singularity|apptainer|docker` if you set it explicitly
- otherwise a conda env named `mdclaw` if it exists
- otherwise `singularity` if a `.sif` is available
- otherwise `apptainer` if a `.sif` is available
- otherwise `docker`

If no managed runtime is available, `bin/mdclaw` falls back to a local
`mdclaw` on your `PATH` (for example from `pip install -e .`).
For local development, use `conda env create -f environment.yml`; that
environment includes conda-only scientific tools and `pymol-open-source` for
headless structure preview rendering. A plain pip install does not provide the
PyMOL executable.

The session-start hook downloads the container automatically:
- on HPC, it prefers a `.sif` for Singularity/Apptainer
- on desktop, it falls back to pulling the Docker image

For a generic harness without plugin support:

1. Put this repository where the harness can read `skills/`, or run
   `scripts/install-agent-skills.sh` so it can read `.agents/skills/`.
2. Add `bin/mdclaw` to `PATH`, or install the Python package and expose the
   `mdclaw` CLI.
3. Use one runtime: conda (`environment.yml`), SIF (`MDCLAW_SIF`), or Docker
   (`MDCLAW_DOCKER_IMAGE`). If slash commands are unavailable, have the
   harness read the relevant `skills/<name>/SKILL.md` directly.

See `docs/agents/deployment.md` for Pi, OpenCode, Codex, Claude Code, and
generic Agent Skills deployment notes.

### Skills

| Command | Purpose |
|---------|---------|
| `/mdclaw:md-prepare` | Structure → cleaning → solvation → topology |
| `/mdclaw:md-equilibration` | Energy minimization → NVT heating → NPT density (also supports multi-stage NPT → NVT → NPT chains via eq → eq DAG) |
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
(`fetch_structure`, `prepare_complex`, `solvate_structure`,
`build_amber_system`, `run_equilibration`, `run_production`, etc.),
create a node first and then pass both `--job-dir` and `--node-id`.
See `mdclaw --list`.

### Nucleic Acids

Standard DNA/RNA chains are supported through the same DAG workflow: include
`nucleic` during preparation and `build_amber_system` resolves the matching
`amber/DNA.OL15.xml` / `amber/RNA.OL3.xml` from `forcefield_catalog` into
the SystemGenerator bundle. Modified nucleotides are handled as an explicit prep
branch with `prepare_modified_nucleic`, which requires `MDCLAW_MODXNA_DIR`
unless the environment already provides `modxna.sh` and `dat/frcmod.modxna`.
MDClaw can auto-fill curated modXNA fragment presets such as `5CM`, while
unknown modifications still require explicit `backbone` / `sugar` / `base`
fragment IDs. See `skills/md-prepare/branches.md` for the workflow.

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

Those `/md-*` commands are shortcuts. The portable sequence is the same set of
runbooks: `skills/md-prepare/SKILL.md` → `skills/md-equilibration/SKILL.md` →
`skills/md-production/SKILL.md` → `skills/md-analyze/SKILL.md`.

### Defaults

ff19SB + OPC water, 15 Å buffer, 0.15 M NaCl, 300 K, 1 bar (NPT),
LangevinMiddleIntegrator with HMR (4 fs timestep, hydrogenMass=4 amu),
HBonds constraints, PME for explicit water. Equilibration uses CA
positional restraints (100 kJ/mol/nm²) for NVT (250000 steps, 4 fs = 1 ns) +
NPT (250000 steps, 4 fs = 1 ns). Override the stage lengths with
`--nvt-steps` / `--npt-steps` on `run_equilibration`.

Need finer control? Multi-stage equilibration (e.g. NPT compress → NVT
thermalize → NPT relax with per-stage restraint atoms and force constants)
is supported via eq → eq DAG chaining — see the "Multi-Stage Chaining"
section in `skills/md-equilibration/SKILL.md`. Ensembles can also be
switched freely between nodes (NPT eq → NVT prod, etc.); the saved XML
state is portable across barostat configurations.

### Output Structure

Each pipeline step is an independent **node** with its own directory,
state file, and artifacts. Parent-child relationships form a DAG:

```
job_a1b2c3d4/
  progress.json                ← thin index of nodes + cached summaries
  nodes/
    source_001/                 ← structure acquisition root
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
        system.system.xml      ← serialized OpenMM System
        system.topology.pdb    ← OpenMM Topology + box vectors
        system.state.xml       ← post-minimization state
    eq_001/                    ← equilibration at 300K
      node.json
      artifacts/
        equilibrated.xml       ← portable state (loaded by default)
        equilibrated.chk       ← binary checkpoint (kept for reproducibility)
    prod_001/                  ← production 300K
      node.json
      artifacts/
        trajectory.dcd
        state.xml              ← portable state (end + periodic)
        checkpoint.chk         ← binary checkpoint (periodic)
    eq_002/                    ← equilibration at 310K (branch)
      ...
    prod_002/                  ← production 310K (branch)
      ...
  events/                      ← append-only audit log
```

The same topology node can be reused for multiple equilibrations at
different temperatures. Each equilibration can branch into multiple
productions (different seeds, lengths, etc.). For multi-stage
equilibration (e.g. NPT compress → NVT thermalize → NPT relax), chain
eq nodes by parenting each onto the previous eq — the next node
auto-resumes from its parent's `state.xml` regardless of ensemble.

One `job_dir` represents one physical system. Keep a single `source` root per
job and use branching after `prep` to explore preparation, equilibration, and
production variants.

### Optional Study Directories

For multi-system or campaign-level work, keep the per-system `job_dir`
contract above and add an optional `study_dir` that indexes multiple jobs:

```text
study_mutation_screen/
  study.json
  decisions.jsonl             # optional cross-job decision log
  question_history.jsonl      # optional question/revision history
  token_ledger.jsonl          # optional LLM/token accounting
  annotations/                # optional external model or user context
  evidence/                   # optional study-level evidence reports
  jobs/
    wt/
      progress.json
      nodes/source_001/...
    mut_v148a/
      progress.json
      nodes/source_001/...
```

Use `mdclaw init_study`, `mdclaw add_study_job`,
`mdclaw record_study_decision`, and `mdclaw summarize_study` to maintain
this layer. It is intentionally optional: ordinary one-system MD workflows do
not need a `study_dir`.

For a runnable skeleton, see `examples/study/`:

```bash
bash examples/study/mutation_campaign.sh
```

The example creates a WT-vs-mutant campaign scaffold, registers two planned
`job_dir`s, and records a question, decision, and token-ledger entry without
running molecular dynamics.

### Evidence And Methods Reports

MDClaw can emit lightweight evidence and manuscript-oriented Methods reports
from completed DAG state:

```bash
mdclaw generate_md_evidence_report --job-dir job_a1b2c3d4
mdclaw generate_md_methods_report --job-dir job_a1b2c3d4

mdclaw generate_study_evidence_report --study-dir study_mutation_screen
mdclaw generate_study_methods_report --study-dir study_mutation_screen
```

Evidence reports are JSON summaries for downstream tools or notebooks. Methods
reports are Markdown drafts that trace the selected node lineage, include a
Mermaid workflow schematic, and select BibTeX entries from
`docs/research/mdclaw_citation_inventory.md` when available.

### State Management

- **Skills** decide what to run (orchestration only, no state mutation)
- **Tools** execute and self-record results via `begin_node`/`complete_node`/`fail_node`
- Input files are **auto-resolved from the DAG**: e.g.,
  `run_equilibration` finds the `system.xml` + `topology.pdb` +
  `state.xml` triple from its `topo` ancestor automatically. The XML
  triple is the only topology contract on the run side

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

### MDAgentBench

MDClaw includes a tool-agnostic benchmark contract for evaluating MD agents
across preparation, execution, scientific interpretation, and publication-ready
evidence packaging. The checked-in dataset is under `benchmarks/mdagentbench/`.
Agents receive each task's `prompt.md` and write a standard `submission/`
directory. The benchmark code reads `task.json` only for validation, scoring,
and run summaries.

Useful commands:

```bash
mdclaw init_benchmark_run --output-dir benchmark_runs --run-id <run_id>
mdclaw validate_benchmark_submission --task-file benchmarks/mdagentbench/tasks/T01_engine_smoke/task.json --submission-dir <submission_dir>
mdclaw score_benchmark_submission --task-file benchmarks/mdagentbench/tasks/T01_engine_smoke/task.json --submission-dir <submission_dir> --run-id <run_id> --output-file benchmark_runs/<run_id>/tasks/T01_engine_smoke/score.json
mdclaw summarize_benchmark_run --run-dir benchmark_runs/<run_id>
```

See `docs/benchmark/README.md` for the task schema, submission contract,
structured LLM judge format, and append-only result ledgers.

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
exists when installed as a plugin). Other harnesses can read the same
`skills/*/SKILL.md` files directly and invoke `mdclaw` through Bash.

Local reference PDFs or manuals can be kept under `ref/`. That directory is
ignored by git and is intended for developer reference material only.

### Daily Cycle

```
1. Edit code in mdclaw/ or skills/
2. conda run -n mdclaw ruff check mdclaw/
3. conda run -n mdclaw pytest tests/test_mcp_server.py tests/test_cli.py tests/test_guardrails.py tests/test_slurm_server.py -v
4. Test skills in a new Claude Code conversation
5. Commit
```

See `CLAUDE.md` / `AGENTS.md` for the mirrored short agent guide and
`docs/developer/` for tool reference, architecture details, test levels,
container internals, release steps, and configuration.

### Release

```bash
# Full version-sync, container test, and GHCR publish flow:
# docs/developer/release.md
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
