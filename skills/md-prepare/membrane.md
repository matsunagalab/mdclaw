# Membrane Embedding

Read this only for membrane systems (`solvent_regime = membrane`). Membrane
embedding replaces bulk solvation: use the same `solv` node type but run
`embed_in_membrane` instead of `solvate_structure`. First complete a `prep` node
(see `skills/md-prepare/happy-path.md` steps 1-4).

## Run

```bash
mdclaw create_node --job-dir <job_dir> --node-type solv
mdclaw --job-dir <job_dir> --node-id <solv_node_id> embed_in_membrane \
  --lipids POPC --ratio "1" --dist 15.0 --dist-wat 17.5 \
  --salt --saltcon 0.15
```

`pdb_file` auto-resolves from the `prep` parent's `merged_pdb` artifact; pass
`--pdb-file` only to override (e.g. a manually oriented PDB). On success the
solv node records `is_membrane=true`, so build topology with
`build_amber_system --is-membrane` (see `explicit-water.md`).

Use `--preoriented` only for structures that are already in a membrane frame
(for example OPM/PPM-derived coordinates). For beta-barrel membrane proteins,
pass `--memembed-beta-barrel` unless the job/task path or study text already
contains beta-barrel wording. The patch-tile backend passes MEMEMBED `-b` in
that mode, consumes MEMEMBED's oriented coordinates, and aligns the cached
lipid patch to MEMEMBED's dummy-membrane midplane.

By default the tool writes `membrane_embedding_geometry.json` and fails with
`membrane_embedding_geometry_failed` when a PBC-aware post-build check shows
that the protein does not intersect the lipid headgroup span. Treat that as a
real membrane-placement failure, not a cosmetic rendering issue.

## Backend

Membrane embedding defaults to the `patch-tile` backend
(`--membrane-backend patch-tile`): it builds a small composition-keyed lipid
patch once, equilibrates it under PBC, caches it, tiles it around the oriented
protein, and neutralizes net charge by swapping bulk waters for ions. This
converges reliably for cholesterol mixtures (e.g. `POPC:POPE:CHL1 2:1:1`) that
full-box packing struggles with.

- `--membrane-backend packmol-memgen`: force the legacy full-box packing path.
- `--membrane-backend auto`: try patch-tile first, fall back to packmol-memgen.

## Timing and cold-build

The first time a given lipid composition is requested (and it is not in the
writable or bundled patch cache), the `solv` step runs a one-time OpenMM
equilibration of the patch (minimization + a few hundred ps of MD), which can
take several minutes on CPU. When this happens the tool prints a
`[mdclaw] patch-tile: ...` notice to stderr, adds it to `warnings`, sets
`patch_cold_build_notice`, and reports `patch_build` /
`parameters.patch_equilibration_ran`. Tell the user up front that the first
build of a new composition takes a few extra minutes and is cached for reuse.
Common compositions are pre-built into a read-only bundled cache.

Membrane embedding is long-running: tens of minutes are normal for large or
mixed systems, and CPU runs (including a cold patch equilibration) may exceed an
hour. Do not assume a running membrane `solv` node is hung just because it has
run for 10-30 minutes. Continue monitoring or `explain_node` the same node until
it completes, fails, or reaches its timeout; do not create a sibling `solv` node
to retry an in-progress build. Run it in the foreground for autonomous
benchmark-style tasks and do not package a final submission while it is still
`running`. For a simple blocking check:

```bash
mdclaw wait_node --job-dir <job_dir> --node-id <solv_node_id> \
  --timeout-seconds 7200 --poll-interval-seconds 30
```

## Packmol race and salt override

Membrane embedding runs MDClaw's bounded Packmol retry plan as a 4-lane parallel
race by default (`--packmol-race-lanes 4`). Use `--packmol-race-lanes 1` only on
CPU-constrained/shared hosts when preserving sequential behavior matters more
than wall time. If neutralization needs a higher ion concentration than the
requested `--saltcon` (default 0.15 M), MDClaw automatically reruns
packmol-memgen with `--salt_override` without changing the explicit-solvent
mode, recording a warning plus provenance metadata.

On retry, keep the requested lipid species, ratios, solute identity, solvent
regime, and force-field intent fixed. Retries may adjust packing controls,
random seed, or the recommended lateral box/buffer via `dist`; keep `leaflet`
and `dist_wat` unchanged unless the user explicitly asks for a thicker
membrane/water slab. Do not manually increase Packmol loop counts after a
failure unless running a deliberate debugging experiment. If the requested
target appears infeasible, stop and report it rather than silently simplifying
the system. Packmol may also write a raw `*_FORCED` PDB when it cannot find a
perfect packing; never pass that to topology generation (it bypasses final
AMBER/LIPID residue-name postprocessing).

## Structured outcomes

| `code` / metadata | Meaning | Agent response |
|---|---|---|
| `packmol_imperfect_primary_output_candidate` | Packmol did not reach perfect packing after the bounded retry, but a postprocessed primary PDB was written. | Continue to `build_amber_system` and `run_minimization`; trust it only if topology load, finite energy, and minimization checks pass. |
| `packmol_packing_quality_failed` + `retry_membrane_with_larger_box` | No perfect packing after bounded adaptive retry; the box is not MD-ready. | Retry only with the CLI-provided larger xy/lateral box suggestion unless geometry was explicitly fixed. |
| `forced_output_available` metadata | A `*_FORCED` PDB was written during a failed attempt. | Keep for debugging/provenance only; do not feed to topology generation. |
| `salt_override_required` metadata | Neutralization needs more ions than the requested salt concentration. | Accept the automatic `--salt_override` rerun and record the warning/provenance. |
| `membrane_embedding_geometry_failed` | Protein/lipid placement failed the post-build PBC-aware bilayer-intersection check. | Retry only after fixing orientation or membrane settings; for beta barrels use `--memembed-beta-barrel` and avoid `--preoriented` unless the input is truly pre-oriented. |
