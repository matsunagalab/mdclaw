# Visual QA

The canonical structure-preview + visual-review procedure shared by every
stage (prep, equilibration, production, analyze). Reference this page instead
of duplicating the checklist per skill.

Visual QA is optional and best-effort. It catches only obvious visual
accidents; it does not validate force fields, protonation states, parameters,
chemistry, or small clashes. Never mark a DAG node failed from visual QA alone.

## Render a preview

```bash
mdclaw --job-dir <job_dir> --node-id <node_id> \
  render_structure_preview --style overview --ray
```

In node mode, `render_structure_preview` resolves `structure_file` from node
artifacts; pass `--structure-file` only to override. Prefer `--style
ligand_site` for ligand binding sites, `--style membrane` for membrane systems,
and `--style solvent_ions --show-solvent` when water/ion placement is the
inspection target.

If `output_png` / `structure_preview_png` is produced, display it in
image-capable agent UIs; otherwise provide the node ID, caption, PNG path, and
source structure artifact. If PyMOL is unavailable (`code=pymol_not_available`),
report that preview rendering was skipped rather than treating it as a failure.

## Inspect (if the agent/UI can see images)

Open `structure_preview_png` and check only:

- The main structure is visible and not cut off.
- Expected components (protein/nucleic/ligand/lipid/water/ion) are not
  obviously missing.
- Ligands or cofactors are not obviously far away from the expected complex.
- Membrane systems do not show an obviously broken protein/membrane placement.
- Water, ions, or lipids do not form impossible-looking clumps, isolation, or
  severe overlap.
- Anything not visible from the image is explicitly marked as not assessable.

## Record the review

```bash
mdclaw --job-dir <job_dir> --node-id <node_id> \
  register_visual_review --reviewer-type multimodal_llm \
  --severity none --recommendation continue \
  --summary "No obvious visual accident detected."
```

If the agent cannot inspect images, register `--reviewer-type not_available
--severity not_reviewed --recommendation manual_review` and show the PNG path to
the user. If `severity` is `high`, ask the user before advancing to the next
workflow step.
