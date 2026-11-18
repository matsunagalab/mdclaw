# Agent-Orchestrated Molecular Dynamics — Research Notes

**Date written**: 2026-04-19
**Authors**: Yasuhiro Matsunaga (RIKEN) × Claude (Opus 4.7)
**Purpose**: A context-restoration document for continuing the discussion on another machine.
Copy and paste the "Continuation prompt" at the top, and Claude on another machine can pick up the discussion where it left off.

> **Legacy note (2026-05 update)**: When this memo was written, `build_amber_system`
> used `tleap` and output `parm7` / `rst7`. That path has since been
> rewritten by the openmmforcefields-unification refactor, and it now
> outputs the modern artifact triple `system.xml` + `topology.pdb` + `state.xml`
> via `openmmforcefields.SystemGenerator` / `GAFFTemplateGenerator` and OpenFF Pablo.
> The meta-level discussion, such as the DAG / node design, ancestor-based artifact resolution,
> and failure-as-data, largely carries over to the current design, but
> for concrete file names and tool names, the current code (`AGENTS.md`, etc.) is authoritative.

---

## 0. Continuation prompt (for Claude on another machine)

```
Read @docs/research/agent_orchestrated_md.md; I want to continue the discussion.
Last time we got as far as "set up sub-agent × REMD as a
scientific-computing benchmark for agent orchestration".
As the next step, I want to decide together which to start with: (a) pinning down the scope of a minimal prototype / (b) implementing
DAG relaxation Levels 1-2 (role-typing of edges + declarative input resolution) in mdclaw ahead of time /
(c) drafting the paper structure.
```

---

## 1. Context — the problem statement that started this

A discussion observed on social media:

- In ThoughtWorks' Technology Radar, **LangGraph was downgraded from Adapt to Trial**
- The gist of the criticism: "If you hold agent state as a graph structure in a local library,
  that kills both **durable execution** and **deployment to other workflows**. Scalable multi-agent
  systems are impossible from the start."
- More generally: "The strength of LangGraph / LangChain / deepagents is that they manage the agent's
  state themselves, but that very architecture creates **fatal incompetencies for scaling and durable
  execution**. A textbook example of how committing to a specific architecture makes some deployments
  harder."

The starting point of the discussion was: does this criticism also apply to mdclaw (a framework that models MD computations as a DAG)?

---

## 2. Position — mdclaw's DAG is a different thing

**Conclusion**: It is a different kind of rigidity, and the LangGraph criticism only partially applies.

| Aspect | LangGraph type | mdclaw type |
|---|---|---|
| What the graph represents | Agent reasoning and control flow | **Lineage of computational artifacts** |
| Where state lives | In-library memory | **Persisted on the filesystem** (`node.json`, `artifacts/`, `events/`) |
| Adding a node | Code change (graph definition) | Just a `create_node` call (the agent grows it dynamically) |
| Agent | Inside the graph | **Outside the graph** (Claude operates the DAG from outside) |
| Durable execution | Hard | Native (re-entry from disk allows resuming at any point) |

Answers to the 3 points of criticism:

1. **Durable execution**: actually strong (no DB needed; the filesystem is the state)
2. **Deployment to other workflows**: the DAG is just a file tree + JSON, so external tools can read it
3. **Multi-agent scale**: designed so that multiple processes can touch the same `job_dir` via `_lock.py`

---

## 3. Architectural commitments in mdclaw — the truly rigid parts

That said, mdclaw definitely has "architectural commitments" too. Specifically, in 2 places:

### 3.1 The `NODE_TYPES` enumeration — `mdclaw/_node.py:36`

```python
NODE_TYPES = frozenset({"source", "prep", "solv", "topo", "eq", "prod", "analyze"})
```

`create_node` rejects any type not listed here (`_node.py:148-152`). Per-type validation
(source cannot have a parent, prep must have exactly one source ancestor, continue_from is prod-only)
is also hard-coded.

### 3.2 Type-specific logic in `resolve_node_inputs` — `mdclaw/_node.py:680-813`

For each `node_type`, "which artifact of which ancestor is passed under which argument name" branches in if/elif:

