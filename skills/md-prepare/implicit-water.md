# Implicit Solvent: Topology

Implicit solvent (Generalized Born) models represent water as a
continuum dielectric instead of explicit water molecules. Faster but
less accurate than explicit water.

## Decision Defaults

Quick reference only; Python tool signatures and guardrails are authoritative.

| Parameter | Default | User Cues |
|---|---|---|
| GB model | GBn2 (igb=8) | "obc", "obc2", "hct" |
| Salt concentration | 0.15 M | "0.3M", "no salt" |
| Force field | ff14SB | "ff19SB" (note: ff19SB is OPC-tuned and warns under GB) |

**Force field choice**: the implicit-solvent default is `ff14SB`. When
`build_amber_system` sees `--forcefield ff14SB --implicit-solvent ...`,
it auto-substitutes the GBneck2-tuned variant `ff14SBonlysc` (the
implicit-tuned XML shipped by openmmforcefields) and surfaces a warning
so the substitution is visible. `ff19SB` was parameterized against OPC
explicit water and is not Amber25's recommended GB pair — a warning is
emitted; pass `--forcefield ff14SBonlysc` to silence it.

**GB model**: defaults to `GBn2` (igb=8) for accuracy. Other supported
models are case-insensitive: `HCT`, `OBC1`, `OBC2`, `GBn`, `GBn2`.
Unknown names fail-fast with `code="implicit_solvent_model_unsupported"`.

**Ligand note**: GBn2 remains the default starting model, but GBn/GBn2
neck corrections can fail for some GAFF or curated ligand atom types.
If production fails with `Radii must be between 1 and 2 Angstroms for
neck lookup`, branch a new `eq`/`prod` path from the same topology using
`--implicit-solvent OBC2`.

Prepare-time checkpoints (chain selection, ligand inclusion, metal
handling, confirmation loop) live in `setup.md` and apply identically
for both explicit- and implicit-solvent paths. The Metal ion handling
section in `setup.md` is relevant here too — `parameterize_metal_ion`
runs on the prep node regardless of solvent type.

---

## Step 4: Skip Solvation

No solvation step is needed for implicit solvent. Proceed directly to topology.

---

## Step 5: Build Topology (no box, no water)

```bash
mdclaw create_node --job-dir <job_dir> --node-type topo --parent-node-ids prep_001
mdclaw --job-dir <job_dir> --node-id topo_001 build_amber_system \
  --forcefield ff14SB \
  --implicit-solvent GBn2 \
  --no-is-membrane
```

`build_amber_system` resolves the matching GB XML from
`forcefield_catalog` (`implicit/gbn2.xml` for GBn2) and bakes the
resulting `CustomGBForce` / `GBSAOBCForce` into the saved `system.xml`.
The run-side shim verifies that force is present before honoring an
`--implicit-solvent` request, so accidental vacuum builds are caught.

Calling contract:
- No `--box-dimensions`, no `--water-model`. Combining `--implicit-solvent`
  with `--box-dimensions` returns
  `code="implicit_solvent_explicit_box_conflict"`.
- Ligand parameters auto-resolve from the `prep` ancestor's artifacts.
- Highly charged ligands and close contacts are recorded as topology
  diagnostics and do not stop the workflow or select a special
  equilibration branch — `/md-equilibration` uses the same standard
  staged minimization and low-temperature warmup protocol for all
  systems.
- For GB models that openmmforcefields does not ship (e.g. the Greener
  group's `GB99dms.xml`), use `build_openmm_system` with the
  third-party ForceField XML; the saved `system.xml` + `topology.pdb` +
  `state.xml` triple flows through eq/prod identically.
- When using `build_openmm_system` for shipped GB models, **also pass
  `--implicit-solvent <MODEL>`** so the topo node's metadata records the
  canonical name (`OBC2` / `GBn2` / …). The run-side topology guard
  then matches build-time and runtime choices on either path. Missing
  `implicit/<model>.xml` in `--forcefield-xml` triggers
  `implicit_solvent_xml_missing`; multiple `implicit/*.xml` without an
  explicit `--implicit-solvent` triggers `implicit_solvent_xml_ambiguous`.
  Third-party GB XML cannot be inferred — for fully custom GB
  research, leave `implicit_solvent` unset and accept that the run-side
  topology guard will not recognise the build choice.

### Domain Knowledge

**Generalized Born models** (fastest to most accurate):
- **HCT** (igb=1): Fastest, least accurate
- **OBC1** (igb=2): Good balance
- **OBC2** (igb=5): Better than OBC1 for most proteins
- **GBn** (igb=7): Improved neck correction
- **GBn2** (igb=8): Best accuracy, recommended default

**When to use implicit solvent**:
- Rapid conformational sampling (folding studies)
- Large systems where explicit water is too expensive
- Screening many mutants or ligands quickly

**Limitations**:
- No explicit water-mediated interactions
- Less accurate for surface-exposed residues
- Membrane systems not supported
- Salt bridge stability may differ from explicit water

---

## Handoff

1. Read `progress.json` — verify `topo_001` status is `completed`.
2. Tell the user:
   ```
   Preparation complete. Next:
     /md-equilibration <job_dir>
   ```
   `/md-prepare` does not auto-invoke equilibration — each stage is
   user-initiated.
