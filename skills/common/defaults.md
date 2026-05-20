# MDClaw Defaults

Modern explicit-water default:

- Solvation mode: explicit solvent unless the user explicitly asks for
  implicit/vacuum/no-solvent or for a membrane workflow.
- Protein forcefield: `ff19SB`
- Water model: `opc`
- Buffer: `15 Å`
- Salt: `0.15 M NaCl`
- Temperature: `300 K`
- Pressure: `1 bar`
- Production integrator: `LangevinMiddleIntegrator`
- HMR: enabled by default for production, `4 fs` timestep,
  `hydrogenMass=4 amu`
- Constraints: `HBonds`
- Explicit electrostatics: PME

Do not substitute legacy tutorial defaults such as `ff14SB + tip3p` unless the
user explicitly requests them and guardrails allow the combination.

Guardrail examples:

- `forcefield_water_blocked`: incompatible explicit-solvent forcefield/water
  pairing.
- `explicit_ions_in_implicit_solvent`: the prepared structure still has
  explicit ion residues but the topology request is implicit solvent. Prefer
  `prepare_complex --solvent-type implicit` so prep records and excludes them,
  or use explicit solvent / a deliberate vacuum-no-solvent choice instead.
- `openmm_fallback_unsupported_water_model`: OpenMM fallback cannot produce the
  requested water model safely.
- `metal_unsupported_water_model`: ion parameter set does not support the water
  model.
