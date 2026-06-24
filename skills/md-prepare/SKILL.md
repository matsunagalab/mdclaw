---
name: md-prepare
description: "Molecular dynamics simulation preparation using MDClaw CLI tools. Covers structure acquisition, protein/nucleic/ligand selection, structure cleaning, solvation, and topology generation."
---

# MD Prepare

You are a computational biophysics expert helping users set up MD simulations using the MDClaw CLI tools.

Read `skills/common/preamble.md`, `skills/common/tool-output.md`,
`skills/common/defaults.md`, `skills/common/node-cli-patterns.md`,
`skills/common/run-loop.md`, `skills/common/autonomous-checklist.md`, and
`skills/common/guardrail-codes.md` before acting.

`skills/common/run-loop.md` is the per-step loop: inspect the job DAG, create
the node for the current stage, validate it with `explain_node`, then run the
tool with node context. Use IDs returned by `inspect_job`, `explain_node`, and
`create_node`, never literal example IDs.

## Defaults — Source of Truth

This project uses **ff19SB + OPC** as the modern explicit-water default
(Amber Manual 2024 recommendation), NOT the legacy `ff14SB + tip3p`
combination commonly seen in AMBER tutorials and training data. The
pairing is enforced by guardrails — `ff19SB + tip3p` is rejected as a
structured error (code `forcefield_water_blocked`).

Default solvation mode is **explicit solvent**. Unless the user explicitly
requests implicit solvent, no-solvent/vacuum topology, or a membrane workflow,
run `prepare_complex` → `solvate_structure` → `build_amber_system`. Topology
tools must consume the completed DAG parent artifact (`solvated_pdb` for
explicit/membrane, `merged_pdb` for implicit/vacuum); never pass a raw/manual
PDB file directly into topology generation.

Do **not** infer defaults from prior AMBER knowledge. Tool signatures and
guardrails are authoritative; the skill guidance provides quick references:

- `skills/md-prepare/defaults-and-guardrails.md` — preparation defaults,
  guardrails, and ligand failure policy
- `skills/md-prepare/explicit-water.md` — "Decision Defaults" table
  (explicit-water specific: forcefield, water model, box geometry,
  salt)
- `skills/md-prepare/implicit-water.md` — implicit-solvent defaults

Read the relevant guidance page **before** writing any value into the Step 0
confirmation summary or executing any tool.

## Workflow

The required execution order is **read → confirm → execute**. Do not
present defaults to the user, and do not run any tool, before the
guidance pages for the relevant solvation mode have been read.

Every MD workflow starts from a study plan and uses the same folder layout.
For a clear single-system request such as "simulate 1AKE chain A" or "run this
PDB in explicit water", create a minimal study plan instead of doing campaign
planning. The canonical bootstrap is:

```bash
mdclaw bootstrap_md_workflow \
  --study-dir <study_dir> \
  --question "<user request>" \
  --md-goal "<one sentence MD goal>" \
  --solvent-regime explicit \
  --execution-mode autonomous
```

Replace `"explicit"` with `"implicit"`, `"vacuum"`, or `"membrane"` when the
request names that regime. The returned `job_dir` is the only directory passed
to DAG tools. The bootstrap writes `study.json`, `study_plan.json`, and
`jobs/main/progress.json`; do not create a standalone job directory outside a
study.

Richer study-planning handoff: if the user is asking a scientific comparison
or campaign-level question (mutant vs WT, apo vs holo, controls, replicates,
analysis criteria, or "what MD should I run?"), use
`skills/md-study/SKILL.md` first to record a richer plan. It still writes the
same canonical `study_dir/jobs/<job_id>` layout.

`solvent_regime` is decided during study bootstrap/planning. For a minimal
single-system bootstrap, the default is `explicit` unless the user explicitly
asks for implicit solvent, vacuum/no-solvent, or a membrane workflow. When a
study/job records `solvent_regime`, treat it as intent and map it to tool
calls:

| `solvent_regime` | prep call | next structural step | topology mode |
|---|---|---|---|
| `explicit` | `prepare_complex --solvent-type explicit` | `solvate_structure` | `build_amber_system` with `box_dimensions` |
| `implicit` | `prepare_complex --solvent-type implicit` | skip solv | `build_amber_system --implicit-solvent <MODEL>` |
| `vacuum` | `prepare_complex --solvent-type vacuum` | skip solv | `build_amber_system` without box or GB |
| `membrane` | `prepare_complex --solvent-type explicit` | `embed_in_membrane` | `build_amber_system` with membrane box |

