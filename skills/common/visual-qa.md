# Visual QA

The canonical structure-preview + visual-review procedure for the stages
`prep`, `solv`, `min`, `eq`, `prod`, and `analyze`. Reference this page instead
of duplicating the checklist per skill.

**Visual QA is off by default. Run it only when the user asks for a preview or
a visual check**, or when a stage reports a geometry problem that a picture
would settle. Do not render after every stage. A preview costs a multi-megabyte
PNG per node, and once such an image is read into the conversation it is re-sent
with every later turn: measured 2026-08-25 on a hundred-task benchmark run,
previews nobody asked for accounted for half of all input tokens.

When the user has asked for it, render it in `autonomous` mode too: autonomous
skips confirmations, not reporting. Rendering itself is best-effort — a failure
to render is not a stage failure.

**Never open a preview on a text-only model.** If the model cannot accept image
input, reading the PNG spends the context window on data it cannot see. Give the
user the paths and register the review as `not_available` (see below) instead.

What the picture can tell you is limited. It catches obvious visual accidents;
it does not validate force fields, protonation states, parameters, chemistry,
or small clashes. Never mark a DAG node failed from visual QA alone.

## Render a preview

```bash
mdclaw --job-dir <job_dir> --node-id <node_id> \
  render_structure_preview --style overview --ray
```

In node mode, `render_structure_preview` resolves `structure_file` from node
artifacts; pass `--structure-file` only to override.

| When | Style |
|---|---|
| An assembled system: after `solv`, membrane embedding, `min`, `eq`, `prod` | `system_box` |
| After `prep` — there is no solvent or box yet | `overview` |
| A ligand binding site | `ligand_site` |
| Water/ion placement specifically | `solvent_ions --show-solvent` |
| Anything else | `overview` |

`system_box` draws the system as built: protein as cartoon coloured per chain,
lipids as sticks, water as a transparent surface, ions as spheres, everything
else as sticks, and the periodic cell as a wire box around it. That box is the
point — it is what shows whether the system fits in its own cell.

`system_box` writes two axis-aligned orthographic views:
`structure_preview_png` down x, with z vertical, and `structure_preview_png_top`
down z. Send both — one projection hides whatever lines up with it.

**Surface the PNGs to the user, do not just write them.** Rendering a preview
the user never sees is the same as not rendering one. Send them as files so they
appear inline in the desktop app, with a one-line caption naming the node and
stage. If the harness cannot deliver files, print the absolute paths. If PyMOL
is unavailable (`code=pymol_not_available`), say rendering was skipped; it is
not a failure.

## Inspect the images

Only if the model accepts image input. Open both `structure_preview_png` and
`structure_preview_png_top` and check only:

- The main structure is visible and not cut off.
- Expected components (protein/nucleic/ligand/lipid/water/ion) are not
  obviously missing.
- Ligands or cofactors are not obviously far away from the expected complex.
- Membrane systems do not show an obviously broken protein/membrane placement.
- Nothing crosses the periodic cell drawn around the system.
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
