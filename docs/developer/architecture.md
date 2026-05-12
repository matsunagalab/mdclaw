# MDClaw Developer Architecture

MDClaw has three layers:

- `skills/`: platform-agnostic workflow guidance used by agents.
- `mdclaw/`: Python tool implementations exposed through the `mdclaw` CLI.
- `tests/`: unit, smoke, and integration tests that lock the tool contracts.

## Repository Map

```text
skills/                  Domain runbooks and skill routers
.claude/commands/        Local slash-command wrappers for development
.claude-plugin/          Plugin marketplace metadata
bin/mdclaw               Plugin CLI wrapper, delegates to SIF or local install
hooks/                   Plugin lifecycle hooks
examples/                Lightweight usage skeletons
scripts/                 Setup and maintenance scripts
mdclaw/                  Python package and CLI tools
container/               Docker/Singularity build assets
docs/                    User, developer, benchmark, and research docs
tests/                   Four-level test suite
```

## Core Python Modules

- `_common.py`: logging, directories, command wrappers, guardrails, shared helpers.
- `_registry.py`: server registry used by CLI discovery.
- `_lock.py`: file-based locking.
- `_event.py`: append-only event log.
- `_node.py`: schema v3 job DAG and node state management.
- `_cli.py`: CLI entry point and global `--job-dir` / `--node-id` injection.
- `_ligand_xml.py`: ParmEd-based bridge that bakes prep-computed ligand
  `mol2 + frcmod` pairs into self-contained OpenMM ForceField XML files,
  stacked under `openmmforcefields`'s shipped `gaff-2.2.20.xml`. Lets
  `build_amber_system` skip the `GAFFTemplateGenerator` AM1-BCC path for
  ligands that already carry curated charges and frcmod additions.
- `*_server.py`: tool modules; each exposes a `TOOLS` dict.

## Topology-build Pipeline (`build_amber_system`)

Stages recorded under `topo_NNN/metadata.topology_build_stage_history`:

```text
resolve_forcefield_xml -> convert_ligand_xml -> pdbfixer_hydrogenation ->
load_ligand_molecules -> pablo_load -> system_generator_init ->
modeller_prepare -> system_generator_create_system -> initial_minimization ->
serialization -> collect_provenance -> completed
```

`convert_ligand_xml` calls `_ligand_xml.convert_amber_ligand_to_openmm_xml`
for every ligand with `parameter_source ∈ {"amber_geostd",
"gaff2_antechamber"}`. Successful conversions land under
`artifacts/ligand_xml/<RES>.xml` and are appended to the
`SystemGenerator(forcefields=...)` bundle via
`forcefield_catalog.resolve_xml_bundle(gaff_base="gaff-2.2.20",
extra_xml=[...])`. Converted ligands are removed from
`SystemGenerator(molecules=...)` so `GAFFTemplateGenerator` is bypassed.
Per-ligand conversion failures fall back to the legacy GAFF path with a
warning.

## Schema v3 DAG Invariants

One `job_dir` represents one physical MD system and has exactly one `source`
root. Variants branch after `prep`, `solv`, `topo`, `eq`, or `prod`.

```text
job_XXXXXXXX/
  progress.json
  progress.lock
  nodes/
    source_001/
      node.json
      node.lock
      artifacts/
    prep_001/
      node.json
      node.lock
      artifacts/
    solv_001/
      node.json
      node.lock
      artifacts/
    topo_001/
      node.json
      node.lock
      artifacts/
    eq_001/
      node.json
      node.lock
      artifacts/
    prod_001/
      node.json
      node.lock
      artifacts/
  events/
```

Design rules:

- Skills decide what to run; tools execute and mutate state.
- Each node owns its own `node.json`, lock, and `artifacts/`.
- Parent-child relationships form a DAG through `parent_node_ids`.
- `progress.json` is a thin index plus cached summaries.
- Events are append-only JSON files under `events/`.
- Workflow tools receive both `job_dir` and `node_id`, then call
  `begin_node`, `complete_node`, or `fail_node`.

## Optional Study Directories

Use a `study_dir` above multiple jobs when a scientific question spans multiple
physical systems.

```text
study_XXXXXXXX/
  study.json
  decisions.jsonl
  question_history.jsonl
  token_ledger.jsonl
  annotations/
  evidence/
  jobs/
    wt/
      progress.json
      nodes/source_001/...
    mut_v148a/
      progress.json
      nodes/source_001/...
```

`study_server.py` manages this index only. It does not execute OpenMM, mutate
node DAG semantics, or relax the single-source `job_dir` invariant.