Start from a study. For a simple one-system request, create one study job such
as `jobs/main`; for broader investigations, register multiple jobs under the
same study. Within each job, use one `source` node that records a source bundle.
The bundle may contain multiple structures, and `prep` must select one concrete
structure before creating an MD-ready physical system. Use DAG branching after
`prep` to explore variants of that prepared system. For point/multi-mutants,
use the HPacker-based `create_mutated_structure` branch in
`skills/md-prepare/branches.md`.

1. Ensure the canonical study layout exists with `bootstrap_md_workflow` or a
   richer `md-study` plan. Use the returned `job_dir` for all DAG commands.
2. Decide `execution_mode` from the user's request:
   - `execution_mode=autonomous` unless the user explicitly asks for
     checkpoint-by-checkpoint confirmation.
   - Persistence to `progress.json` normally happens in
     `bootstrap_md_workflow`. If you are repairing an older study, write it via:
     ```bash
     mdclaw update_job_params --job-dir <job_dir> \
       --params '{"execution_mode":"autonomous","solvent_regime":"explicit"}'
     ```
3. **Read `skills/md-prepare/setup.md` first** — it routes to the focused
   setup guidance for acquisition, inspection, cleaning, branches, and resume.
   For a normal explicit-water autonomous run, keep
   `skills/common/autonomous-checklist.md` as the short execution spine and
   open only the task-specific guidance pages tagged by `setup.md`.
4. Determine the effective `solvent_regime` from the study plan / job params.
   Then read the matching guidance page. If the current job lacks
   `solvent_regime`, repair the bootstrap with `update_job_params` before
   running `prepare_complex`.
5. **Read the solvation-specific guidance page** — required before stating
   any forcefield / water / box default to the user:
   - Explicit water (default) → `skills/md-prepare/explicit-water.md`
   - Implicit solvent → `skills/md-prepare/implicit-water.md`
   - Membrane → explicit-water guidance plus membrane setup guidance from
     `setup.md`
   - Vacuum/no-solvent → implicit-water guidance for the no-solvation topology
     contract, but do not pass `--implicit-solvent`
6. **Now present the Step 0 confirmation summary** (see Step 0 below)
   to the user. Only the fields enumerated there belong in the table —
   forcefield, water model, box geometry, etc. are tool-level defaults
   surfaced from the guidance pages read in steps 2–3 and are not part of
   the user-facing summary unless the user explicitly named them.
7. Execute prepare_complex / mutate / PTM restore / solv / topo per setup.md and the
   solvation guidance. If the user specifies site-specific residue
   protonation, pass it explicitly through `protonation_states`; do not leave
   it as a free-text note. If the user specifies a biological assembly, request
   it during `fetch_structure` with `--assembly-ids <id...>` or
   `--assembly-mode preferred|all`, then select the intended source candidate
   during `prepare_complex`. Create nodes first, then run workflow tools with
   both `--job-dir` and `--node-id`. For biological assemblies or systems with
   many chains, do not treat the one-character PDB chain ID in `merged_pdb` as
   a canonical identity. Read `chain_identity_map.json` and use `component_id`,
   source label/auth IDs, topology chain index, and atom/residue ranges to
   identify components. Always pass the effective solvent regime to
   `prepare_complex`: `explicit` for explicit-water and membrane systems,
   `implicit` for implicit solvent, and `vacuum` for deliberate no-solvent
   topologies. Keep supported crystallographic ions by default on the
   explicit-solvent path. In implicit solvent, prep will exclude explicit ion
   components from `merged_pdb` and record them in
   `component_disposition.json`. A deliberate vacuum/no-solvent topology may
   keep explicit ions, but it is not the default MD workflow. `build_amber_system`
   validates the same invariant and rejects implicit builds that contain
   explicit ions with `code="explicit_ions_in_implicit_solvent"`.
   Experimental isotope atoms such as deuterium are excluded by
   `prepare_complex` across split components from the default classical MD
   path, then standard hydrogens are rebuilt; copy the tool-written
   `component_disposition.json` rather than hand-writing it. If the user
   requests terminal caps, use `--n-terminal-cap ACE` and/or
   `--c-terminal-cap NME`; `--cap-termini` is only the shorthand for both.
   Cap-residue hydrogen completion is tool-owned in `prepare_complex`; when
   the user specifies a non-default protein force field for the eventual
   topology, pass the same value as `--terminal-cap-forcefield`, otherwise use
   the ff19SB default. If the user
   prepares standard DNA/RNA, `prepare_complex` rebuilds nucleic hydrogens with
   OpenMM Modeller using the current DNA.OL15/RNA.OL3 libraries before topology.
   If the user
   explicitly asks for isotope-preserving MD, treat that as unsupported for now
   and stop with a structured explanation instead of silently converting D to H.
   For glycoproteins, prep preserves glycan provenance/linkages; Amber/GLYCAM
   conversion, bond-plan application, and glycan-only H completion are topology
   normalization artifacts written by `build_amber_system`.
   When creating the `topo` node, use the correct completed parent node and let
   `build_amber_system` auto-resolve its input; do not supply a free-standing
   `--pdb-file` or re-enter the workflow from a raw PDB.
