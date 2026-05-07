# Implicit Solvent: Topology

Implicit solvent (Generalized Born) models represent water as a
continuum dielectric instead of explicit water molecules. Faster but
less accurate than explicit water.

## ⚠️ Current support status

`build_amber_system` under the openmmforcefields-unification refactor
**does not yet build implicit-solvent (GB) systems**. Passing
`--implicit-solvent OBC2` (or any other GB model name) returns a
structured error with code
`implicit_solvent_unsupported_under_openmmforcefields` because the
SystemGenerator path does not yet attach a GB force to the saved
`system.xml`. Omitting both `--box-dimensions` and `--implicit-solvent`
would build a vacuum (NoCutoff) System, which the run-side guardrail
also rejects (`Non-periodic topology without implicit_solvent would run
vacuum equilibration`).

Until the GB-aware build path ships, implicit-solvent runs require one
of these escape hatches:

- **Research mode**: build via `build_openmm_system` with a
  third-party / hand-curated OpenMM ForceField XML that already attaches
  the GB force you want. The shim's contract check passes when the
  saved System carries a `GBSAOBCForce` / `CustomGBForce` /
  `AmoebaGeneralizedKirkwoodForce`; the run-side `--implicit-solvent`
  flag then matches.
- **Skip implicit until then**: the rest of this runbook documents the
  intended (not-yet-shipped) interface so the workflow is ready when GB
  support lands. For real campaigns today, use explicit water (see
  `skills/md-prepare/explicit-water.md`).

---

## Decision Defaults

Quick reference only; Python tool signatures and guardrails are authoritative.

| Parameter | Default | User Cues |
|---|---|---|
| GB model | GBn2 (igb=8) | "obc", "obc2", "hct" |
| Salt concentration | 0.15 M | "0.3M", "no salt" |
| Force field | ff14SB | "ff19SB" (note: ff19SB is optimized for explicit water) |

**Force field choice**: default to `ff14SB` for implicit solvent.
`ff19SB` was parameterized against OPC explicit water and may be less
accurate under GB models; `ff14SB` remains the tested pair for implicit
solvent runs.

**Ligand note**: GBn2 remains the default starting model, but GBn/GBn2 neck
corrections can fail for some GAFF or curated ligand atom types. If production
fails with `Radii must be between 1 and 2 Angstroms for neck lookup`, branch a
new `eq`/`prod` path from the same topology using `--implicit-solvent OBC2`.

Prepare-time checkpoints (chain selection, ligand inclusion, metal
handling, confirmation loop) live in `setup.md` and apply identically
for both explicit- and implicit-solvent paths. The Metal ion handling
section in `setup.md` is relevant here too — `parameterize_metal_ion`
runs on the prep node regardless of solvent type.

---

## Step 4: Skip Solvation

No solvation step is needed for implicit solvent. Proceed directly to topology.

---

## Step 5: Build Topology (no box, no water) — *not yet supported via build_amber_system*

The intended call (kept here for reference; **currently rejected** —
see "Current support status" above):

```bash
mdclaw create_node --job-dir <job_dir> --node-type topo --parent-node-ids prep_001
mdclaw --job-dir <job_dir> --node-id topo_001 build_amber_system \
  --forcefield ff14SB \
  --implicit-solvent OBC2 \
  --no-is-membrane
```

Today this returns
`code="implicit_solvent_unsupported_under_openmmforcefields"`. To run
implicit-solvent MD until GB support is wired into the openmmforcefields
path, build via `build_openmm_system` with a GB-aware ForceField XML —
e.g. the Greener group's `GB99dms.xml` or a third-party amber14 + GB
port — and the same `system.xml` + `topology.pdb` + `state.xml` triple
flows through to `run_equilibration` / `run_production`.

> No `--box-dimensions` or `--water-model` needed for implicit solvent.
> Ligand parameters are auto-resolved from the `prep` ancestor's artifacts.
> Highly charged ligands and close contacts are recorded as topology
> diagnostics. They do not stop the workflow or select a special equilibration
> branch; `/md-equilibration` uses the same standard staged minimization and
> low-temperature warmup protocol for all systems.

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
