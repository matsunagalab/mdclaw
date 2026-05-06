# CLAUDE.md

This file is the always-read agent guide for MDClaw. Keep it short. Put long
reference material under `docs/developer/` and link to it from here.

## Project Overview

MDClaw is an AI-assisted system for building Amber/OpenMM molecular dynamics
workflows. It combines:

- `mdclaw <tool>` CLI tools for concrete MD operations.
- `skills/*/SKILL.md` runbooks for workflow orchestration.
- Boltz-2 for AI structure prediction.
- AmberTools for preparation, topology, and parameterization.
- OpenMM for equilibration and production MD.

## Where Things Live

- `mdclaw/`: Python package and CLI tool implementations.
- `skills/`: platform-agnostic workflow guidance.
- `.claude/commands/`: local development slash-command wrappers.
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
/md-prepare -> /md-equilibration -> /md-production -> /md-analyze
```

Core schema v3 rules:

- `skill = what to run`; skills orchestrate only and do not mutate state.
- `tool = run + record`; tools call `_node.py` helpers to update state.
- One `job_dir` represents one physical system with exactly one `source` root.
- Branch variants from `prep`, `solv`, `topo`, `eq`, or `prod`.
- `eq` accepts both `topo` and prior `eq` parents (multi-stage equilibration
  chains, e.g. NPT compress → NVT thermalize → NPT relax).
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
