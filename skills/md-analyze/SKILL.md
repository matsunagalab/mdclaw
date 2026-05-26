---
name: md-analyze
description: "Molecular dynamics trajectory analysis using MDClaw CLI tools. Routes concat, metric, and troubleshooting workflows through focused guidance pages."
---

# MD Analyze

Read `skills/common/preamble.md`, `skills/common/tool-output.md`, and
`skills/common/node-cli-patterns.md` before acting.

Analysis is always user-initiated. Production does not chain into analysis;
the user or harness invokes this skill when ready. In harnesses with slash
commands, `/md-analyze` is the shortcut.

If the job belongs to a study with `study_plan.json`, use the plan's `analysis`
list as the starting point for metric selection. Treat it as scientific intent,
not as a brittle execution contract: missing or incomplete plan fields should
not block normal analysis.

## Route To The Right Guidance

- Combine a production lineage into an analysis trajectory:
  `skills/md-analyze/concat.md`
- RMSD, RMSF, contacts, distances, hydrogen bonds, or energy summaries:
  `skills/md-analyze/metrics.md`
- Errors, missing artifacts, bad selections, or empty DCDs:
  `skills/md-analyze/troubleshooting.md`
- Legacy notes for current analysis helpers:
  `skills/md-analyze/analysis.md`

## Step 0 Summary

Confirm these fields before running analysis:

| Parameter | Value |
|-----------|-------|
| Target | job directory |
| Analysis data scope | segment, production_chain, or comparison |
| Analysis subjects | optional for segment/production_chain; required for comparison |
| Comparison mapping | required for different chains/topologies; initial types: `residue_number`, `atom_selection` |
| Validation | require `analysis_data_scope`; comparison is binary/pairwise with two unique subject `label`s |
| Leaf prod node | requested node or deepest continuation leaf |
| Atom selection | mdtraj selection, default `"protein"` |
| Stride | integer, default `1` |

For comparison analyses, create the node with explicit subjects and mapping:
Use two completed `production_chain` analyze nodes as parents.
Put `analysis_subjects` and `comparison_mapping` on the comparison node itself,
not on the parent `production_chain` analyze nodes.
The resolver still exposes multi-parent analyze inputs as `branches_input` for
tool compatibility.
For `residue_number` mappings, each reference uses
`subject_label:residue_id`; the `residue_id` is a string, not a number.
For `atom_selection` mappings, selection values are mdtraj selection strings.

```bash
mdclaw create_node --job-dir <job_dir> --node-type analyze \
  --parent-node-ids <analyze_apo> <analyze_holo> \
  --label "apo_vs_holo" \
  --conditions '{"analysis_data_scope": "comparison",
                 "analysis_subjects": [
                   {"label": "apo"},
                   {"label": "holo"}
                 ],
                 "comparison_mapping": {
                   "type": "residue_number",
                   "pairs": [["apo:10", "holo:10"]]
                 }}'
```

Create an `analyze` node first, then run analysis tools with both `--job-dir`
and `--node-id`.

## Structure Preview

When the user wants a human-readable structural snapshot, or when a completed
prod/analyze artifact would benefit from visual inspection, run:

```bash
mdclaw --job-dir <job_dir> --node-id <node_id> \
  render_structure_preview --style overview --ray
```

In node mode, `render_structure_preview` resolves `structure_file` from node
artifacts; pass `--structure-file` only to override.

Prefer `--style ligand_site` for ligand binding sites, `--style membrane` for
membrane systems, and `--style solvent_ions --show-solvent` when water/ion
placement is the inspection target. If `output_png` / `structure_preview_png`
is produced, display it in image-capable agent UIs; otherwise provide the node
ID, caption, PNG path, and source structure artifact. If PyMOL is unavailable
(`code=pymol_not_available`), report that preview rendering was skipped rather
than treating it as an analysis failure.

## Visual QA

Visual QA is optional and best-effort. It is only for catching obvious visual
accidents, not for validating force fields, protonation states, parameters,
chemistry, or small clashes.

If the current agent/UI can inspect images, open the `structure_preview_png`
and check only:

- The main structure is visible and not cut off.
- Expected components (protein/nucleic/ligand/lipid/water/ion) are not
  obviously missing.
- Ligands or cofactors are not obviously far away from the expected complex.
- Membrane systems do not show an obviously broken protein/membrane placement.
- Water, ions, or lipids do not form impossible-looking clumps, isolation, or
  severe overlap.
- Anything not visible from the image is explicitly marked as not assessable.

Record the result with:

```bash
mdclaw --job-dir <job_dir> --node-id <node_id> \
  register_visual_review --reviewer-type multimodal_llm \
  --severity none --recommendation continue \
  --summary "No obvious visual accident detected."
```

If the agent cannot inspect images, register `--reviewer-type not_available
--severity not_reviewed --recommendation manual_review` and show the PNG path
to the user. If severity is `high`, ask the user before advancing to the next
workflow step, but do not mark the DAG node failed solely from visual QA.