```python
elif node_type == "topo":
    v = find_ancestor_artifact(job_dir, node_id, "solv", "solvated_pdb")
    lc = find_ancestor_artifact(job_dir, node_id, "prep", "ligand_chemistry")
    # ...
elif node_type == "eq":
    sx = find_ancestor_artifact(job_dir, node_id, "topo", "system_xml")
    # ...
elif node_type == "prod":
    # prod is special-cased: strict resolution of continued_from + checkpoint fallback from eq/prod ancestors
```

**In other words, "node type = data-flow contract" is bound 1:1.** This is the core of mdclaw's rigidity.

### 3.3 What is easy / what is hard

| Type of change | Work required |
|---|---|
| Parameter variations (temperature, pressure, restraint) | **No code change** (just put them in `conditions`) |
| Adding variants in the same flow | **No code change** (branch with existing types) |
| A new **step type** (fep_window, etc.) | Add to `NODE_TYPES` + `resolve_node_inputs`, add a new tool |
| A new **data-flow shape** (many-to-one, lateral exchange) | Rewrite including the premise of `find_ancestor_artifact` (a BFS that walks up for a single ancestor type) |
| Non-DAG structure (mutual exchange in REMD) | The DAG model must be abandoned |

---

## 4. 5 Levels of DAG relaxation — the foothold for the next research

Addendum (2026-05): rather than putting multiple source roots / multiple physical systems into one
`job_dir`, the direction is to keep `job_dir` as the source-bundle execution
unit and to have a `study_dir` bundle multiple `job_dir`s.
A source bundle can contain multiple candidates, such as NMR models or ensembles of generated structures,
and `prep` selects one and makes it a physical system. This keeps the simplicity of ordinary
MD research while handling agentic campaigns and AI for Science integration
in a higher layer.

> **Question**: Can a minimal extension of the artifact-lineage DAG cover FEP / REMD / ensemble analysis /
> adaptive sampling, without degenerating into a control-flow DAG?

### Level 1: Role-typing of edges (almost free)

Currently `parent_node_ids: list[str]` is an unlabeled set. Simply changing it to
`parents: [{node_id, role}]` gives:

- `analyze` node: receives N prods with `role="sample"`
- FEP difference analysis: receives N fep_windows with `role="lambda_endpoint"`
- bias-source / base-topo can be distinguished

This lays the groundwork for promoting the if/elif in `resolve_node_inputs` to a **type-schema declaration**.

### Level 2: Declarative input resolution (moderate)

Turn the hard-coded branches in `_node.py:680-813` into a form where each node_type **declares its own** inputs:

```python
TOPO_SCHEMA = {
    "pdb_file":        from_ancestor("solv",  "solvated_pdb"),
    "ligand_chemistry": from_ancestor("prep",  "ligand_chemistry", optional=True),
    "box_dimensions":  from_ancestor("solv",  "box_dimensions", loader=json),
}
```

Users can register a new node_type as a plugin **without changing code**. The `input:` block of Snakemake /
Nextflow is a similar idea.

### Level 3: Fan-in aggregators (small to moderate)

Umbrella / FEP / bootstrap analysis falls into place here. What is needed:

- `find_ancestors_of_type(node_id, type)` — a BFS that takes **multiple** ancestors rather than one
- `aggregate_artifacts(nodes, key)` — collect across them

Umbrella WHAM, FEP BAR/MBAR, and even REUS analysis can be written **without breaking the DAG**.

### Level 4: Hierarchical composition (moderate to large; interesting)

A whole FEP experiment = 1 node (seen from outside); internally, a DAG of fep_windows.

- In the outer DAG, simply `fep_experiment → analyze` in series
- Fine-grained internal parallelism does not leak outside
- Equivalent to hermetic execution inside a Bazel `genrule`, or a sub-DAG / nested workflow
- Category-theoretically a **functor** — the operation of collapsing a sub-DAG to a single point of the outer graph

### Level 5: Coupled state like REMD (this is the real research)

Options:

- **(a) Time discretization**: cut a node at every exchange period. N replicas × K exchanges = N×K nodes.
  The graph stays a DAG, with exchange edges crossing between replicas. Cost: node-count explosion.
