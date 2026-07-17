# MDClaw Developer Architecture

MDClaw provides skills and CLIs for vibe-MD simulations and autonomous
scientific investigation. The skills turn scientific intent into MD actions,
the Python tool modules do the work, and the node DAG records what actually
happened.

## Mental Model

| Layer | Responsibility | Main Files |
|---|---|---|
| Skill layer | Agent-facing MD decision policy and procedures | `skills/`, `.agents/skills/`, `.claude/skills/` |
| CLI and dispatch | Parse command-line calls, discover tools, inject node context | `bin/mdclaw`, `mdclaw/_cli.py`, `mdclaw/_registry.py` |
| Tool execution | Fetch structures, prepare systems, build OpenMM XML, run MD, analyze output | `mdclaw/<tool>/` packages |
| State and evidence | Record node status, artifacts, events, and reports | `mdclaw/_node.py`, `mdclaw/_event.py`, `mdclaw/evidence/` |
| Distribution | Package skills and runtime for users | `.claude-plugin/`, `hooks/`, `container/`, `scripts/` |

The key design split is:

- **Skills translate scientific intent into tool choices.**
- **Tools run it and record state.**
- **The DAG is the source of truth for workflow progress.**
- **Study planning records the scientific question, MD goal, planned jobs,
  analysis intent, and decision criteria without replacing per-job DAG state.**

Deployment details live in `docs/agents/deployment.md`.

## Request Path

A normal skill-driven request follows this path:

```mermaid
flowchart LR
  user[User prompt] --> agent[Agent]
  agent --> skill[skills/*/SKILL.md]
  skill --> cli[mdclaw CLI]
  cli --> registry[_registry.py]
  registry --> tool[mdclaw/&lt;tool&gt;/ package]
  tool --> node[_node.py state helpers]
  tool --> artifacts[node artifacts]
  node --> progress[progress.json index]
  node --> events[events/*.json]
```

Important boundaries:

- `skills/*/SKILL.md` should contain scientific decision policy and tool-use
  procedure, not hidden state mutation logic.
- `_cli.py` is the common entry point for direct users and agents.
- `_registry.py` maps public tool names to `mdclaw/<tool>/` packages.
- Workflow tools receive `job_dir` and `node_id`, then call `begin_node`,
  `complete_node`, or `fail_node`.
- `progress.json` is a thin index. Each node owns its durable details in
  `nodes/<node_id>/node.json` and `nodes/<node_id>/artifacts/`.

## Repository Map

| Path | Role |
|---|---|
| `skills/` | Source-of-truth MDClaw skills. |
| `.agents/skills/` | Generic Agent Skills discovery surface, normally symlinked to `skills/`. |
| `.claude/skills/` | Repo-local Claude Code skill discovery surface, normally symlinked to `skills/`. |
| `.claude-plugin/` | Claude plugin marketplace metadata. |
| `hooks/` | Plugin lifecycle hooks, including packaged runtime setup. |
| `bin/mdclaw` | Runtime wrapper that selects conda, SIF, Docker, or local CLI. |
| `mdclaw/` | Python package: CLI dispatch, one `mdclaw/<tool>/` package per tool server, and shared state helpers. |
| `container/` | Docker image and Singularity/Apptainer SIF build assets for the packaged MD runtime. |
| `scripts/` | Setup, doctor, release, and maintenance scripts. |
| `benchmarks/mdprepbench/` | Preparation benchmark prompts, scorer-only metadata, and truth artifacts. |
| `benchmarks/mdstudybench/` | Scientific-study benchmark prompts, scorer-only metadata, and truth artifacts. |
| `docs/` | User, agent, developer, benchmark, and research documentation. |
| `tests/` | Unit, smoke, benchmark scorer, and integration tests. |

## Core Python Modules

| Module | Responsibility |
|---|---|
| `_common.py` | Logging, directories, command wrappers, guardrails, shared helpers. |
| `_registry.py` | Server registry used by CLI discovery. |
| `_cli.py` | CLI entry point, JSON input handling, global `--job-dir` / `--node-id` injection. |
| `_node.py` | Re-export shim for the schema v3 node DAG API; the implementation lives in the `node/` package (`lifecycle`, `graph`, `progress`, `inputs`, `failure`, ...). |
| `_event.py` | Append-only JSON event log. |
| `_lock.py` | File-based locking with `fcntl.flock`. |
| `<tool>/` | Public tool packages (e.g. `structure/`, `solvation/`, `amber/`, `simulation/`, `study/`, `evidence/`). Each package `__init__.py` assembles a `TOOLS` dict from responsibility-scoped submodules. |

## Job DAG

