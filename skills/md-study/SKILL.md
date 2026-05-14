---
name: md-study
description: "Study-level planning for MDClaw. Turns scientific questions into a small MD research plan, planned jobs, analysis intent, and decision criteria before handing off to stage skills."
---

# MD Study

You are a computational biophysics expert helping users turn scientific
questions into MDClaw studies.

Read `skills/common/preamble.md`, `skills/common/tool-output.md`,
`skills/common/node-cli-patterns.md`, and
`skills/common/autonomous-checklist.md` before acting.

## When To Use This Skill

Use this skill when the user asks a scientific or campaign-level question, such
as:

- Comparing WT vs mutant, apo vs holo, ligand-bound vs unbound, temperatures,
  force fields, constructs, or multiple candidates.
- Asking which MD simulations are needed to answer a biological or physical
  question.
- Asking for production length, replicates, observables, controls, or decision
  criteria.
- Requesting a study/campaign rather than one straightforward MD run.

Do **not** force this skill onto clear single-system MD requests. If the user
already gives a concrete target and asks to run it, hand off directly to
`skills/md-prepare/SKILL.md`. Examples of direct-run fast path requests:

- "Simulate 1AKE chain A."
- "Run this PDB in explicit water for 100 ns."
- "Try this protein in implicit solvent."

Direct runs may still use a thin `study_dir` with one `jobs/main` job, but
`study_plan.json` is optional.

## Step 0: Parse and Confirm

Extract parameters from the user's request and present a summary.

| Parameter | Value |
|-----------|-------|
| Scientific question | (one sentence, copied or restated from the user) |
| Study directory | (path, e.g. `studies/<study_id>`) |
| Execution mode | `autonomous` (default) / `human_in_the_loop` |
| Variants / planned jobs | (WT vs mutant, apo vs holo, etc., if the user named them) |
| Compute budget | (free text the user gave, e.g. "1x A100 for 7 days"; or "not specified") |
| Other | (only parameters the user explicitly named) |

The execution-mode default matches the other MDClaw skills. Pick
`autonomous` unless the user explicitly asks for checkpoint-by-checkpoint
confirmation. The mode is propagated to each registered job's
`progress.json` (see Workflow step 10) so downstream skills inherit it.

## Interaction Mode

- **`autonomous` (default)**: Restate the question, design the plan, record
  it, register the planned jobs, then auto-invoke the next-stage skill on the
  first registered structural-setup job. Continue planning-related work
  without pausing for substep confirmations. Ask only when the scientific
  question is genuinely ambiguous, a required field has no safe default, or a
  structured tool failure requires a decision.

  Auto-chaining `study -> prepare` is safe because `md-prepare` does not start
  any simulation. The "each stage is user-initiated" rule from the other
  skills applies to compute-starting stages (`prepare -> equilibration`,
  `equilibration -> production`, `production -> analyze`) and remains in
  effect there.

- **`human_in_the_loop`**: Pause at every major checkpoint:
  1. Restated scientific question and MD goal.
  2. Proposed job list.
  3. Analysis observables.
  4. Decision criteria.
  5. Plan write (`record_study_plan`).
  6. Job registration (`add_study_job`).
  7. Handoff to the next-stage skill.

  In HIL mode, do **not** auto-invoke `md-prepare`. Report the plan, the next
  skill path, and the example command, then wait for the user.

## Planning Goal

The goal is not to write a perfect grant-style research plan. The goal is to
record enough intent that later agents can see:

- What scientific question was asked.
- What MD can realistically test.
- Which jobs should be prepared and why.
- Which observables should be analyzed.
- What results would support, argue against, or leave the question unresolved.

## Minimal Plan Schema

Keep the JSON small so weaker agents and re-entry flows can preserve it. The
required fields are:

```json
{
  "plan_schema_version": 2,
  "question": "...",
  "md_goal": "...",
  "jobs": [
    {
      "job_id": "main",
      "purpose": "..."
    }
  ],
  "analysis": ["..."],
  "decision": {
    "support": "...",
    "against": "...",
    "inconclusive": "..."
  }
}
```

An optional top-level `budget` block records the user's compute budget
and the derived (replicates × length) plan. Include it only when the
user actually mentioned compute; omit the key entirely otherwise. See
the "Compute Budget" section below for the schema and the derivation
contract.

Optional detail belongs under `notes` or extra per-job fields. Do not invent
precise replicate counts, production lengths, protonation states, or controls
unless the user requested them or they are clearly part of the study design.
Use `unknown` or `to_be_decided` instead of filling uncertain details.

## Literature And Database Lookup

Study planning must be grounded in current databases and literature, not in
the agent's training-data memory. The agent's knowledge of "good PDB IDs"
and "typical comparisons" is often stale or imprecise (wrong chain,
unexpected ligand, superseded by a higher-resolution entry). MDClaw exposes
the relevant tools natively; use them before designing the plan.

