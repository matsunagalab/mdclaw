---
name: md-prepare
description: "Molecular dynamics preparation with MDClaw CLI tools: acquisition, molecule selection, cleaning, solvation, and topology. Before any state-changing command, follow the pre-command gate in this skill; open linked pages only when their condition applies."
---

# MD Prepare

You are a computational biophysics expert helping users prepare MD simulations
with the MDClaw CLI. This page is the complete normal-path spine; linked pages
provide conditional detail and do not need to be read in a fixed order.

**MDClaw CLI manages node state and artifact handoff.** Do not manage files
between workflow nodes; follow `skills/common/run-loop.md`.

## Pre-command Gate

Before the first state-changing command:

1. Copy the target exactly from the current request. Decide the requested
   stopping stage, `solvent_regime`, and interaction mode, and prepare the Step
   0 summary below. Explicit solvent and `autonomous` are the defaults unless
   the user says otherwise.
2. Try the canonical MDClaw stage before custom MD code; bare `mdclaw` uses the
   configured dependency-complete runtime; unsupported results permit another toolchain.
3. Start new work with `bootstrap_md_workflow`; broader comparisons use
   `md-study`. Immediately inspect the returned job once. Also run `inspect_job`
   on re-entry, before shared-job work, or when branch parents are ambiguous.
   A fresh unambiguous serial run does not need inspection before every node.
4. For every state-changing stage, run `create_node`, then `explain_node`, then
   the stage tool with the returned `job_dir` and `node_id`. Run only when
   `ready_to_run=true` with no blocking codes or missing inputs.
5. The workflow below already names the normal-path tools; do not scan the
   global registry with bare `mdclaw --list`. To check one tool's signature,
   use `mdclaw --list-json <tool>`. Only if that is insufficient, read the
   complete `mdclaw <tool> --help`. Never pipe CLI discovery or help through
   `head`, `tail`, or `grep`.
Use IDs returned by tools, never literal example IDs. Read
`skills/common/run-loop.md` for re-entry, shared-job, and failure detail;
`skills/common/tool-output.md` for unfamiliar responses; and
`skills/common/guardrail-codes.md` after a structured failure. Use
`skills/md-prepare/setup.md` only to route to task-specific preparation pages.

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

On this standard Amber path, pass catalog names such as `ff19SB` and `opc` to
`build_amber_system`, or keep its defaults. The tool resolves the XML bundle;
do not translate catalog names into XML filenames, search the filesystem for
XML, or replace this step with `openmm.app.ForceField`.

Do **not** infer defaults from prior AMBER knowledge. Tool signatures and
guardrails are authoritative; the skill guidance provides quick references:

- `skills/md-prepare/defaults-and-guardrails.md` — preparation defaults,
  guardrails, and ligand failure policy
- `skills/common/solvent-regimes.md` — "Explicit-Water Constant Defaults" table
  (forcefield, water model, box geometry, salt) and regime mapping
- `skills/md-prepare/implicit-water.md` — implicit-solvent defaults

Open the matching solvent page before its `prep`, `solv`, or `topo` stage when
the regime is implicit, vacuum, or membrane, or when overriding explicit-water
defaults. Tool-level defaults do not belong in the Step 0 confirmation table.

## Workflow

Complete the pre-command gate, confirm the run identity, and then execute the
workflow below. Load a linked page only when its condition applies.

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
mdclaw inspect_job --job-dir <returned_job_dir>
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

1. Present the Step 0 run-identity summary below. `execution_mode=autonomous`
   unless the user requested checkpoint-by-checkpoint confirmation; it controls
   pauses, not the stopping stage.
2. Ensure the canonical study layout exists with `bootstrap_md_workflow` or a
   richer `md-study` plan. Use its returned `job_dir` for all DAG commands and
   run `inspect_job` once after bootstrap. For an older study missing workflow
   params, repair them with `update_workflow_state --params ...`.
3. Create, explain, and run the `source` node. Read `acquisition.md` only for a
   remote/generated source, biological assembly, or multi-candidate bundle.
4. Run `inspect_molecules`, confirm Step 0b, then create, explain, and run the
   `prep` node with `prepare_complex --solvent-type
   explicit|implicit|vacuum`. Read `inspection-and-chains.md` or
   `prepare-complex.md` when molecule selection is not trivial.
5. When requested, create an explicit mutation/PTM prep branch using
   `branches.md`. Read `ion-policy.md` or `prep-chemistry.md` only when the
   corresponding chemistry is present.
6. For explicit solvent, create, explain, and run a `solv` node using
   `explicit-water.md`; for membrane use `membrane.md`. Skip `solv` for
   implicit/vacuum and use `implicit-water.md` or the vacuum section of
   `skills/common/solvent-regimes.md`.
7. Run the platform preflight when later local compute is requested. Then
   create, explain, and run `topo`; let `build_amber_system` resolve the
   completed `solv` parent for explicit/membrane or `prep` parent for
   implicit/vacuum. Never pass a free-standing raw PDB.
8. After each completed structural node where human inspection is useful
   (`source`, `prep`, `solv`, `topo`), perform Visual QA per
   `skills/common/visual-qa.md` and register it with `register_visual_review`.
   Visual QA is only an obvious-accident check; never infer force-field,
   protonation, parameter, or chemistry correctness from the image. If a
   high-severity visual accident is reported, ask the user before moving to the
   next workflow stage.
9. The short minimization inside `topo` is initial topology relaxation only; it
   does not satisfy the `min` node contract. When the preparation request
   requires a minimized/relaxed state or post-minimization artifact, execute
   `topo` -> `min` and stop before equilibration unless equilibration was also
   requested. Otherwise report the completed topology and the `min` handoff.
   `/md-equilibration` is the shortcut for continuing beyond preparation.

## Step 0: Parse and Confirm

Run this after the pre-command gate and before bootstrap/source acquisition.
Confirm in two parts because chains and ligands can only be finalized after
molecule inspection.

Do **not** add forcefield, water model, box geometry, or any other tool-level
default to these tables; tools apply them unless the user explicitly named one.

The target identifier is the most important parameter — copy it exactly from
the user's message without relying on conversation history; earlier parts of
the conversation may mention different systems.

**Step 0 (before inspection)** — confirm the identity of the run:

| Parameter | Value |
|-----------|-------|
| Target | (PDB ID / sequence / file — exactly as the user wrote) |
| Solvent regime | explicit (default) / implicit / vacuum / membrane |
| Execution mode | `autonomous` (default) / `human_in_the_loop` |
| Mutations | (if any — one-letter notation, e.g. K27A) |
| Production length | (if specified) |
| Other | (only parameters the user explicitly named — do not pre-fill defaults here) |

**Step 0b (after `inspect_molecules`)** — confirm what to keep:

| Parameter | Value |
|-----------|-------|
| Chain(s) | (expand to ligand label chains when ligands should be included) |
| Ligands | include / exclude (use inspected ligand `unique_id` values) |

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
