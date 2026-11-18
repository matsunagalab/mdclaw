# MDClaw × ScienceClaw × OpenClaw integration possibilities

Date written: 2026-05-14

This doc records how MDClaw is positioned within the "Claw family of agents", and, narrowed to three, the concrete boundaries at which they can integrate. The detailed integration plan with ScienceClaw alone is in the existing
[`mdclaw_scienceclaw_integration_plan.md`](./mdclaw_scienceclaw_integration_plan.md); this doc extends it to cover three-way integration including OpenClaw.

## Assumptions and terminology

The discussion fixes each agent's assumed scope as follows. Everything other than MDClaw is assumed to be developed outside this repository; this doc will be updated once the scopes are settled.

| Agent | Assumed scope |
|---|---|
| **MDClaw** | Study / execution / evidence for atomic-level MD. Managed in this repository. |
| **ScienceClaw** | Cell- and pathway-scale reasoning, hypothesis generation, updating cell/pathway models. The side that calls MDClaw as an atomistic evidence provider. |
| **OpenClaw** | An agent that integrates public databases and experimental data. A layer that bridges external resources such as PDB / UniProt / AlphaFold DB / ChEMBL / cryo-EM EMDB / HDX-MS / NMR / omics. |

As a common policy, no agent exposes its internal DAG / implementation details. Only the following 4 kinds of contract sit at the boundary:

- request (what is being asked)
- evidence report (what was learned; typed metrics + natural-language summary + confidence + limitations)
- artifact reference (pointer to a reusable product)
- provenance (where it came from)

This extends the same principle as the existing ScienceClaw integration doc to the whole Claw family.

## Three integration possibilities

### 1. A Claw-wide request / evidence schema

**What**: Promote the `evidence_type` + `summary` + `effect` + `metrics` + `confidence` + `limitations` + `provenance` contract that MDClaw exposes into a schema, one level more abstract, shared with ScienceClaw / OpenClaw.

**Why**: When ScienceClaw receives evidence from both MDClaw and OpenClaw and uses it to update cell/pathway model parameters, if both outputs have the same shape, the ranking / threshold / hypothesis-update logic can be unified. Conversely, if the schema stays MDClaw-specific, MDClaw-specific adapters will proliferate on the ScienceClaw side.

**Boundary design**:

- Common envelope: `evidence_type`, `target`, `question`, `summary`, `effect`, `confidence`, `metrics`, `limitations`, `provenance`
- The payload part (`metrics`, `effect`, `model_parameter_hints`) varies per evidence_type
- evidence_type identifies the source by a prefix such as `md_*` / `experimental_*` / `database_lookup_*`

**First step**: Cut the existing `mdclaw/evidence_schema.py` (proposed) into a form with an `evidence_type` prefix, aligning names like `evidence_type: "md_mutation_stability"`. Export a JSON Schema file to `docs/research/schemas/` that the ScienceClaw / OpenClaw side can reference read-only.

**Design decision that is not MDClaw's alone**: Share the schema versioning policy (migration rules for breaking changes) across the Claw family. Do not decide it within MDClaw alone.

### 2. OpenClaw → MDClaw structure-source bypass

**What**: OpenClaw gathers candidate structures from external resources (PDB, AlphaFold DB, UniProt, generative models such as Boltz-2, cryo-EM maps, homology models) and passes them directly in the shape of the *source bundle* that MDClaw's `source` node accepts.

**Why**: MDClaw's source node is already designed (schema v3) to "normalize multiple candidate structures into `candidates/candidate_*` and record rank / provenance / confidence in `source_bundle.json`", so pouring the structures OpenClaw gathered into it is the most natural connection point. It avoids adding logic on the MDClaw side to fetch individual data sources directly.

**Boundary design**:

- OpenClaw passes MDClaw a JSON equivalent to `source_bundle.json` + a set of candidate structure files
- Required fields: `candidates[*].source_type` (`pdb` / `alphafold` / `boltz2` / `cryoem` / `homology` etc.), `candidates[*].rank`, `candidates[*].confidence_metric`, `candidates[*].provenance`
- Optional fields: `candidates[*].experimental_constraints` (references to cryo-EM map files, HDX-MS protection factors, NMR chemical shifts, etc.)
- The MDClaw side writes it as is as the `source` node's artifact, and prep and everything after it run on the same contract

**First step**: Settle the output schema of `list_source_candidates` as is as the "input contract", and publish `docs/research/schemas/source_bundle.schema.json`. Make the relationship such that if the OpenClaw side writes it, MDClaw can receive it without changing anything.

**Side benefit**: Experimental data can be fed in via `experimental_constraints`, so this becomes a natural extension point toward flexible fitting and restrained MD (the knowledge in the existing `flexible_fit/` pays off).

### 3. Three-way closed loop: hypothesis → atomistic → experimental cross-check

**What**: An operating pattern that runs a closed loop once / a few times: ScienceClaw proposes a hypothesis, MDClaw produces evidence by atomistic simulation, and OpenClaw cross-checks it against public databases and experimental data.

**Why**: MD is powerful but has many limitations in principle (sampling, force fields, absolute-value scale). If known experimental facts about the same system (clinical effects of mutations, binding affinities, HDX-MS protection, cryo-EM local resolution) are pulled back via OpenClaw, then even when MDClaw's confidence is at the `low_to_medium` level, ScienceClaw can screen for "is this consistent with experimental facts?", raising the quality of decisions.

**Loop example**:

```text
ScienceClaw:
  "Predict that mutation V148A lowers activity in pathway P. Ask MDClaw to check stability,
    and OpenClaw to check existing experimental data"
       ↓
MDClaw:
  short replicate MD of WT vs V148A → evidence_type: md_mutation_stability
    effect: destabilizing, confidence: medium
       ↓
OpenClaw:
  ClinVar / UniProt variant DB / literature extraction → evidence_type: db_variant_effect
    effect: pathogenic (literature), confidence: high
       ↓
ScienceClaw:
  Both agree → update pathway P's rate parameter to down-regulate
  (Both disagree → raise a flag and escalate to a human)
```

**Boundary design**:

- The requests ScienceClaw sends to both share `target` + `question` (the same UniProt ID + mutation coordinates, the same "stability_effect" question, etc.)
- The two evidence_types differ, but they are comparable through the common envelope (`effect.direction`, `effect.magnitude`, `confidence`)
- ScienceClaw holds the policy "update automatically when both pieces of evidence agree; escalate when they disagree"

**First step**: Implement one question type, "mutation stability effect", across the 3 as a pilot. The MDClaw side can handle it with the existing study type `wt_vs_mutant_comparison`. The minimal implementation is the part where the ScienceClaw / OpenClaw side sends the same `target` and returns evidence in the same envelope.

**Design decision that is not MDClaw's alone**: Agree with the ScienceClaw side on the escalation target on disagreement (human / another agent), and on the contract for the additional information then requested of the MDClaw side (longer MD, more replicates, enhanced sampling).

## Summary: preparation needed on the MDClaw side

What the MDClaw side should firm up before integration gets serious is largely common across the three proposals.

1. **Export the evidence schema**: Settle the `evidence_type` prefix and the common envelope, and place them as JSON Schema somewhere that can be published externally (`docs/research/schemas/`).
2. **Make the source bundle schema an input contract**: Turn the existing `source_bundle.json` into a schema that also works on the premise that "the outside writes it and MDClaw reads it".
3. **Enforce the practice of always returning confidence / limitations**: A precondition for detecting disagreement in the closed loop. Emit `confidence: low/medium/high` at a granularity the caller can judge against a threshold.

These were already needed for integration with ScienceClaw alone; there are no new requirements that first arise in the three-way integration with OpenClaw. In other words, simply pushing ahead with the existing ScienceClaw integration plan becomes the integration foundation for the whole Claw family.