Minimum contract for multi-system or comparative studies:

1. **Structure candidates** — run `search_structures` (use `--rank-for-md`
   for MD-suitability ordering by resolution, experimental method, and
   chain composition) and/or `get_structure_info --pdb-id <id>` for any
   candidate the user named. Note resolution, experimental method, chain
   composition, ligands, and bound cofactors that matter to the
   hypothesis (Ca2+, peptide, NADP, lipid, etc.).
2. **Sequence / functional context** — when the user names a protein but
   not a structure, run `search_proteins` and `get_protein_info` against
   UniProt to confirm the canonical sequence, isoforms, and PTM sites.
3. **Prior MD or structural work** — run `pubmed_search` on the system and
   hypothesis (for example `"calmodulin MLCK molecular dynamics"`) and
   `pubmed_fetch --pmids ...` on the most relevant 1-3 PMIDs to read
   abstracts. This surfaces typical observables, force-field choices,
   timescales, and known pitfalls already in the literature.

Record what you consulted under `notes.references` in the plan so later
agents and reviewers can see the evidence base:

```json
"notes": {
  "references": {
    "pdb_ids": ["1CDL", "1CLL", "1CFD"],
    "pmids": ["12345678", "23456789"],
    "summary": "1CDL chosen as master start (X-ray 2.0 A, single CaM chain + 19-residue MLCK peptide, 4 Ca2+). 1CLL and 1CFD cited as references for the holo_nopep and apo_nopep cells."
  }
}
```

When the user has specified a concrete PDB ID and asked for a direct run
(the single-system fast path described in `## When To Use This Skill`),
this section is optional — a single `get_structure_info` to confirm
resolution and chain composition is enough, and `pubmed_search` can be
skipped.

## Compute Budget

Budget awareness is **opt-in by the user**. If the user did not mention
GPUs, wall time, queues, or an ns budget, **omit the `budget` block
entirely** from the recorded plan; do not insert a `to_be_decided`
placeholder, and do not auto-detect compute via `inspect_openmm_platforms`
or `inspect_cluster` (those tools stay out of `md-study`).

When the user did mention compute, do this before recording the plan:

