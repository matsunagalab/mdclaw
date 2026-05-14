<p align="center">
  <img src="docs/assets/mdclaw-logo.png" alt="MDClaw logo" width="720">
</p>

# MDClaw

MDClaw provides skills and CLIs for vibe-MD simulations and autonomous
scientific investigation in the Amber/OpenMM ecosystem. It helps an AI agent
turn scientific intent into reproducible atomistic work: prepare systems, run
equilibration and production MD, analyze trajectories, branch hypotheses, and
package evidence with provenance.

## What MDClaw Can Do

- Turn a scientific question into a study plan with observables and
  decision criteria, then run the planned MD jobs end-to-end.
- Prepare MD systems from PDB IDs, AlphaFold/UniProt entries, or local
  structure files.
- Start from a study-level scientific question, translate it into a small MD
  plan, then organize one or more job DAGs under that study.
- Inspect chains, ligands, waters, ions, glycans, DNA/RNA, and modified
  residues before committing to a setup.
- Clean structures, preserve selected ligands when safe, solvate systems, and
  assign Amber/OpenMM force fields.
- Build OpenMM-ready topology artifacts, then run equilibration and production
  MD with restartable state files.
- Branch workflows for mutations, PTMs, ligand choices, solvent models,
  temperatures, seeds, and protocols.
- Run locally, through containers, or on SLURM/HPC systems.
- Analyze trajectories and package reproducible evidence, provenance, figures,
  and Methods-style reports.
- Evaluate MD agents with the included MDAgentBench dataset and scorer.

MDClaw is split into two things that are deployed together but should be
understood separately:

| Layer | What It Is | Main Files |
|---|---|---|
| Skill layer | Agent-facing MD decision policy and procedures | `skills/`, `.agents/skills/`, `.claude/skills/` |
| MD runtime | The scientific software stack and CLI that perform the work | `bin/mdclaw`, `mdclaw/`, `container/`, `hooks/` |

The skills are text and are portable across agent harnesses. The MD runtime is
the packaged scientific stack behind the CLI: a conda environment,
Singularity/Apptainer SIF, Docker image, or local editable install.

## Install / Deploy

Choose the path that matches your agent. After installation, run
`scripts/mdclaw-doctor.sh` when using a repo checkout; it checks the runtime,
OpenMM, AmberTools, container availability, and skill discovery.

### Claude Code Plugin

Use this when you want `/mdclaw:*` slash commands and plugin-managed runtime
setup.

```text
/plugin marketplace add matsunagalab/mdclaw
/plugin install mdclaw@mdclaw
```

The plugin provides:

- `.claude-plugin/`: marketplace metadata.
- `hooks/hooks.json`: SessionStart hook that prepares the packaged MD runtime.
- `bin/mdclaw`: runtime wrapper that chooses conda, SIF, or Docker.
- `skills/`: the same MDClaw skills used by other agents.

The plugin prepares the container runtime on first session start. On HPC it
prefers a SIF for Singularity/Apptainer; on desktop it can use Docker. This is
only the execution environment for `mdclaw <tool>`; skill discovery remains the
same text files under `skills/`.

### Pi

Pi reads skills from the repository package metadata:

```bash
pi install git:github.com/matsunagalab/mdclaw@main
```

`package.json` points Pi at `./skills`. You still need one MD runtime:
the `mdclaw` conda env, a SIF through `MDCLAW_SIF`, Docker through
`MDCLAW_DOCKER_IMAGE`, or the plugin/container wrapper.

### Claude Code, Codex, OpenCode, and Generic Agents

Use this path when an agent discovers skills from repo-local skill mirrors.

```bash
git clone https://github.com/matsunagalab/mdclaw
cd mdclaw
scripts/install-agent-skills.sh
scripts/mdclaw-doctor.sh
```

`scripts/install-agent-skills.sh` creates `.agents/skills/<name>` and
`.claude/skills/<name>` symlinks to `skills/<name>`. Use
`scripts/install-agent-skills.sh --copy` if your agent or filesystem does not
follow symlinks.

Repo-local Claude Code uses `.claude/skills/` for skill discovery. The older
repo-local short commands such as `/md-prepare` are intentionally not tracked;
use the discovered skills directly, or install the Claude plugin when you want
the plugin command namespace such as `/mdclaw:md-prepare`.

