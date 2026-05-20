# Implicit Solvent: Topology

Implicit solvent (Generalized Born) models represent water as a
continuum dielectric instead of explicit water molecules. Faster but
less accurate than explicit water.

## Decision Defaults

Quick reference only; Python tool signatures and guardrails are authoritative.

| Parameter | Default | User Cues |
|---|---|---|
| GB model | GBn2 (igb=8) | "obc", "obc2", "hct" |
| Salt concentration | continuum model only | "explicit ions" means use explicit solvent instead |
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

Prepare-time checkpoints (chain selection, ligand inclusion, confirmation
loop) live in `setup.md`. Ion handling is different from explicit solvent:
implicit solvent must not retain crystallographic or bulk ions as explicit
particles. If the scientific request requires those ions, switch to the
explicit-solvent path or make a deliberate vacuum/no-solvent choice. Otherwise
exclude ion residues during preparation.

---

## Step 4: Skip Solvation And Explicit Ions

No solvation step is needed for implicit solvent. Proceed directly to topology.
Before topology, verify the prep `merged_pdb` contains no explicit ion residues
such as CA, K, NA, CL, MG, or ZN. If ions remain, create a new prep branch
without `ion` in `--include-types`; do not parameterize them or pass them into
an implicit topology.

---

## Step 5: Build Topology (no box, no water)

```bash
mdclaw create_node --job-dir <job_dir> --node-type topo --parent-node-ids prep_001
mdclaw --job-dir <job_dir> --node-id topo_001 build_amber_system \
  --forcefield ff14SB \
  --implicit-solvent GBn2 \
  --no-is-membrane
```

The input PDB is auto-resolved from the completed `prep` parent's `merged_pdb`.
Do not pass a manual `--pdb-file`; if the prep artifact is wrong, branch a new
`prep` node and build topology from that completed node. Pass
`--solvent-type implicit` to `prepare_complex` so explicit crystallographic
ions are excluded and recorded in `component_disposition.json` before
`merged_pdb` is written.

`build_amber_system` resolves the matching GB XML from
`forcefield_catalog` (`implicit/gbn2.xml` for GBn2) and bakes the
resulting `CustomGBForce` / `GBSAOBCForce` into the saved `system.xml`.
The run-side shim verifies that force is present before honoring an
`--implicit-solvent` request, so accidental vacuum builds are caught.

Calling contract:
- No `--box-dimensions`, no `--water-model`. Combining `--implicit-solvent`
  with `--box-dimensions` returns
  `code="implicit_solvent_explicit_box_conflict"`.
- The input PDB must not contain explicit ions. `prepare_complex
  --solvent-type implicit` excludes them during prep; `build_amber_system`
  validates the invariant and returns `code="explicit_ions_in_implicit_solvent"`
  if ions still reach topology.
- Ligand parameters auto-resolve from the `prep` ancestor's artifacts.
- Highly charged ligands and close contacts are recorded as topology
  diagnostics and do not stop the workflow or select a special
  equilibration branch — the equilibration skill uses the same standard
  staged minimization and low-temperature warmup protocol for all
  systems.

### Implicit-solvent paths in MDClaw

| Path | Command | Coverage |
|---|---|---|
| **Standard (recommended)** | `build_amber_system --implicit-solvent <MODEL>` | Full catalog integration; metadata + run-side topology guard match by canonical name. |
| **Research, shipped XML** | `build_openmm_system --forcefield-xml … implicit/<model>.xml --implicit-solvent <MODEL>` | Same metadata contract, but the user owns the XML bundle. ``--implicit-solvent`` is required for the topology guard to match — pass the canonical name explicitly. |
| **Research, external XML** | `build_openmm_system --forcefield-xml … <custom_GB>.xml` | Advanced escape hatch. mdclaw cannot canonicalize a third-party GB XML (e.g. the Greener group's `GB99dms.xml`), so the topo node's `metadata.implicit_solvent` stays `None` and the run-side topology guard cannot validate the build/runtime match. The user must manage XML correctness, GB-force presence, and run-time consistency themselves. |

Officially supported implicit-water models (catalog + run-side guard
recognition): **HCT, OBC1, OBC2, GBn, GBn2**.

Failure codes you may see (build side):
- `implicit_solvent_model_unsupported` — name is not in the catalog (typo
  / drift). The error message lists the supported set.
- `implicit_solvent_explicit_box_conflict` — `--implicit-solvent` paired
  with `--box-dimensions`.
- `implicit_solvent_xml_missing` (`build_openmm_system` only) — declared
  model whose `implicit/<model>.xml` is not in `--forcefield-xml`.
- `implicit_solvent_xml_ambiguous` (`build_openmm_system` only) —
  multiple shipped `implicit/*.xml` in the bundle without an explicit
  `--implicit-solvent`.
- `implicit_solvent_force_missing` — XML loaded but the built System
  carries no `GBSAOBCForce` / `CustomGBForce` /
  `AmoebaGeneralizedKirkwoodForce`.

Failure codes you may see (run side, after the topology resolver):
- `implicit_solvent_topology_mismatch` — topo
  `metadata.implicit_solvent` and the runtime `--implicit-solvent`
  disagree after canonicalization. Aliases (`gbneck2` ↔ `GBn2`, `obc2`
  ↔ `OBC2`, `igb1`–`igb8`) match; different models do not. Rebuild the
  topo node, or rerun with the canonical name the topo carries.

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
     Continue with skills/md-equilibration/SKILL.md on this job_dir.
     Shortcut, if available: /md-equilibration <job_dir>
   ```
   Preparation does not auto-invoke equilibration — each stage is
   user-initiated.