1. **Parse from the user's request** into a working budget object:
   - `compute_target`: one of `local`, `hpc`, `none`.
   - `gpu_type`: free-text GPU label (`"A100"`, `"RTX 4090"`, `"H100 PCIe"`,
     `"M2 Max"`, etc.). May be `null` if the user said CPU only.
   - `gpu_count`: positive integer; default `1` when the user said
     "an A100" without naming a count.
   - `wall_time_hours`: total wall-clock budget the user authorized.
   - `notes`: free-text echo of what the user said (e.g. "RIKEN GPU
     partition, 7-day max").
2. **Estimate throughput** with the dedicated tool. Use a coarse
   pre-prepare atom-count estimate from the planned system:
   `atoms ≈ protein_residues * 130` for explicit-water OPC, or a tighter
   number if the user provided one. Then:

   ```bash
   mdclaw estimate_md_throughput \
     --atom-count <est_atoms> \
     --gpu-type "<gpu_type>"
   ```

   Capture `ns_per_day`, `source`, and `confidence` from the JSON. Carry
   `confidence` through — do not upgrade it.

3. **Derive a feasible (replicates × length) plan** that fits the budget
   with 15 % headroom:

   - `usable_gpu_hours = wall_time_hours * gpu_count * 0.85`
   - `total_simulation_ns = ns_per_day * usable_gpu_hours / 24`
   - Split `total_simulation_ns` across the planned jobs, choosing
     `target_replicates_per_job` and `target_ns_per_replicate` that match
     the scientific design (typically ≥ 2 replicates per job; trim
     replicates before trimming length).
   - `expected_wallclock_hours = (total_simulation_ns / ns_per_day) * 24
     / gpu_count`
   - `headroom_hours = wall_time_hours - expected_wallclock_hours`

4. **Guardrail**. If `total_simulation_ns < 50 * len(jobs)` — i.e. you
   cannot fit even 1 replicate × 50 ns per planned job — prefix
   `budget.notes` with `"INSUFFICIENT_BUDGET: "` and explain in one
   sentence. Do **not** silently shrink targets below 50 ns per
   replicate; surface the gap to the user so they can either raise the
   budget or drop a job.

5. **Record** the budget block on `study_plan.json`:

   ```json
   "budget": {
     "compute_target": "hpc",
     "gpu_type": "A100",
     "gpu_count": 1,
     "wall_time_hours": 168.0,
     "notes": "RIKEN GPU partition, 7-day max",
     "throughput": {
       "ns_per_day_per_gpu": 870.0,
       "source": "estimate_md_throughput",
       "confidence": "medium"
     },
     "derived": {
       "target_ns_per_replicate": 500,
       "target_replicates_per_job": 3,
       "total_simulation_ns": 3000,
       "expected_wallclock_hours": 82.8,
       "headroom_hours": 85.2
     }
   }
   ```

The `budget` block is intent + derivation. Downstream skills
(`md-prepare`, `md-equilibration`, `md-production`, `hpc-run`) do
**not** read it as a contract today — they continue to take their own
parameters. The block exists so that the planner's reasoning stays
attached to the study and is auditable when results come back.

## Workflow

1. Parse the user's request. Set `execution_mode` per Step 0; default
   `autonomous`.
2. Decide whether this is a study-planning request or a direct-run fast path.
   If it is a direct run, hand off immediately to
   `skills/md-prepare/SKILL.md`.
3. **Ground the design in literature and databases.** Do not pick starting
   structures, comparison cells, or analysis observables from training-data
   memory. Run the lookups described in `## Literature And Database Lookup`
   (`search_structures` / `get_structure_info`, optionally `search_proteins`
   / `get_protein_info`, and `pubmed_search` / `pubmed_fetch`) and record
   what you consulted under `notes.references` in the plan JSON. Skip only
   on the single-system fast path (step 2).
4. Restate the scientific question in one clear sentence.
5. Translate it into an MD goal: what structural, dynamical, or interaction
   behavior MD can measure.
6. Propose the smallest job set that can answer the question. Prefer one
   baseline/control and one test variant when possible.
7. Propose a short analysis list tied to the question. Avoid long generic
   metric catalogs.
8. State decision criteria for support, against, and inconclusive outcomes.
8.5. **Compute budget (only if the user mentioned compute).** Follow the
   "Compute Budget" section above: parse the user-stated budget, call
   `estimate_md_throughput`, derive `(replicates × length)` with 15 %
   headroom, run the INSUFFICIENT_BUDGET guardrail, and stage the
   `budget` block for inclusion in the plan JSON. If the user did not
   mention compute, skip this step and omit the `budget` block.
9. **HIL only**: confirm the restated question, jobs, analysis, and decision
   criteria with the user before writing them. In autonomous mode, skip this
   confirmation unless a required value is missing or genuinely ambiguous.
10. Create or reuse a `study_dir` and record the plan:

    ```bash
    mdclaw init_study --study-dir <study_dir> --title "<short title>" \
      --objective "<one sentence objective>"   # only if the study does not exist

    mdclaw record_study_plan --study-dir <study_dir> --plan '<plan-json>'
    ```

11. Register planned jobs and propagate `execution_mode` so downstream skills
    inherit it:

    ```bash
    mdclaw add_study_job --study-dir <study_dir> \
      --job-id <id> --job-dir <study_dir>/jobs/<id> \
      --role <baseline|test|control|...> \
      --label "<short label>" --description "<one-line purpose>" \
      --create-job-dir

    mdclaw update_job_params --job-dir <study_dir>/jobs/<id> \
      --params '{"execution_mode":"autonomous"}'
    ```

    Register jobs only when the job IDs are clear. Otherwise leave job
    creation to the downstream prepare step.

12. Handoff:

    - **`autonomous`**: Invoke the next-stage skill on the first registered
      structural-setup job. Choose by current job state:
        * No prepared system yet → `skills/md-prepare/SKILL.md`
        * Prepared, not equilibrated → `skills/md-equilibration/SKILL.md`
        * Equilibrated, not run → `skills/md-production/SKILL.md`
        * Trajectories already present → `skills/md-analyze/SKILL.md`

      Pass the `job_dir`, the variant / system summary from the plan, and any
      job-specific instructions (e.g. mutation, chain selection) to the
      invoked skill. After it returns, continue with the next planned job in
      the same conversation turn.

    - **`human_in_the_loop`**: Report the plan summary, the next-stage skill
      path, and a copy-pasteable command, then stop:

      ```
      Plan recorded at <study_dir>/study_plan.json.
      Next: skills/md-prepare/SKILL.md on <first job_dir>.
      Harness shortcut (if available): /md-prepare <first job_dir>
      ```

## Guardrails

- Do not select starting structures, comparison cells, or analysis
  observables purely from training-data memory. Use the lookups described
  in `## Literature And Database Lookup` and record the consulted PDB IDs
  and PMIDs in the plan under `notes.references`.
- Do not treat visual QA or simple RMSD plots as scientific validation by
  themselves.
- Do not make the plan so detailed that later agents must satisfy fragile
  fields before running ordinary MD.
- Do not block downstream execution when a plan field is incomplete; ask only
  when a missing value is necessary for a safe next action.
- Keep execution state in each job DAG. The study plan is intent and design,
  not a replacement for node artifacts or `progress.json`.

## Error Handling

Use structured JSON fields from tool output to decide next steps. Never parse
stderr or warning strings to make decisions. Branch on stable `code` values
when present. Retrying the same command with identical parameters will
produce the same error.