### Local Runtime

For development or non-plugin usage, create the conda environment:

```bash
conda env create -f environment.yml
conda activate mdclaw
pip install -e .
mdclaw --list
```

`bin/mdclaw` chooses a runtime in this order:

1. `MDCLAW_RUNTIME=conda|singularity|apptainer|docker`, if set.
2. A conda env named `mdclaw`, if available.
3. Singularity/Apptainer with `MDCLAW_SIF` or an auto-downloaded SIF.
4. Docker image `ghcr.io/matsunagalab/mdclaw:<version-or-latest>`.
5. A local `mdclaw` on `PATH`.

See `docs/agents/deployment.md` for the full deployment matrix and
`docs/developer/container.md` for container details.

## Ask In Plain Language

Users do not need to remember command names. The framing of your request
decides how far MDClaw goes — three patterns:

**Plan only.** Ask the agent to plan a study. It records a lightweight
`study_plan.json` (question, MD goal, planned jobs, observables, decision
criteria) and stops so you can review before any system is built.

```text
Plan an MD study for the PSD-95 PDZ3 domain bound to the CRIPT peptide
(PDB 1BE9). Test whether the H372A mutation weakens dynamic coupling between
the distal alpha-3 helix and the peptide-binding groove. Define the WT and
mutant jobs, peptide-contact and groove-dynamics observables, and decision
criteria.
```

**End-to-end.** Ask the agent to take the scientific question all the way to
MD. It plans the study, then runs the planned jobs through preparation,
equilibration, production, and analysis.

```text
Set up and run an apo-vs-holo MD study for the T4 lysozyme L99A
benzene-binding cavity (benzene-bound PDB 4W53). Test whether benzene
occupancy stabilizes the engineered hydrophobic cavity. Plan the minimal
job set, then prepare, equilibrate, run 50 ns of production per job, and
analyze cavity hydration and ligand-pose observables.
```

**Direct one-system run.** Skip the scientific framing and ask for a single
MD run. MDClaw takes the fast path with a thin study record (one
`jobs/main`).

```text
Prepare PDB 1AKE chain A as a protein-only explicit-water system using the
default force field and water model. Continue through default equilibration,
run 10 ns of production MD, and analyze RMSD, RMSF, and energy stability.
```

Good prompts for **planning** state the scientific question, comparison
groups, and what evidence would answer the question. Good prompts for
**end-to-end runs** add a production length, replicate count, and the
observables that decide the outcome. Good prompts for **direct runs**
specify the structure source, molecular selection, solvent model, force
field, runtime target, duration, ensemble, stopping policy, and desired
evidence.

## Repository Map

| Path | Role |
|---|---|
| `skills/` | Portable MDClaw skills. This is the source of truth for skill behavior. |
| `.agents/skills/` | Generic Agent Skills discovery entries, symlinked to `skills/`. |
| `.claude/skills/` | Repo-local Claude Code skill discovery entries, symlinked to `skills/`. |
| `.claude-plugin/` | Claude plugin marketplace metadata. |
| `hooks/` | Plugin lifecycle hooks, including packaged runtime setup. |
| `bin/mdclaw` | Runtime wrapper used by plugin and local deployments. |
| `mdclaw/` | Python package and CLI tool implementations. |
| `container/` | Docker image and Singularity/Apptainer SIF build assets for the packaged MD runtime. |
| `benchmarks/mdagentbench/` | MDAgentBench dataset and scorer contracts. |
| `docs/agents/` | Deployment notes for agent harnesses. |
| `docs/developer/` | Architecture, CLI internals, testing, release, and tool references. |
| `tests/` | Unit, smoke, benchmark, and integration tests. |

## Workflow DAG

Internally, each MD job is represented as a workflow DAG. This is the technical
contract that lets agents resume work, branch variants, and report exactly
which artifacts were used.

![MDClaw workflow DAG](docs/assets/mdclaw-dag.png)

The main path is:

```text
study question -> MD study plan -> source bundle -> select + prepare -> solvate -> topology / force field -> equilibrate -> production MD -> analyze / evidence
```