The study layer is the normal outer record for every MD workflow. A simple
one-system request is still represented as a study with one job, usually
`jobs/main`, and it still has a minimal `study_plan.json`. Broader
investigations register multiple job DAGs under the same study. See
[Design Decision: study-first even for single-system runs](#design-decision-study-first-even-for-single-system-runs)
for why this holds even when it adds a step for the simple case.

Node type names are short on disk and in the CLI, while design discussions use
the formal names from `CONTEXT.md`:

| CLI / Disk Type | Formal Name | Usual Product |
|---|---|---|
| `source` | Source Node | Source Bundle |
| `prep` | Preparation Node | Prepared System |
| `solv` | Solvation Node | Solvated System |
| `topo` | Topology Node | Topology |
| `min` | Minimization Node | Minimized state artifacts |
| `eq` | Equilibration Node | Equilibrated state artifacts |
| `prod` | Production Node | Production Segment artifacts |
| `analyze` | Analysis Node | Analysis evidence artifacts |

Inside a job, the `source` node acquires structural input, records a structural
source bundle, and normalizes downstream-selectable candidates. The required
execution contract is `source_bundle.json` plus normalized
`artifacts/candidates/candidate_*.pdb|cif` files. Raw input files may also be
kept for provenance, but `prep` always selects one candidate file before
producing an MD-ready physical system. Candidate files can come from ordinary
single structures, NMR models split out of a multi-model PDB/mmCIF, PDB
assembly/chain choices, or generated prediction ensemble members from
Boltz/BioEm-like tools. Generator-specific rank and confidence data live on
the relevant candidate records and are surfaced through `list_source_candidates`.
Variants then branch from `prep`, `solv`, `topo`, `min`, `eq`, or `prod`.

Preparation nodes select one candidate, choose MD-relevant molecular
components, clean and standardize them, record chemistry and provenance for
topology building, and produce a prepared system. They do not create explicit
solvent boxes, apply force fields, or run MD protocols.

Topology nodes own force-field, template, and parameter resolution. Preparation
nodes provide chemistry and provenance materials such as ligand chemistry,
disulfide records, component disposition, and chain identity; topology nodes
turn those materials into an MD-ready topology artifact contract.

Minimization nodes own post-topology coordinate relaxation before MD
equilibration. They consume the topology node's OpenMM XML triple and write a
portable minimized `state` plus `minimized_structure.pdb` and
`minimization_report.json`. Equilibration nodes should parent from `min` by
default; direct `topo -> eq` remains only a compatibility fallback.

Production continuations are represented as new Production Nodes in the same
Production Chain. The timeline metadata continues from the selected ancestor,
but each Production Segment writes its own node-owned artifacts.

Analysis nodes declare an Analysis Data Scope (`segment`, `production_chain`, or
`comparison`) so a single production parent can mean the parent Production
Segment or the full Production Chain, and a comparison node consumes exactly two
analyze parents. The field-level condition contract — allowed values,
`analysis_subjects` / `comparison_mapping` ownership, the `residue_number` and
`atom_selection` mapping formats, and the pre-execution validation rules — lives
in [Analysis Node Condition Contract](analysis-node-contract.md). The rationale
for requiring explicit cross-topology mappings is
[ADR 0003](../adr/0003-cross-topology-analysis-requires-explicit-mapping.md).

```mermaid
flowchart LR
  source[source_001<br/>source bundle] --> prep[prep_001<br/>select / clean / merge / params]
  prep --> solv[solv_001<br/>water / membrane]
  solv --> topo[topo_001<br/>OpenMM XML triple]
  topo --> min1[min_001<br/>standalone minimization]
  min1 --> eq1[eq_001<br/>equilibration]
  eq1 --> prod1[prod_001<br/>production]
  eq1 --> eq2[eq_002<br/>staged or branched eq]
  eq2 --> prod2[prod_002<br/>production branch]
  prep --> prep2[prep_002<br/>mutation / PTM]
  prep2 --> solv2[solv_002<br/>variant solvation]
  solv2 --> topo2[topo_002<br/>variant topology]
```

Node artifacts are intentionally local to each node:

| Node Type | Typical Artifacts |
|---|---|
| `source` | `source_bundle.json`, normalized `candidates/candidate_*` files, optional raw downloaded/copied/generated structures, source metadata, optional `inspection.json`. |
| `prep` | `source_selection.json`, cleaned/merged PDB, `chain_identity_map.json`, `ligand_chemistry.json`, `residue_mapping.json`, branch-specific prepared structures. |
| `solv` | `solvated.pdb`, `box_dimensions.json`, membrane metadata when applicable. |
| `topo` | `system.system.xml`, `system.topology.pdb`, `system.state.xml`, force-field provenance. |
| `min` | `minimized_structure.pdb`, `minimized.xml`, `minimization_report.json`. |
| `eq` | `equilibrated.pdb`, `equilibrated.xml`, `equilibrated.chk`, stage logs. |
| `prod` | `trajectory.dcd`, `final_structure.pdb`, `state.xml`, `checkpoint.chk`, `energy.dat`. |

The canonical study layout is:

```text
study_XXXXXXXX/
  study.json
  study_plan.json
  plans/
    extension_or_revision.json
  jobs/
    main/
      progress.json
      nodes/
      events/
  annotations/
  evidence/
```

Use `mdclaw bootstrap_md_workflow` for the common one-job case. It creates or
reuses `study.json`, records the active plan, registers `jobs/main`, and writes
the job-level `progress.json` params that connect the job DAG back to the study
and plan.

Inside each job, the DAG shape is:

```text
jobs/main/
  progress.json
  progress.lock
  nodes/
    source_001/
      node.json
      node.lock
      artifacts/
        source_bundle.json
        1AKE.cif
        candidates/
          candidate_001.cif
          candidate_002.cif
    prep_001/
      node.json
      node.lock
      artifacts/
        source_selection.json
    solv_001/
      node.json
      node.lock
      artifacts/
    topo_001/
      node.json
      node.lock
      artifacts/
    min_001/
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
- New equilibration DAGs should use `topo -> min -> eq`; `eq` can still accept
  `topo` directly for legacy records, but skills should not create that shape.
- Terminal (`completed` or `failed`) node.json records are sealed. Create a new
  node for further work; later scheduler observations belong in events.
- Agent claims and open needs are work-routing hints for unfinished nodes.
  Completion clears those operational hints before sealing the node.
- Events are append-only files, not a shared JSON array.
- Broken or unsupported chemistry should surface as structured errors rather
  than silent best-effort topology builds.

## Orchestration For Weak Agents

The DAG is designed so an agent can resume from durable evidence instead of a
separate next-step planner. Five additive helpers carry that load:

- `inspect_job(job_dir)` reads `progress.json` and returns node statuses,
  leaves, claims, open needs, warnings, and workflow params such as
  `solvent_regime`.
- `wait_node(job_dir, node_id)` polls a long-running node until it reaches a
  terminal status. It is read-only and exists to prevent premature packaging or
  duplicate retry branches while a node is still `running`.
- `explain_node(job_dir, node_id)` validates a candidate node before execution
  and reports `ready_to_run`, resolved inputs, missing inputs, parent status, and
  blocking codes.
- `create_node` auto-resolves the canonical forward parent when
  `parent_node_ids` is omitted (single completed leaf of the preferred parent
  type), so example ids never need to be copied. In canonical study jobs,
  ambiguous or empty frontiers return `node_context_required` with parent
  candidates before creating a node. Bare job directories retain parent-less
  creation for low-level repair and tests.
- `trace_failure(job_dir, node_id)` / `explain_failure(job_dir, node_id)` reads
  a failed node, failure artifacts, events, parent/dependency status, and
  existing workflow recommendations. It returns read-only `recovery_options`
  and `next_commands`; it never creates branches automatically.

These are orchestration aids only; the study plan records scientific intent,
and `node.json` + `progress.json` remain the execution source of truth.

Failed nodes keep the core record small: `node.json.metadata.errors`, optional
`metadata.failure_code`, and `artifacts.failure` pointing to
`artifacts/failure/latest/failure_manifest.json`. Full diagnostic evidence
(`tool_result.json`, stderr/stdout tails when available, SLURM log tails from
`check_job`, and tracebacks for unhandled exceptions) belongs under that
failure artifact directory, not in `progress.json`.

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
  resolve --> ligand[load ligand chemistry]
  ligand --> pablo[Pablo topology load]
  pablo --> modeller[Modeller preparation]
  modeller --> system[SystemGenerator createSystem]
  system --> minimize[initial minimization]
  minimize --> serialize[serialize XML triple]
  serialize --> provenance[record provenance]
```

Stages recorded under `topo_NNN/metadata.topology_build_stage_history` include:

```text
resolve_forcefield_xml -> topology_input_ready ->
load_ligand_molecules -> pablo_load -> system_generator_init ->
modeller_prepare -> system_generator_create_system -> initial_minimization ->
serialization -> collect_provenance -> completed
```

The `initial_minimization` stage is a short topology-time initial relaxation
(at most 10 minimizer iterations by default): it validates the
force-field-applied system and writes the initial `state.xml`. Its report uses
`scope="topology_initial_relaxation"` and
`satisfies_min_node_contract=false`; it does not satisfy the schema-v3 `min`
node contract. The separate `min` node owns post-topology minimization and
creates the minimized restart state consumed by `eq`.

Standard ligand records are loaded from `ligand_chemistry` into OpenFF
Molecules. Ligand formal charge is taken from the charged molecule graph.
Topology assigns ligand partial charges with OpenFF NAGL first, then passes the
precharged molecules into `SystemGenerator` / `GAFFTemplateGenerator`; AM1-BCC
is the fallback when NAGL is unavailable or fails. The prep-to-topology ligand
handoff is the `ligand_chemistry` artifact.

`build_openmm_system` is the research escape hatch for explicit custom OpenMM
XML. It emits the same XML triple, so downstream `eq` and `prod` nodes consume
both builders through the same contract.

## Study Directories

Use a `study_dir` for every new scientific question. For a single ordinary MD
run, register one job such as `jobs/main`. When the question spans multiple
systems, such as WT versus mutant or apo versus holo, register multiple
independent `job_dir`s under the same study.

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
  study_plan.json
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

The `mdclaw/study/` package manages the study index and lightweight study plans. It does
not execute OpenMM or mutate node DAG semantics. Each registered job owns its
node DAG and source bundle; the study records cross-job intent, roles,
decisions, planned analyses, and evidence.

`study_plan.json` is intentionally small and weak-agent friendly. It records
the minimum needed to reconnect results to intent:

```json
{
  "question": "scientific question",
  "md_goal": "what MD should test",
  "jobs": [{"job_id": "main", "purpose": "why this job exists"}],
  "analysis": ["observables to inspect"],
  "decision": {
    "support": "what would support the question",
    "against": "what would argue against it",
    "inconclusive": "what would leave it unresolved"
  }
}
```

For clear one-system requests such as "simulate 1AKE chain A", the direct path
through `md-prepare` remains valid, but it still bootstraps the canonical study
layout and records a minimal `study_plan.json` before creating DAG nodes.

### Design Decision: study-first even for single-system runs

There is a real tension between two stated goals. MDClaw wants to be
weak-agent-friendly and reduce agent steps, yet it also requires that *every*
workflow — including an unambiguous single-system request — begin by creating a
`study.json` + `study_plan.json` + `jobs/main` layout before the first `prep`.
Inserting a study-bootstrap step ahead of the actual scientific work adds
handling for the simple case, which pulls against "fewer steps".

This is resolved deliberately in favor of **provenance and structural
consistency over minimizing steps**, for the following reasons:

- **One layout, one re-entry contract.** `inspect_job`, `trace_failure`,
  `explain_node`, and the evidence/report tools all assume the study/job/node
  layout exists. Allowing a "bare run" special case would fork every read and
  recovery path into "study present" vs "study absent" branches — which raises
  the total cognitive load on weak agents far more than a single bootstrap call
  ever saves. Uniformity is itself the weak-agent affordance.
- **Provenance must not be retrofitted.** The scientific question, MD goal, and
  decision criteria are cheapest to capture at the start and effectively
  impossible to reconstruct faithfully after results exist. A run that begins
  without a study has no home for that intent, so its results are harder to
  interpret and compare later.
- **Simple runs grow into studies.** A one-off "simulate X" frequently becomes
  "now compare wild-type vs mutant" or "add a replicate". Starting inside a
  study means that growth is `add_study_job`, not a migration of an orphaned
  job into a study after the fact.

The mitigation for the step-count cost is `bootstrap_md_workflow`: it collapses
study creation, plan recording, `jobs/main` registration, and job→study linkage
into a single idempotent CLI call (it reuses an existing study/plan rather than
erroring), so the "ceremony" is one command with sensible defaults rather than a
multi-step ritual. The trade-off accepted here is: pay one fixed, automatable
setup call so that *every* downstream read, recovery, and evidence path can
assume a single canonical shape.

Enforcement is intentionally *soft*, at the boundary rather than in the DAG
core. `_node.py` / `mdclaw/node/` stays study-agnostic — a job DAG is
self-contained, and bare `job_dir`s remain valid for tests, repair, and
advanced use. Instead, `create_node` emits a non-blocking `study_context_missing`
signal (a `warnings` entry plus a `study_context` block, and the same warning
seeded onto the `source` node so `inspect_job` surfaces it) when a `source` node
is created in a `job_dir` that is neither linked to a study via job params
(`study_dir` / `study_job_id`) nor under the canonical `<study>/jobs/<job_id>`
layout. This turns a silent convention violation into an actionable, branchable
code without coupling the node layer to the study layer or hard-failing
legitimate bare-job flows. Hard enforcement (an override-gated failure) is
deferred until real deviations are observed.

## Adding Tools

To add a new CLI tool:

1. Add a plain Python function in the relevant `mdclaw/<tool>/` submodule.
2. Re-export it from that package's `__init__.py` and add it to the package `TOOLS` dict.
3. Register a new server in `_registry.py` only if you created a new package.
4. Add focused tests for registration, argument handling, and behavior.
5. Update the relevant `skills/*/SKILL.md` examples if users or agents should
   call the new tool.
6. Update `docs/developer/tool-reference.md` when the public contract changes.

Keep state mutation in tools, not in skills. If a tool participates in the DAG,
make its artifact registration and structured failure codes explicit.
