# Implicit Solvent: Topology

Implicit solvent (Generalized Born) models represent water as a
continuum dielectric instead of explicit water molecules. Faster but
less accurate than explicit water: no water-mediated interactions, less
accurate for surface-exposed residues, salt-bridge stability may differ,
and membrane systems are not supported. Read
`skills/common/implicit-solvent-contract.md` for the supported model set
(HCT, OBC1, OBC2, GBn, GBn2; GBn2 recommended), the build/run validation
contract, and the research/external-XML paths.

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

**Ligand note**: if production later fails with the GBn2 neck-radius error,
branch to `OBC2` per the fallback in `skills/md-production/implicit-water.md`;
GBn2 remains the default starting model.

Ion policy by regime is in `skills/common/solvent-regimes.md`: implicit
solvent must not retain crystallographic or bulk ions as explicit particles.

---

## Skip Solvation And Explicit Ions

No solvation step is needed for implicit solvent. Proceed directly to topology.
Before topology, verify the prep `merged_pdb` contains no explicit ion residues
such as NA, CL, K, MG, CA, MN, or ZN. If ions remain, create a new prep branch
without `ion` in `--include-types`; do not parameterize them or pass them into
an implicit topology.

---

## Build Topology (no box, no water)

```bash
mdclaw create_node --job-dir <job_dir> --node-type topo
mdclaw explain_node --job-dir <job_dir> --node-id <topo_node_id>
mdclaw --job-dir <job_dir> --node-id <topo_node_id> build_amber_system \
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
resulting GB force into the saved `system.xml`; the run-side layers that
validate this are in `skills/common/implicit-solvent-contract.md`.

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
  standalone `min` node followed by low-temperature warmup for all systems.

---

## Handoff

Verify the `topo` node is `completed` in `progress.json`, then follow the
canonical handoff in `SKILL.md` (continue with
`skills/md-equilibration/SKILL.md` on this `job_dir`; shortcut
`/md-equilibration`) when the current request continues beyond preparation.
