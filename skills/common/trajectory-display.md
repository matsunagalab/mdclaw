---
title: Trajectory preparation for display
category: skill-reference
updated: 2026-10-25
---

# Trajectory Preparation for Display

Follow this page for production playback, long trajectories, PBC correction,
requested alignment, or a preparation snapshot through an older MCP connection. [visual-qa.md](visual-qa.md) owns display selection
and representations. The Molstar frame server transforms requested coordinates on the compute host
without rewriting the original topology or trajectory. A disk copy is not
required for ordinary viewing.

## Invoke the integrated viewer

For a current Molstar HTTP/MCP schema, pass the source `stage`, leave `wrap=true`,
use `max_frames=1000` for a production overview (`0` for all frames), and leave
`fit=false`. Set `fit=true` only after an explicit fitting request. The server
performs the processing below and sends a matching filtered PDB plus requested
coordinate frames. Do not run concat or fit again on that same stream.

Check the returned `display` metadata and the viewer's processing status:
source/output counts, stride, wrap state, fit state/convergence, and warnings.
An old MCP connection may still expose the old schema and show a Legacy notice;
it does not acquire host-side processing merely because the HTML was refreshed.
Use the compatibility path below when the current connection lacks those options.

## Existing-connection compatibility path

Use this path when `open_molstar_probe` accepts only PDB/DCD paths and the updated
viewer code is available. Keep the current connection and application running.

1. Resolve the original completed stage artifact. Reuse a display copy only when
   its source hashes, stage, atom selection, stride, wrap and fit match the request.
2. Otherwise use the shared `DisplayData` processing on the host, with the known
   stage and requested options. Save a separate display PDB/DCD and source/output
   hashes outside the simulation artifacts. Follow the receipt format and export
   procedure in [the viewer guide](../../docs/developer/molstar-viewer.md#existing-mcp-connection-without-restarting-codex).
   This is display preprocessing, not a new MD or analysis job.
3. For a prepared single structure, retain the entire assembled system and write
   one frame of its actual coordinates to the legacy DCD container. Label it a
   preparation snapshot; add no motion. The updated UI uses its PDB coordinates,
   hides playback, and shows the periodic cell when unfitted. For production,
   apply the normal water removal and sampling below, retaining lipids/ligands.
4. Open the pair with explicit absolute host paths. Confirm `Host-prepared`, stage,
   `wrap: applied` (or the actual skip reason), stride and `fit: off` unless requested.
   Say this uses host-prepared data through the old connection; the server itself
   was not hot-reloaded. Future targets need their own matching copy.

Use the current exporter so discarded-water TER records are also removed.
An RPC timeout alone does not establish a fixed payload-size limit: both a
314 KB production PDB and a 6.5 MB whole-system PDB were displayed successfully.
Use client-visible errors to diagnose a failed display; report an unverified
remote rendering as pending rather than done.

## Decide the scope

1. Resolve the requested production chain/segment, matching topology, atom
   identities, and per-frame cell information. Reuse an existing display copy
   only if its source, selection, stride, PBC treatment, and fitting match.
2. Preserve protein, nucleic acid, ligand/cofactors, lipids, and relevant ions.
   For routine production viewing, remove water from the display copy, not just
   its representation, to reduce host-to-client traffic. `not water` is a
   starting selection; verify model-specific water/virtual-site residue names
   and component counts. Never silently use concat's default `protein` selection.
3. For a long overview with no requested sampling interval, retain at most
   about 1,000 frames using a uniform stride `max(1, ceil(N / 1000))`, where N
   is the number of source frames in the requested scope. Short trajectories
   keep every frame. Honour an explicit frame range, stride, or all-frame request.
   For continuation segments, preserve their order and record the source frame
   indices; account for per-segment stride resets when reporting output counts.
   Use recorded simulation times where available; frame index and playback fps
   do not establish physical time. Subsampling is for an overview, not event
   detection or quantitative analysis.

## Process in the right order

For each retained frame: reconstruct whole molecules from verified connectivity
and its periodic cell; centre the chosen solute/complex and image other whole
molecules around it; then strip display-excluded atoms. Keep bound ligands and
the membrane near the solute. Do not independently reduce each atom modulo the
box: that splits proteins, lipids, and ligands at the boundary. Validate several
frames, including a boundary crossing, and retain a matching PDB and DCD atom order.

PBC imaging is the normal periodic-trajectory display preparation. If the
trajectory is nonperiodic, skip it explicitly. If box information/connectivity
is missing, or the imaging method does not support the cell shape, report that
PBC correction is unavailable rather than guessing a box or claiming success.
For diffusion, transport, membrane motion, or unwrapped-path requests, preserve
that requested coordinate convention instead of imposing an overview image.

Only when the user asks to align/superpose/remove tumbling (「重ねて」「揃えて」),
fit after PBC correction. Default to a least-squares fit of `protein and backbone`
to an iteratively refined average; use a user-specified domain or reference when
given. Validate the atom selection. Apply the same rigid transform to all retained
atoms, including ligand and lipids, so relative positions are preserved.
For membrane orientation or whole-protein motion, leave fitting off unless
explicitly requested. Fitting removes translation/rotation, not internal motion.

The integrated `fit=true` path iterates Kabsch fitting and averaging on retained
protein backbone coordinates in bounded memory (10 iterations, tolerance
0.0001 nm). For a requested persistent analysis artifact instead, use
`fit_trajectory --selection "protein and backbone" --reference average` on
PBC-corrected files under its normal tool contract. Check and
report convergence; a mean of unaligned coordinates is not this procedure.
For a sampled display copy, label the reference as the average of the retained
frames, not the full trajectory. The average is an alignment reference, not an
observed simulation frame. Rotation also changes the cell orientation: hide the
cell in a fitted view unless the renderer can carry the transformed vectors.

## Processing and limits

- `DisplayData` is shared by both Molstar transports. It reads one DCD frame at
  a time, uses MDTraj whole-molecule imaging with explicit polymer anchors and
  verified PDB connectivity, then removes water for production. Orthorhombic
  and triclinic cells are supported. OpenMM's topology parser supplies standard
  terminal/force-field aliases; unknown disconnected residues require a bonded
  PDB with CONECT. Missing cells are reported as skipped, not corrected.
- Production uniformly samples at most `max_frames`; preparation stages retain
  all frames and water. Lipids, ligand, and ions remain. The source-frame index
  is shown beside the display-frame index; it is not a physical time estimate.
- The viewer shows a unit cell for an unfitted static structure. It omits the
  cell for DCD playback rather than showing stale PDB cell dimensions during NPT.
- A requested fit uses the sampled frames' average and applies the same transform
  to all displayed components. It reports nonconvergence; fitting is off by default.
- `concat_trajectory` and `fit_trajectory` remain available for persistent derived
  analysis files, but neither alone performs PBC imaging. Do not confuse those
  existing analysis tools with the integrated viewer's coordinate processing.

For persistent derived files, follow the existing analysis/DAG contract. Display
preprocessing never authorizes new preparation, equilibration, or production
simulations. Keep the original files on their host and report the actual
selection, stride, PBC state and fitting applied. A static fallback does not
repair a PBC problem.
