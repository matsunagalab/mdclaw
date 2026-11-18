# MDClaw and ScienceClaw Integration Plan

Date written: 2026-05-01

Addendum (2026-09-06): What follows is the concept as of that time. The old report CLI has been
merged into `generate_md_report`. For the current specification, see the
[tool reference](../developer/tool-reference.md#evidence).

## Purpose

Develop MDClaw into an "atomic-level evidence provider" that ScienceClaw and cell-simulator foundation model agents can call naturally.

The key policy here is not to force MDClaw's internal DAG to be compatible with ScienceClaw's DAG. What ScienceClaw needs to see is not MDClaw's internal execution plan; the following two things are enough.

- Input: what to run MD on, and for what scientific question
- Output: what was learned from that MD

MDClaw's internal `source -> prep -> solv -> topo -> eq -> prod -> analyze` DAG is kept as an implementation detail that lets MDClaw run MD correctly and resumably, and leave provenance.

## Basic concept

The ScienceClaw / cell-simulator side uses MDClaw not as a "trajectory generator" but as an "atomistic evidence generator that can feed a cell model".

```text
ScienceClaw / Cell Simulator Agent
  └─ asks biological or mechanistic question
       ↓
MDClaw Study Layer
  └─ turns question into MD workflow
       ↓
MDClaw Execution Layer
  └─ runs source/prep/solv/topo/eq/prod/analyze DAG
       ↓
MDClaw Evidence Layer
  └─ returns natural-language summary + typed evidence
       ↓
ScienceClaw / Cell Simulator Agent
  └─ updates hypothesis, pathway model, or cell-scale parameters
```

## Three layers

### 1. Execution Layer

The current MDClaw DAG. Its role is to "run MD correctly, resumably, and with records".

Main existing components:

- `mdclaw/_node.py`
- `create_node`
- `resolve_node_inputs`
- `run_equilibration`
- `run_production`
- `node.json`
- `events/`
- `artifacts/`

This layer is kept as a low-level API. ScienceClaw and cell foundation model agents, as a rule, need not understand this internal structure directly.

### 2. Study Layer

A newly needed layer. It converts the scientific questions of external agents and users into MDClaw execution DAGs.

Example: a request from outside

```json
{
  "study_type": "mutation_stability",
  "target": {
    "uniprot_id": "P12345",
    "mutation": "V148A"
  },
  "comparison": "WT_vs_mutant",
  "budget": {
    "simulation_time_ns": 20,
    "replicates": 3
  }
}
```

Example of the jobs the Study Layer bundles internally:

```text
WT:
  job_wt/source_001 -> prep_001 -> solv_001 -> topo_001 -> eq_001 -> prod_rep1/2/3

Mutant:
  job_mut/source_001 -> prep_001 -> mutate_V148A -> solv_001 -> topo_001 -> eq_001 -> prod_rep1/2/3

Analysis:
  study/evidence/study_evidence_report.json
```

Important: do not create multiple `source` roots inside a `job_dir`. Keep a `job_dir` as the durable execution unit of one physical
system; comparisons across multiple sources / multiple systems are expressed by an optional `study_dir`
that bundles multiple `job_dir`s.

Implementation candidate:

```text
mdclaw/study_server.py
```

Example of the low-level study-index tools to put in place first:

```python
init_study(study_dir, title=None, objective=None)
add_study_job(study_dir, job_id, job_dir, role=None)
record_study_decision(study_dir, phase, decision, reason)
summarize_study(study_dir)
```

The Study Layer does not touch OpenMM directly; it combines the existing execution tools and `job_dir`
DAGs. High-level autonomous workflows such as mutation stability are
run by skills / harnesses / external agents on top of
this thin study-index contract.

Priority study types:

- `stability_screen`
- `mutation_effect_study`
- `binding_site_flexibility_study`
- `protein_interface_stability_study`
- `apo_vs_holo_study`
- `wt_vs_mutant_comparison`

### 3. Evidence Layer

The main target of external integration. It converts raw MD output into evidence reports that ScienceClaw and cell models can use.

An evidence report carries both a natural-language summary and typed metrics. Natural language alone is readable by humans but hard for external agents to use for comparison, ranking, and model updates.

Example:

```json
{
  "evidence_type": "mutation_stability_effect",
  "target": {
    "uniprot_id": "P12345",
    "mutation": "V148A"
  },
  "summary": "V148A shows moderately increased flexibility near the active-site loop compared with WT.",
  "effect": {
    "direction": "destabilizing",
    "magnitude": "moderate",
    "confidence": "medium"
  },
  "metrics": {
    "wt_rmsd_mean_nm": 0.31,
    "mutant_rmsd_mean_nm": 0.44,
    "delta_rmsd_nm": 0.13,
    "active_site_delta_rmsf_nm": 0.06,
    "replicates": 3,
    "simulation_time_ns_per_replica": 20
  },
  "model_parameter_hints": {
    "active_fraction_change": "decrease",
    "protein_stability_effect": "lower",
    "suggested_cell_model_update": {
      "parameter": "degradation_rate",
      "direction": "increase",
      "confidence": "low_to_medium"
    }
  },
  "limitations": [
    "Short MD screen; not a free-energy calculation",
    "Structure source was predicted, not experimental"
  ],
  "provenance": {
    "mdclaw_job_dir": "...",
    "nodes": ["prod_001", "prod_002", "analyze_001"]
  }
}
```

Implementation candidates:

```text
mdclaw/evidence_server.py
mdclaw/evidence_schema.py
```

Tool example:

```python
def generate_md_evidence_report(
    job_dir: str,
    study_node_id: str | None = None,
    report_type: str = "mutation_stability",
) -> dict:
    ...
```

## MDClaw as seen from external agents

For cell-simulator foundation model agents, MDClaw becomes a specialist tool that answers questions such as:

- Does this mutation stabilize/destabilize the protein?
- How do the populations of the active/inactive forms change?
- Is ligand binding likely to strengthen or weaken?
- Is the protein-protein interface likely to break?
- Is it likely to affect gating / transport of a membrane protein?
- Can it be reflected in the rate / affinity / state transition parameters of a pathway model?

What a cell model wants is not the trajectory itself but coarse-grained evidence and parameter hints extracted from MD.

## Implementation order

### Step 1: Decide the Evidence Report Schema

First fix the contract returned to the outside. Minimum fields:

- `evidence_type`
- `target`
- `question`
- `summary`
- `status`
- `effect`
- `confidence`
- `metrics`
- `model_parameter_hints`
- `limitations`
- `artifacts`
- `provenance`

### Step 2: Add a tool that builds an evidence report from the existing DAG

Do not build the Study Layer yet; generate the report from existing `prod` / `analyze` nodes.

First tool:

```python
generate_md_evidence_report(job_dir, report_type="stability")
```

### Step 3: Build a minimal Study Layer

At first, do not fix a specific scientific workflow; create a thin file contract for `study_dir`.

Reasons:

- It can also be used for ordinary MD research (WT vs mutant, apo vs holo, forcefield comparison)
- Integration with AlphaGenome/Evo2/transcriptomics etc. can be an optional annotation
- It keeps a `job_dir` as one source-bundle execution unit

### Step 4: Add a request schema for ScienceClaw / cell agents

External agents do not specify MDClaw's low-level nodes directly; they send requests like the following.

```json
{
  "target": {
    "gene": "GENE_X",
    "uniprot_id": "P12345",
    "mutation": "V148A"
  },
  "question": "stability_effect",
  "context": {
    "cell_type": "hepatocyte",
    "pathway": "MAPK"
  },
  "budget": {
    "simulation_time_ns": 20,
    "replicates": 3
  },
  "required_confidence": "medium"
}
```

## Key design decisions

### Do not make the MDClaw DAG overly externally compatible

Do not force MDClaw's internal DAG to merge with ScienceClaw's DAG. The internal DAG is kept as a durable execution graph for running MD.

Narrow the contract exposed to the outside to these four:

- MD request
- MD evidence report
- artifact references
- provenance

### Do not rely on natural language alone

An evidence report needs a natural-language summary, but that alone is not enough.

For ScienceClaw and cell models to use it in their next decisions, a small number of typed indicators is needed.

Natural language:

- Readable by humans
- Strong at conveying meaning
- Easy to explain limitations with

Typed metrics:

- Can be compared
- Can be ranked
- Can be judged against thresholds
- Can be used as parameter hints for downstream models

### Always return confidence and limitations

In connecting with cell foundation models, overconfidence is dangerous.

MDClaw always returns not only the conclusion but also how far it can be trusted and what its limits are.

Example:

- `low`: short single-replicate screen; qualitative only
- `medium`: multiple replicates with consistent RMSD/RMSF shift
- `high`: longer sampling plus free-energy estimate and replicate agreement

## Minimum goal

The first finished form:

```text
External agent:
  "Does P12345 V148A affect stability?"

MDClaw:
  Run mutation_stability_study
  Run short replicate MD for WT vs mutant
  Compare RMSD/RMSF/active-site flexibility
  Return the evidence report
```

In this form, MDClaw becomes a natural "atomic-level evidence provider" for cell-simulator foundation models.