7. After each completed structural node where human inspection is useful
   (`source`, `prep`, `solv`, `topo`), perform Visual QA per
   `skills/common/visual-qa.md` and register it with `register_visual_review`.
   Visual QA is only an obvious-accident check; never infer force-field,
   protonation, parameter, or chemistry correctness from the image. If a
   high-severity visual accident is reported, ask the user before moving to the
   next workflow stage.
8. After the `topo` node completes, hand off to the equilibration skill on the
   same `job_dir` (use the node id from `create_node`, not a literal copied
   from an example). In harnesses with slash commands, `/md-equilibration` is the
   shortcut. This skill does not auto-chain into equilibration — each stage is
   user-initiated.

## Step 0: Parse and Confirm

Run this **after** Workflow steps 2–4 (the guidance pages have been read).

The summary table includes only the fields listed below. Do **not**
add forcefield, water model, box geometry, or any other tool-level
default to this table — those values come from the guidance pages and are
applied silently by the tools unless the user explicitly named one.

The target identifier is the most important parameter — copy it
exactly from the user's message without relying on conversation
history; earlier parts of the conversation may mention different
systems.

| Parameter | Value |
|-----------|-------|
| Target | (PDB ID / sequence / file — exactly as the user wrote) |
| Execution mode | `autonomous` (default) / `human_in_the_loop` |
| Chain(s) | (if specified; after inspection, expand to ligand label chains when ligands should be included) |
| Ligands | include / exclude (use inspected ligand `unique_id` values) |
| Solvent regime | explicit (default) / implicit / vacuum / membrane |
| Mutations | (if any — one-letter notation, e.g. K27A) |
| Production length | (if specified) |
| Other | (only parameters the user explicitly named — do not pre-fill defaults here) |

This confirmation step applies to all interaction modes including
autonomous. Misidentifying the target cannot be recovered later.

**Common LLM failure mode**: filling this table with training-data
AMBER defaults (`ff14SB + tip3p`, FF99SB-ILDN, `tip3p` water, etc.).
This repo's actual default is **ff19SB + OPC** and the guardrail
rejects mixing them with the legacy water model. Trust the skill guidance,
not your prior knowledge.

**Common chain/ligand failure mode**: treating "chain A ligandあり" as
`--select-chains A` only. Use `inspect_molecules.associated_ligand_candidates`
or the `associated_ligands_require_selection` guardrail instead of inferring
ligand label chains manually. When the requested ligand/cofactor is named by
residue, use `--include-ligand-resnames <RESNAME>`; reserve
`--include-associated-ligands` for cases where every same-author ligand
candidate should be included.

## Interaction Mode

- **`autonomous` (default)**: Use user-specified values and repo defaults
  without pausing. Ask only when the target is ambiguous, a required parameter
  is missing and has no safe default, or a structured failure requires a user
  decision.
- **`human_in_the_loop`**: Pause at every decision checkpoint and confirm the
  next action with the user. The full checkpoint list and the confirmation
  loop is summarized in `skills/md-prepare/checkpoints.md`.

## Error Handling

Use structured JSON fields from tool output to decide next steps. **Never parse stderr or warning strings to make decisions.**

Key fields to check:
- `overall_status` — `success`, `completed_with_blocking_ligand_failure`, or `failed`
- `ligand_chemistry` — ligand SDF/SMILES/provenance for topology
- `workflow_recommendation` — contains `options` (list of valid next actions)
- `recommended_next_action` — per-ligand: `provide_smiles_or_exclude_ligand`, `hard_fail`
- `failure_class` — what went wrong. Common classes include `input_error`,
  `metal_atoms`, `ligand_chemistry_failed`, and `unexpected_error`.

Rules:
- Ligand charge comes from charged SMILES/SDF, not an integer note. Use
  explicit formal charges such as `[O-]` or `[NH3+]`.
- If `recommended_next_action = provide_smiles_or_exclude_ligand`: ask for a
  chemistry source or exclude the ligand; do not continue with an untyped PDB
  ligand.
- If `recommended_next_action = hard_fail`: stop immediately. Do not attempt workarounds.
- Retrying the same command with identical parameters will produce the same error.
- If stuck, report the structured error fields and ask the user for guidance.
- The full HITL interaction loop (check `confirmation_needed`, respect
  `source`, re-run with overrides if needed) is documented in
  `skills/md-prepare/checkpoints.md`.
