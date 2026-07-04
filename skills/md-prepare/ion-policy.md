# Ion Policy By Solvent Regime

Single source of truth for how crystallographic / explicit ions are handled at
prep time. Always pass the study's `solvent_regime` to `prepare_complex`
(`--solvent-type explicit|implicit|vacuum`); the regime decides ion disposition,
so it must not be deferred to topology generation.

| Regime | Ion handling at prep | Enforced by |
|---|---|---|
| `explicit` (and `membrane`) | Keep standard bare crystallographic ions covered by the active water XML in `merged_pdb` by default. Default OPC covers common ions such as NA, CL, K, MG, CA, MN, ZN, FE/FE2, CU, CO, NI, CD, and HG. Non-OPC models can differ; topology checks the exact active XML template set. | default + topology guard |
| `implicit` | Prep excludes explicit ion components from `merged_pdb` and records them in `component_disposition.json`. | `prepare_complex --solvent-type implicit` |
| `vacuum` | A deliberate no-solvent topology may keep explicit ions, but this is not the default MD workflow. | manual choice |

Rules:

- Do not create extra parameter artifacts for standard bare monatomic ions just
  because they are metals. Keep them as ions on the explicit path and let
  `build_amber_system` match the water-model XML template.
- Ion support is water-model-specific. `build_amber_system` rejects retained
  bare ions whose residue names are absent from the active water XML with
  `code="unsupported_ion_for_water_model"`. For example, OPC supports `I`,
  while TIP3P-like water XMLs use `IOD` instead.
- If a metal site is not a standard bare ion, or the scientific model needs
  bonded/coordination-specific metal-site parameters, the required artifact is a
  pre-converted OpenMM ForceField XML used through
  `build_openmm_system(forcefield_xml=...)`.
- `build_amber_system` validates the same invariant and rejects implicit builds
  that still contain explicit ions with `code="explicit_ions_in_implicit_solvent"`.
  Recover by rebuilding the `prep` branch with `--solvent-type implicit`, or
  switch to the explicit path with `solvate_structure`, or make a deliberate
  vacuum choice if that is the scientific request.
- Copy the tool-written `component_disposition.json`; do not hand-write it.
