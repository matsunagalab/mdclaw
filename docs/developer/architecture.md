# MDClaw Developer Architecture

MDClaw is easiest to understand as a small set of layers that meet at the
`mdclaw` CLI. Agent-facing files decide what should happen. Python tool modules
do the work. The node DAG records what actually happened.

## Mental Model

| Layer | Responsibility | Main Files |
|---|---|---|
| Agent guidance | Short runbooks that an agent reads before choosing tools | `skills/`, `.agents/skills/`, `.claude/commands/` |
| CLI and dispatch | Parse command-line calls, discover tools, inject node context | `bin/mdclaw`, `mdclaw/_cli.py`, `mdclaw/_registry.py` |
| Tool execution | Fetch structures, prepare systems, build OpenMM XML, run MD, analyze output | `mdclaw/*_server.py` |
| State and evidence | Record node status, artifacts, events, and reports | `mdclaw/_node.py`, `mdclaw/_event.py`, `mdclaw/evidence_server.py` |
| Distribution | Package skills and runtime for users | `.claude-plugin/`, `hooks/`, `container/`, `scripts/` |

The key design split is:

- **Skills decide what to run.**
- **Tools run it and record state.**
- **The DAG is the source of truth for workflow progress.**

Deployment details live in `docs/agents/deployment.md`.

## Request Path

A normal skill-driven request follows this path:

```mermaid
flowchart LR
  user[User prompt] --> agent[Agent]
  agent --> skill[skills/*/SKILL.md]
  skill --> cli[mdclaw CLI]
  cli --> registry[_registry.py]
  registry --> tool[*_server.py tool]
  tool --> node[_node.py state helpers]
  tool --> artifacts[node artifacts]
  node --> progress[progress.json index]
  node --> events[events/*.json]
```

Important boundaries:

- `skills/*/SKILL.md` should contain orchestration guidance, not hidden state
  mutation logic.
- `_cli.py` is the common entry point for direct users and agents.
- `_registry.py` maps public tool names to `mdclaw/*_server.py` functions.
- Workflow tools receive `job_dir` and `node_id`, then call `begin_node`,
  `complete_node`, or `fail_node`.
- `progress.json` is a thin index. Each node owns its durable details in
  `nodes/<node_id>/node.json` and `nodes/<node_id>/artifacts/`.

## Repository Map

| Path | Role |
|---|---|
| `skills/` | Source-of-truth skill runbooks. |
| `.agents/skills/` | Generic Agent Skills discovery surface, normally symlinked to `skills/`. |
| `.claude/commands/` | Repo-local Claude Code slash-command wrappers for development. |
| `.claude-plugin/` | Claude plugin marketplace metadata. |
| `hooks/` | Plugin lifecycle hooks, including container setup. |
| `bin/mdclaw` | Runtime wrapper that selects conda, SIF, Docker, or local CLI. |
| `mdclaw/` | Python package, CLI dispatch, server tools, state helpers. |
| `container/` | Docker/Singularity build assets. |
| `scripts/` | Setup, doctor, release, and maintenance scripts. |
| `benchmarks/mdagentbench/` | Benchmark prompts, scorer-only metadata, and truth artifacts. |
| `docs/` | User, agent, developer, benchmark, and research documentation. |
| `tests/` | Unit, smoke, benchmark scorer, and integration tests. |

## Core Python Modules

| Module | Responsibility |
|---|---|
| `_common.py` | Logging, directories, command wrappers, guardrails, shared helpers. |
| `_registry.py` | Server registry used by CLI discovery. |
| `_cli.py` | CLI entry point, JSON input handling, global `--job-dir` / `--node-id` injection. |
| `_node.py` | Schema v3 node DAG management, artifact registration, status transitions. |
| `_event.py` | Append-only JSON event log. |
| `_lock.py` | File-based locking with `fcntl.flock`. |
| `_ligand_xml.py` | Converts prep-computed ligand `mol2 + frcmod` pairs into OpenMM ForceField XML. |
| `*_server.py` | Public tool modules. Each exposes a `TOOLS` dict. |

## Job DAG

One `job_dir` represents one physical MD system and has exactly one `source`
root. Scientific variants branch inside that job after preparation,
solvation, topology building, equilibration, or production.

```mermaid
flowchart LR
  source[source_001<br/>structure source] --> prep[prep_001<br/>clean / merge / params]
  prep --> solv[solv_001<br/>water / membrane]
  solv --> topo[topo_001<br/>OpenMM XML triple]
  topo --> eq1[eq_001<br/>equilibration]
  eq1 --> prod1[prod_001<br/>production]
  eq1 --> eq2[eq_002<br/>staged or branched eq]
  eq2 --> prod2[prod_002<br/>production branch]
  prep --> prep2[prep_002<br/>mutation / PTM / modXNA]
  prep2 --> solv2[solv_002<br/>variant solvation]
  solv2 --> topo2[topo_002<br/>variant topology]
```

