# Implicit Solvent Contract

Shared by equilibration and production implicit-solvent pages. It defines the
supported models and the build/run validation contract; the stage pages add the
stage-specific run commands.

Officially supported implicit-water models: **HCT, OBC1, OBC2, GBn, GBn2**
(GBn2 recommended).

Standard recipe: pass `--implicit-solvent <MODEL>` consistently on every node:
`build_amber_system --implicit-solvent <MODEL>` on the `topo` node, then the same
flag on `run_minimization`, `run_equilibration`, and `run_production`.
`build_amber_system` bakes the matching GB force (e.g. `implicit/gbn2.xml`) into
`system.xml` and stamps the canonical model name on `metadata.implicit_solvent`.

The run side validates the chain in three layers before building any System:

| Layer | Code | Trigger |
|---|---|---|
| Topology guard (resolver) | `implicit_solvent_topology_mismatch` | `metadata.implicit_solvent` and the runtime `--implicit-solvent` disagree after canonicalization |
| Runtime lookup | `implicit_solvent_model_unsupported` | Unknown / typo'd GB name (no silent OBC2 fallback) |
| Shim contract (deserialize) | `modern_system_implicit_solvent_unsupported` | `system.xml` carries no GB force at all |

`--implicit-solvent` is required; omitting it builds a vacuum system, not
implicit solvent.

## Research and external XML paths

- Research-mode shipped XML: `build_openmm_system --forcefield-xml … implicit/<model>.xml
  --implicit-solvent <MODEL>`. Same metadata contract, but the user owns the
  bundle: missing/duplicate `implicit/*.xml` returns `implicit_solvent_xml_missing`
  / `implicit_solvent_xml_ambiguous`.
- External GB XML (third-party, e.g. the Greener group's `GB99dms.xml`) is an
  advanced escape hatch through `build_openmm_system`. MDClaw cannot canonicalize
  a non-catalog GB XML, so `metadata.implicit_solvent` stays `None` and the
  run-side topology guard cannot validate the build/run match — the user manages
  XML correctness, GB-force presence, and build/run consistency.
