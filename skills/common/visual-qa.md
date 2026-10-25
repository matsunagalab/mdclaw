---
title: Molecular display and visual QA
category: skill-reference
updated: 2026-10-25
---

# Molecular Display and Visual QA

For a request to show, rotate, or play a molecule or trajectory, first select
the display below. This applies to existing files as well as workflow artifacts.
Resolve the requested files from the conversation or `inspect_job` results;
ask only if the target remains ambiguous. Viewing existing results does not
require a new study or simulation. Reuse display-ready artifacts; a requested
display transformation may need derived artifacts as described below.

## Select the display

| User intent | Action |
|---|---|
| Play a trajectory, rotate/zoom interactively, or use Molstar (「再生して」「回して見たい」「Molstarで」) | Check the interactive routes below, then use Molstar. |
| Prepared full-system view (「準備後の全系を見せて」「水や膜の入り方を見たい」), without an explicit still-image request | Use Molstar when available; preserve the assembled system and apply the preparation row below. |
| A still image, PNG, or PyMOL image (「静止画」「PNGにして」) | Use the PyMOL preview below, even if Molstar is available. |
| Simply show a structure, with no interactive request | Use a PyMOL preview. |
| Both interactive viewing and a still image | Deliver both through their respective routes. |
| No display request | Leave visualization off, except for the geometry diagnostic described below. |

## Select the stage and contents

Use the producing node and the user's purpose, not the filename or the viewer
chosen. An `analyze` artifact inherits the stage of its source trajectory.
An explicit request to inspect water, ions, or a binding site overrides the
defaults below. For 「prep直後の全系」 with water, resolve the completed solvated/assembled
structure (solv/topo), even if the protein-only DAG node is called prep. Use the
existing prepared snapshot rather than a production frame or a protein-only
analysis export. For an external file, use the stated purpose; if none is given,
show its components without claiming a workflow stage.

| Stage / purpose | Contents and representation | PyMOL style |
|---|---|---|
| Source / protein preparation before solvation | Protein/nucleic acid cartoon; ligand and other non-solvent components as sticks; relevant ions as spheres. Show only components actually present. | `overview` |
| Solvation, membrane assembly, topology, minimization; checking packing or equilibration of the assembled system | Protein/nucleic acid cartoon; ligand and lipids as sticks; water as a transparent surface; ions as spheres; periodic cell when supported. Preserve the full system. | `system_box` |
| Production playback / conformational analysis | Protein/nucleic acid cartoon; keep ligand, cofactors, and lipids as sticks. Hide water, including its surface; retain relevant ions. Apply the trajectory procedure below. | `membrane` when lipids are present, otherwise `overview` with solvent hidden |
| Ligand binding site | Keep the ligand and nearby polymer atoms as sticks. | `ligand_site` |
| Water/ion placement explicitly requested | Preserve the requested solvent/ions; use a transparent water surface for bulk packing and spheres for ions. | `system_box` for bulk packing; `solvent_ions --show-solvent` only for individual solvent inspection |

Water's surface is a visual envelope of the present coordinates, not a computed
density or proof of packing quality. Avoid drawing all water bonds as lines.
Show two orthogonal views of an assembled system so the membrane edge and plane
can both be inspected. If a surface exceeds the renderer's resource limit,
report its actual fallback rather than claiming the surface was displayed.

Apply the same content policy in Molstar and PyMOL. The current Molstar HTTP and MCP
viewers apply the stage representations automatically: polymer cartoon, lipid
and ligand sticks, ion spheres, and a transparent Gaussian water surface for
preparation. The `stage` argument selects the table's source stage. Pass it
explicitly when the source stage is known; auto uses the trajectory's DAG path,
otherwise prod for DCD and prep for PDB. PyMOL uses the style column above.

For production playback, long trajectories, PBC correction, or requested
alignment, follow [trajectory-display.md](trajectory-display.md) before opening
the viewer. Keep lipids and ligands: the analysis tool's default `protein`
selection is unsuitable for a membrane/complex display.

## Check the interactive route

