# Ion Policy By Solvent Regime

Single source of truth for how crystallographic / explicit ions are handled at
prep time. Always pass the study's `solvent_regime` to `prepare_complex`
(`--solvent-type explicit|implicit|vacuum`); the regime decides ion disposition,
so it must not be deferred to topology generation.

| Regime | Ion handling at prep | Enforced by |
|---|---|---|
| `explicit` (and `membrane`) | Keep supported crystallographic ions (CA, MG, NA, K, CL) in `merged_pdb` by default. | default |
| `implicit` | Prep excludes explicit ion components from `merged_pdb` and records them in `component_disposition.json`. | `prepare_complex --solvent-type implicit` |
| `vacuum` | A deliberate no-solvent topology may keep explicit ions, but this is not the default MD workflow. | manual choice |

Rules:

- Do NOT call `parameterize_metal_ion` for standard monatomic ions (CA, MG, NA,
  K, CL) just because they are metals. Keep them as ions on the explicit path
  and let `build_amber_system` handle them. Use explicit metal parameterization
  only when a structured tool result reports missing or coordination-specific
  metal parameters.
- `build_amber_system` validates the same invariant and rejects implicit builds
  that still contain explicit ions with `code="explicit_ions_in_implicit_solvent"`.
  Recover by rebuilding the `prep` branch with `--solvent-type implicit`, or
  switch to the explicit path with `solvate_structure`, or make a deliberate
  vacuum choice if that is the scientific request.
- Copy the tool-written `component_disposition.json`; do not hand-write it.