A study is the outer record for the scientific question. It may contain one
job, such as `jobs/main`, or many jobs for WT versus mutant, apo versus holo,
or protocol comparisons. Inside each job, the `source` node records a source
bundle. A bundle can contain one structure or multiple candidate structures,
such as NMR models, PDB assembly choices, or generated prediction ensembles.
Internally, MDClaw normalizes these into `candidates/candidate_*` files and
records the index/provenance in `source_bundle.json`. Generated ensembles such
as Boltz-2 predictions can also attach per-candidate rank and confidence
metrics. The `prep` node selects one concrete candidate before making an
MD-ready physical system.

For clear single-system requests, the study plan is optional: a thin study with
one `jobs/main` job is enough. For scientific comparisons or campaigns,
`study_plan.json` keeps the question, MD goal, planned jobs, intended analyses,
and decision criteria connected to the evidence report.

Each step writes a node with its own state, artifacts, and provenance. Branches
can fork from preparation, solvation, topology, equilibration, or production
when comparing variants such as mutants, ligands, protocols, temperatures, or
random seeds.

Detailed node layout, artifact names, study directories, and invariants live in
`docs/developer/architecture.md`.

## Technical Scope And Guardrails

- Protein systems with Amber ff19SB / OpenMM.
- Explicit solvent setup, defaulting to OPC, 15 A buffer, and 0.15 M salt.
- HMR production runs with 4 fs timestep by default.
- Standard DNA/RNA through OL15/OL3 XMLs.
- Ligand preparation through curated Amber/OpenMM pathways where supported.
- Branching workflows for mutations, PTMs, modified nucleic acids, membrane
  embedding, alternate equilibration protocols, and production variants.
- SLURM submission and restart/extension workflows through `hpc-run`.

Some chemistry remains deliberately guarded. If a force-field conversion or
parameterization path is not safe, tools return structured error codes instead
of silently building a dubious system.

## Benchmarking

MDClaw includes MDAgentBench under `benchmarks/mdagentbench/`. The benchmark is
agent-agnostic: evaluated agents read `prompt.md` and write `submission/`;
the scorer reads `task.json`, scorer-only truth files, and submitted artifacts.

The v1.0 task set covers nine tasks in four families:

| Family | What It Tests | Example Tasks |
|---|---|---|
| System preparation and guardrails | MD-ready artifacts, ligand-pose preservation, and structured refusal for unsafe parameterization. | Metal guardrail refusal; T4L L99A + benzene pose preservation |
| Execution and engine reliability | Short explicit-water MD, finite energies, reloadable trajectories, and restart continuation. | Engine smoke MD; short protein MD; restart continuation |
| Scientific answer versus experimental truth | Mutation stability and protein-protein binding-effect questions with held-back truth. | T4L L99A stability direction; barnase D39A binding hotspot |
| Evidence and Methods communication | Figures, metrics, captions, provenance, limitations, and Methods-style reporting. | Dynamics figure package; WT-vs-L99A Methods package |

See `docs/benchmark/README.md` for the task table, submission contract,
scorer runtime, and detailed validation commands.

## Developer Quickstart

```bash
conda env create -f environment.yml
conda activate mdclaw
pip install -e .
ruff check mdclaw/
pytest tests/test_mcp_server.py tests/test_cli.py tests/test_guardrails.py tests/test_slurm_server.py -v
```

Short agent guidance is mirrored in `CLAUDE.md` and `AGENTS.md`; keep those
files identical. Long-form references:

- `docs/developer/architecture.md`
- `docs/developer/tool-reference.md`
- `docs/developer/cli-internals.md`
- `docs/developer/testing.md`
- `docs/developer/configuration.md`
- `docs/developer/container.md`
- `docs/developer/release.md`

## Release

Follow `docs/developer/release.md`. Version tags must stay synchronized across
the Python package, plugin metadata, marketplace metadata, and container image.

Users update the plugin with:

```text
/plugin update mdclaw@mdclaw
```

## License

MIT

## Citations

- Boltz-2: S. Passaro et al., bioRxiv (2025). doi:10.1101/2025.06.14.659707
- AmberTools: D. A. Case et al., J. Chem. Inf. Model. 63, 6183 (2023).
- OpenMM: P. Eastman et al., J. Phys. Chem. B 128, 109 (2024).