Check capabilities in the **current session**, not an app name, OS, environment
variable, or a previous conversation. Codex/Claude Code desktop, CLI, and remote
sessions can expose different tools. Discover available tools before asking the
user which app they use. Recheck after a connection changes or a display fails.

For an interactive request, choose the first usable route:

1. **Attached MCP viewer:** discover the available `open_molstar_probe` tool
   (normally under `mdclaw_molstar`; older connections use `mdclaw_molstar_probe`)
   and inspect its current schema. With stage options, pass absolute source paths
   and the known `stage`, keeping wrap on and fitting off unless requested.
   With an older schema, first use the **existing-connection compatibility path**
   in [trajectory-display.md](trajectory-display.md): reuse or prepare a matching
   host-side display copy and receipt, then pass its PDB/DCD pair. This also
   supports a preparation snapshot as one real coordinate frame, without a new
   simulation. Open a new attached viewer to load updated UI code.
   Check the displayed stage, wrap, stride and fit state. Treat call success and
   visible rendering separately; if the client reports an error, inspect its
   exact message and source/receipt before choosing another route.
2. **Reachable browser:** use `serve_molstar` when a browser/preview surface is
   available and can reach the file host. A browser on the same machine or an
   already established forwarding route qualifies; a host-side localhost URL
   alone does not establish reachability from a remote client. Start the server,
   open the complete printed `viewer_url`, and verify that the requested
   molecule loads. This route supports PDB alone or PDB+DCD. Retain the server
   process handle for cleanup. Read
   [the viewer guide](../../docs/developer/molstar-viewer.md) for invocation and
   lifecycle details; read it from the checkout if only skills were installed.
3. **Static fallback:** if neither route is available or succeeds, state the
   concrete reason briefly and render a PyMOL PNG of the requested structure.
   Label it as a static substitute; it does not satisfy trajectory playback.
   If PyMOL also fails, report the error and provide the existing artifact paths.

Use existing connections. Installing/registering an MCP server, restarting the
agent app, adding SSH forwarding, or publishing a web endpoint is a separate
setup task, not an automatic fallback. Keep the DCD on its host and transfer
only topology and requested frames through the supplied viewer. Molstar uses
orthographic camera by default. For a UI update, open a new attached viewer;
existing widgets may retain old code (the current playback UI shows
`playback up to 60 fps`, an upper target rather than a measured rate).
For uneven remote playback, pause older widgets, open a fresh viewer, and let
its bounded buffer fill. Record measured fps, target, update/wait time and cache
occupancy before attributing a slowdown to transport or drawing. The viewer
retains visited coordinates within its cache budget and lowers the target when
necessary; do not add fitting or discard more scientific frames just to raise fps.

An interactive display request ends after delivering the requested viewer or
explaining and delivering the fallback. It does not by itself request a visual
review or `register_visual_review`. Continue below to render a static image;
inspect/register a review only for an explicit visual check or geometry diagnosis.

## Visual QA scope

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

On a text-only model, deliver the requested viewer or PNG through the available
user-facing surface, but skip model image inspection. If attachments are not
supported, give the paths. For a requested review, register `not_available`
(see below); opening a viewer does not establish model image capability.

What the picture can tell you is limited. It catches obvious visual accidents;
it does not validate force fields, protonation states, parameters, chemistry,
or small clashes. Never mark a DAG node failed from visual QA alone.

## Render a preview

For an existing file outside a workflow, use direct mode:

```bash
mdclaw render_structure_preview --structure-file /absolute/path/structure.pdb \
  --output-dir /absolute/path/previews --style overview --ray
```

For an existing completed workflow node:

```bash
mdclaw --job-dir <job_dir> --node-id <node_id> \
  render_structure_preview --style overview --ray
```

In node mode, `render_structure_preview` resolves `structure_file` from node
artifacts; pass `--structure-file` only to override. Select the style from the
stage table above (the command shows `overview` only as an example). Use direct
mode for a plain display if no completed node is suitable; do not create a node
just to view a display-ready file.

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