- **(b) Group node + event stream**: collapse the replica group into 1 node and record exchanges in an append-only log in `events/`.
  The DAG is clean, but **the node internals are opaque**.
- **(c) Synchronization-point nodes**: insert a "barrier" node every M exchanges, with independent sub-DAGs in between.
  An intermediate option that continuously connects (a) and (b) through a granularity parameter.

**(c) looks the most balanced, and being able to choose the granularity freely is interesting for research** (one can draw the trade-off curve of accuracy vs. metadata
cost).

---

## 5. Sub-Agent × REMD research concept — the most important section

> If you think of "1 replica = 1 sub-agent", REMD becomes not an MD problem but
> a **multi-agent orchestration** problem.

### 5.1 The layers where agent judgment matters

- **inner loop** (integration every 1 ps): deterministic, no agent needed
- **meta loop** (from checkpoint units to exchange periods): **all of this is agent work**

Conventional REMD frameworks (GROMACS `-multidir`, OpenMM replica) hard-coded the meta loop too.
**Replacing that with judgment** is the core of the novelty.

### 5.2 The role each agent plays

#### Replica worker agent

- **Local failure recovery**: NaN blow-up → read the recent trajectory → locate the clash →
  restart from a slightly earlier checkpoint + a different seed → report the incident to the coordinator
- **Local quality judgment**: "I ran 5 ns at this temperature but the RMSD does not stabilize;
  I want to weaken the thermostat coupling"
- **Rare-event detection**: monitor CVs in real time: "I am in a rare state now; requesting a fork"
- **GPU / platform switching**: OOM → migrate to another GPU instead of falling back to CPU

#### Coordinator agent

- **Dynamic adjustment of the temperature ladder**: the `rep_005 ↔ rep_006` exchange acceptance rate dropped to 5% →
  spawn an intermediate temperature `rep_005.5` and reconstruct the ladder
- **Mixing heterogeneous enhanced sampling**: rep_000 uses tempering, rep_001 metadynamics,
  rep_002 REST2 — the coordinator judges "REST2 is likely to work for the current system" and changes the configuration
- **Budget management**: reallocate GPU time per replica (from stalled replicas to active ones)
- **Convergence judgment**: not simply time-based, but judging "whether the free energy surface has
  stabilized as an ensemble"

#### Human agent (possible)

An expert intervenes only on specific replicas — without stopping the whole ensemble. This is a usage that was
**structurally impossible** in conventional batch MD.

### 5.3 Architecture sketch

```
job_dir/
  nodes/
    remd_001/                       # outer DAG: the whole ensemble is 1 node
      node.json                     # temperature ladder, index of the exchange log
      replicas/
        rep_000/
          agent_session.jsonl       # agent conversation log for this replica
          artifacts/trajectory.dcd
          events/                   # local incident log
        rep_001/
        ...
      coordinator/
        agent_session.jsonl         # coordinator's reasoning log
        decisions.jsonl             # decision history such as ladder changes
  events/
    exchange_log.jsonl              # global exchange events (append-only)
```

**3-layer separation**:

- **DAG** = artifact lineage (outer)
- **Events** = loosely coupled communication ordered on a time axis (exchange, incident)
- **Filesystem subtree** = each agent's private state

Erlang's supervision tree + actor model is the closest analogue. What is novel, however, is that "each actor is an LLM agent"
and "state is persisted on the filesystem".

### 5.4 The strongest counter to the LangGraph criticism

| Criticism | Answer with mdclaw × multi-agent REMD |
|---|---|
| Holding agent state in a local library graph kills you | Each replica agent has an **independent filesystem subtree**. It can even be moved to another machine |
| Durable execution dies | Each replica can crash & resume individually. Even if the coordinator dies, the workers keep running |
| Multi-agent scale is impossible from the start | N replicas = N processes = N GPUs = N nodes, loosely coupled. Only exchanges are synchronization points |

In other words, it becomes **a scientific-computing use case that can show "multi-agent is flexible and durable" with real data**.
Far more convincing than chat-agent collaboration.

