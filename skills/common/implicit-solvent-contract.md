# Implicit Solvent Contract

Shared by equilibration and production implicit-solvent pages. It defines the
supported models and the build/run validation contract; the stage pages add the
stage-specific run commands.

Officially supported implicit-water models: **HCT, OBC1, OBC2, GBn, GBn2**
(GBn2 recommended).

Set `--implicit-solvent <MODEL>` when building the `topo` node.
`build_amber_system` bakes the matching GB force into `system.xml` and records
the model on the topology. `min`, `eq`, and `prod` inherit it when the flag is
omitted; an explicit runtime value must match the topology.

The run side validates the chain in three layers before building any System:

| Layer | Code | Trigger |
|---|---|---|
| Topology guard (resolver) | `implicit_solvent_topology_mismatch` | An explicit runtime model disagrees with `metadata.implicit_solvent` |
| Runtime lookup | `implicit_solvent_model_unsupported` | Unknown / typo'd GB name (no silent OBC2 fallback) |
| Shim contract (deserialize) | `modern_system_implicit_solvent_unsupported` | `system.xml` carries no GB force at all |

The topology build still requires `--implicit-solvent`; omitting it there builds
a vacuum system.

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