Node artifacts are intentionally local to each node:

| Node Type | Typical Artifacts |
|---|---|
| `source` | Downloaded or copied structure, source metadata, optional `inspection.json`. |
| `prep` | Cleaned/merged PDB, `ligand_params.json`, `residue_mapping.json`, branch-specific prepared structures. |
| `solv` | `solvated.pdb`, `box_dimensions.json`, membrane metadata when applicable. |
| `topo` | `system.system.xml`, `system.topology.pdb`, `system.state.xml`, force-field provenance. |
| `eq` | `equilibrated.pdb`, `equilibrated.xml`, `equilibrated.chk`, stage logs. |
| `prod` | `trajectory.dcd`, `final_structure.pdb`, `state.xml`, `checkpoint.chk`, `energy.dat`. |

The on-disk shape is:

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
    <ISO8601>_<node_id>_<event_type>.json
```

DAG invariants:

- Parent-child relationships are stored in each node's `parent_node_ids`.
- Workflow nodes require both `job_dir` and `node_id`.
- Tools should auto-resolve inputs from ancestors when that is the documented
  contract.
- A completed topology node must provide the full OpenMM XML triple; run-side
  tools do not fall back to legacy Amber `parm7/rst7`.
- Events are append-only files, not a shared JSON array.
- Broken or unsupported chemistry should surface as structured errors rather
  than silent best-effort topology builds.

## State Files

| File | Purpose |
|---|---|
| `progress.json` | Thin job index: node list, cached summaries, current high-level state. |
| `nodes/<node_id>/node.json` | Authoritative node record: status, parents, artifacts, conditions, metadata. |
| `nodes/<node_id>/node.lock` | Per-node lock for concurrent-safe updates. |
| `nodes/<node_id>/artifacts/` | Tool-owned outputs registered by that node. |
| `events/*.json` | Append-only operational history. |

When debugging, start with the relevant node's `node.json`, then inspect its
registered artifacts and nearby event files. Do not infer workflow state from
loose files in the repository root.

## Topology Build Path

The recommended topology path is `build_amber_system`. It emits the modern
OpenMM triple consumed by equilibration and production:

```text
system.system.xml
system.topology.pdb
system.state.xml
```

The high-level topology pipeline is:

```mermaid
flowchart LR
  prepared[prepared PDB] --> resolve[resolve force-field XML]
  resolve --> ligand[convert ligand XML when possible]
  ligand --> pablo[Pablo topology load]
  pablo --> modeller[Modeller preparation]
  modeller --> system[SystemGenerator createSystem]
  system --> minimize[initial minimization]
  minimize --> serialize[serialize XML triple]
  serialize --> provenance[record provenance]
```

Stages recorded under `topo_NNN/metadata.topology_build_stage_history` include:

```text
resolve_forcefield_xml -> convert_ligand_xml -> pdbfixer_hydrogenation ->
load_ligand_molecules -> pablo_load -> system_generator_init ->
modeller_prepare -> system_generator_create_system -> initial_minimization ->
serialization -> collect_provenance -> completed
```

`convert_ligand_xml` calls `_ligand_xml.convert_amber_ligand_to_openmm_xml`
for ligands with `parameter_source` of `amber_geostd` or
`gaff2_antechamber`. Successful conversions land under
`artifacts/ligand_xml/<RES>.xml` and are appended to the OpenMM force-field
bundle. Conversion failures fall back to the standard GAFF template path with
a warning.

`build_openmm_system` is the research escape hatch for explicit custom OpenMM
XML. It emits the same XML triple, so downstream `eq` and `prod` nodes consume
both builders through the same contract.

## Study Directories

Use a `study_dir` when one scientific question spans multiple physical systems,
such as WT versus mutant or apo versus holo. A study is a thin campaign index
above multiple independent `job_dir`s.

```mermaid
flowchart TB
  study[study_XXXXXXXX] --> wt[jobs/wt<br/>job DAG]
  study --> mut[jobs/mut_v148a<br/>job DAG]
  study --> apo[jobs/apo<br/>job DAG]
  study --> evidence[study-level evidence]
```

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

`study_server.py` manages the study index only. It does not execute OpenMM,
mutate node DAG semantics, or relax the single-source `job_dir` invariant.

## Adding Tools

To add a new CLI tool:

1. Add a plain Python function in the relevant `mdclaw/*_server.py`.
2. Add it to that module's `TOOLS` dict.
3. Register a new server in `_registry.py` only if you created a new module.
4. Add focused tests for registration, argument handling, and behavior.
5. Update the relevant `skills/*/SKILL.md` examples if users or agents should
   call the new tool.
6. Update `docs/developer/tool-reference.md` when the public contract changes.

Keep state mutation in tools, not in skills. If a tool participates in the DAG,
make its artifact registration and structured failure codes explicit.