---

## 6. Experiment Proposal — minimal prototype

### Experimental target

- **System**: Trp-cage (a 20-residue mini-protein, a folding benchmark) or alanine dipeptide
- **Comparison**: conventional REMD vs agent-supervised REMD

### Evaluation metrics

- **convergence time**: wall time / GPU hours until the free energy surface converges
- **acceptance rate stability**: temporal variation of the exchange acceptance rate, and automatic recovery when it drops
- **failure recovery rate**: recovery success rate when NaN / crashes are artificially injected
- **sampling efficiency**: conformational space coverage per GPU hour

### Implementation plan

1. **mdclaw extension**: add a `remd_group` node type + a replica sub-agent launcher
2. **coordinator**: Claude Code itself (Opus 4.7)
3. **worker**: a lightweight agent (Haiku-class), dedicated to failure judgment for each replica
4. **communication**: filesystem events (`events/`) + JSONL message log

### Timeline

A prototype for the first paper in 3-6 months.

---

## 7. Related Work Pointers

Prior work to reference directly:

- **W3C PROV model**: `Entity / Activity / Agent` + `wasDerivedFrom` — a straightforward formalization of
  role-labeled edges
- **Pegasus / Nextflow / Snakemake**: declarative I/O binding (the existing solution to Level 2)
- **Bazel / Nix**: pure-function artifact DAG, hash-based reuse
- **CWL (Common Workflow Language)**: patterning of scatter/gather
- **Dagster**: software-defined assets (the idea of viewing a workflow as an asset DAG)
- **AutoGen / CrewAI**: multi-agent but no durable state, centered on chat tasks
- **Papers on automatic ladder optimization for Parallel Tempering**: statistical optimization, no LLM semantic reasoning
- **Erlang supervision tree**: actor model + fault tolerance via process restart

**The bundle of novelty**:

1. An LLM agent as a replica supervisor (the first?)
2. Proposing the 3-layer separation of DAG + events + filesystem as a form of multi-agent durable execution
3. A concrete example of replacing MD meta-decisions with natural-language reasoning
4. Multi-agent as a science benchmark: unlike chat benchmarks, it has **objective metrics**
   (free energy accuracy, sampling efficiency)

---

## 8. Research Questions / Open Points

### Theory

- **Balancing token cost vs MD cost**: if 1 replica runs for days, agent intervention at a granularity of seconds to minutes is
  enough → the cost is negligible. Conversely, for short sweeps the agent overhead matters → the threshold is a research subject
- **The context an agent should have**: the entire past trajectory cannot be passed. How to summarize it
  (can it judge from just the RMSD time series + recent logs?)
- **Scope of the coordinator's authority**: how far to allow autonomous ladder changes, and where to put a human gate

### Engineering

- **failure modes**: detecting pathological patterns, such as an agent running amok and repeatedly killing replicas
- **Reproducibility**: can the results of REMD with agent judgment be reproduced? The agent trace, not just the seed,
  must be included in the artifacts
- **Granularity choice**: which of Level 5's (a)(b)(c) granularities to make standard, or whether to let the user choose

### Paper strategy

It looks like it can be split into 2 papers:

1. **Level 1 + 2 + 3** as the first (implementation + MD case study)
2. **Level 5 (c)** and **Sub-agent × REMD** as the second (theory + experiments + decision-log analysis)

---

## 9. Related files (in mdclaw)

Concrete code locations referenced in the discussion:

- `mdclaw/_node.py:36` — `NODE_TYPES` definition
- `mdclaw/_node.py:121-314` — `create_node` implementation
- `mdclaw/_node.py:605-677` — `find_ancestor_artifact` (BFS search for ancestor artifacts)
- `mdclaw/_node.py:680-813` — `resolve_node_inputs` (type-specific input resolution, hard-coded branches)
- `CLAUDE.md` / `docs/developer/architecture.md` — project overview, explanation of schema v3
- `mdclaw/_event.py` — append-only event log (could be repurposed for the REMD exchange log in the future)
- `mdclaw/_lock.py` — file lock (designed on the premise of multiple processes)
