# MDClaw Agent Guide

This guide is mirrored as both `CLAUDE.md` and `AGENTS.md`. Keep the two copies
identical. Keep it short. Put long reference material under `docs/developer/`
and link to it from here.

## Project Overview

MDClaw provides skills and CLIs for vibe-MD simulations and autonomous
scientific investigation in the Amber/OpenMM ecosystem. It combines:

- `mdclaw <tool>` CLI tools for concrete MD operations.
- `skills/*/SKILL.md` instructions for translating scientific intent into MD
  actions.
- Boltz-2 for AI structure prediction.
- AmberTools (`pdb4amber`, `cpptraj`) for structure preparation support.
- HPacker for mutation-side-chain reconstruction and surrogate side-chain
  packing.
- `openmmforcefields` (`SystemGenerator` + `GAFFTemplateGenerator`) plus
  topology-time Amber geostd XML conversion for force-field application.
- OpenFF Pablo for chemistry-aware PDB → topology loading (CCD-based).
- OpenMM for equilibration and production MD.

`build_amber_system` and `build_openmm_system` emit a `system.xml` +
`topology.pdb` + `state.xml` artifact triple — the only topology
contract on the run side. min / eq / prod / analyze consume that triple via
the DAG resolver; the run side never reconstructs a System from
ForceField XML.

## Where Things Live

- `mdclaw/`: Python package and CLI tool implementations.
- `skills/`: platform-agnostic MDClaw skill instructions.
- `.agents/skills/`, `.claude/skills/`: skill discovery mirrors of `skills/`.
- `.claude-plugin/`, `bin/`, `hooks/`: plugin distribution and runtime wrapper.
- `tests/`: unit, smoke, and pipeline tests.
- `docs/developer/`: long-form developer references.
- `docs/research/`: research notes and citation inventory.
- `examples/`: runnable skeletons.

Developer references:

- `docs/developer/architecture.md`: repository map and schema v3 DAG details.
- `docs/developer/tool-reference.md`: tool modules and signatures index.
- `docs/developer/cli-internals.md`: CLI discovery, argument mapping, guardrails.
- `docs/developer/testing.md`: test levels and commands.
- `docs/developer/configuration.md`: environment variables and CLI basics.
- `docs/developer/container.md`: Docker, GHCR, and Singularity notes.
- `docs/developer/release.md`: version sync and release steps.
- `docs/developer/roadmap-and-known-issues.md`: known issues and deferred work.

## Development Defaults

Use the `mdclaw` conda environment for linting and tests:

```bash
conda run -n mdclaw ruff check mdclaw/
conda run -n mdclaw pytest tests/test_mcp_server.py tests/test_cli.py tests/test_guardrails.py tests/test_slurm_server.py -v
```

If touching tool execution paths, also run the relevant smoke or pipeline tests
from `docs/developer/testing.md`.

## Adding Or Changing Tools

1. Add or update the function in the appropriate `mdclaw/*_server.py` file.
2. Register it in that module's `TOOLS` dict.
3. If adding a server, register it in `mdclaw/_registry.py` and
   `mdclaw/__init__.py`.
4. Add or update unit tests and smoke tests.
5. Run `conda run -n mdclaw mdclaw --list` to verify CLI discovery.
6. Update `docs/developer/tool-reference.md` and affected `skills/*` examples.

## Skill Workflow Invariants

User-facing sequence:

```text
md-prepare -> md-equilibration -> md-production -> md-analyze
```

Core schema v3 rules:

- `skill = scientific intent and procedure`; skills do not mutate state.
- `md-study` is the study-planning skill: it translates scientific questions
  into a small MD goal, planned jobs, analysis intent, and decision criteria.
  Clear single-system requests such as "simulate 1AKE chain A" still flow
  directly through `md-prepare` with a simple `jobs/main` study.
- `tool = run + record`; tools call `_node.py` helpers to update state.
- `plan_next` (read-only) is the weak-agent orchestration entry point: it
 returns the next node type, tool, concrete parent ids, `solvent_regime`, and
 `next_skill`. Skills follow `skills/common/run-loop.md`:
 `plan_next -> create_node -> run tool`.
- `create_node` auto-resolves the canonical forward parent when
 `--parent-node-ids` is omitted (single completed frontier leaf), and the CLI
 appends a `workflow_hint` to successful workflow tools. CLI preflight failures
 return structured `code`s (`node_context_required`, etc.).
- New scientific work should start with a `study_dir`; a simple one-system MD
  run is a study with one job, usually `jobs/main`.
- A study may index many `job_dir`s. Each job DAG has one `source` node, and
  that source node records a structural source bundle.
- A source bundle may contain multiple structures, such as NMR models,
  assembly candidates, or generated prediction ensemble members.
- The source node normalizes those inputs into
  `artifacts/candidates/candidate_*` files plus `source_bundle.json`; optional
  raw inputs are provenance only.
- Use `list_source_candidates` to expose candidate IDs, ranks, generator
  metadata, and confidence metrics to the agent/user.
- `prep` selects one concrete candidate from the source bundle before creating
  an MD-ready physical system.
- Branch variants from `prep`, `solv`, `topo`, `min`, `eq`, or `prod` after that
  concrete structure has been prepared.
- `min` owns post-topology coordinate minimization and writes the portable
  minimized `state` consumed by `eq`.
- `eq` accepts `min` parents by default, with `topo` accepted only for legacy
  DAGs and prior `eq` parents for multi-stage equilibration chains
  (e.g. NPT compress -> NVT thermalize -> NPT relax).
- Each node owns `node.json`, `node.lock`, and `artifacts/`.
- `progress.json` is a thin index plus cached summaries.
- Events are append-only JSON files in `events/`.
- Workflow tools require both `--job-dir` and `--node-id`.

See `docs/developer/architecture.md` for the full job and study directory
contracts.

## Important Code Contracts

- `_node.py` is the source of truth for DAG resolution, locking, status, and
  progress synchronization. Refactor it only with focused tests.
- `run_production` and `run_equilibration` both prefer portable XML state over
  binary checkpoints for restart, and use `metadata.final_step` to restore
  timeline metadata. The ensemble-agnostic loader (`_load_state_into_simulation`
  in `mdclaw/md_simulation_server.py`) transfers positions / velocities / box
  via `XmlSerializer.deserialize` without restoring Context parameters, so
  NPT ↔ NVT switching is safe across nodes (barostat parameters in the saved
  state are dropped or introduced as the new System requires).
- `build_amber_system` guardrails are part of the public agent contract; branch
  on stable `code` values rather than human messages.
- Skills invoke tools through the CLI. When tool signatures change, update the
  matching skill examples.

## Release And Distribution

Keep the Python package version, plugin metadata, and container tag in sync.
Follow `docs/developer/release.md` and `docs/developer/container.md`.
