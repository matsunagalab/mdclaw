# Solvent Regimes And Local-Run Policy

Single source of truth for solvent-regime intent, the explicit-water constant
defaults, and the local-execution/platform policy shared by prepare,
equilibration, and production. Stage pages reference this page instead of
repeating these blocks. Python tool signatures and guardrails remain
authoritative; this is a quick reference.

## Regime -> Tool Calls

`solvent_regime` is study/job-level intent decided at bootstrap/planning. Map it
to tool calls:

| `solvent_regime` | prep call | next structural step | topology mode |
|---|---|---|---|
| `explicit` (default) | `prepare_complex --solvent-type explicit` | `solvate_structure` | `build_amber_system` with `box_dimensions` |
| `implicit` | `prepare_complex --solvent-type implicit` | skip solv | `build_amber_system --implicit-solvent <MODEL>` |
| `vacuum` | `prepare_complex --solvent-type vacuum` | skip solv | `build_amber_system` without box or GB |
| `membrane` | `prepare_complex --solvent-type explicit` | `embed_in_membrane` | `build_amber_system` with membrane box |

Default to `explicit` unless the user explicitly asks for implicit solvent,
vacuum/no-solvent, or a membrane workflow. The regime affects prep-time
component disposition (e.g. whether explicit ion components are retained), so do
not defer it to topology generation.

## Explicit-Water Constant Defaults

| Parameter | Default | User cues to override |
|---|---|---|
| Protein force field | `ff19SB` | "ff14SB" |
| Water model | `opc` | "tip3p", "spce", "tip4p-ew" |
| Buffer distance | `15 Å` | "buffer 20", "20A" |
| Salt | `0.15 M NaCl` | "0.3M", "no salt" |
| Box | cubic | "octahedral", "truncated octahedron" |
| Temperature | `300 K` | user value |
| Pressure | `1 bar` | user value |
| Ensemble | NPT for prod, NVT/NPT for eq | |
| Electrostatics | PME (cutoff 1.0 nm) | |
| Constraints | HBonds | |
| Integrator | `LangevinMiddleIntegrator` (friction 1/ps) | |
| HMR | enabled, `4 fs`, `hydrogenMass=4 amu` | `--no-hmr --timestep-fs 2.0` |

**Standard pair is `ff19SB + opc`** (Amber Manual 2024). ff19SB was
parameterized against OPC and behaves incorrectly with TIP3P; the guardrail
rejects `ff19SB + tip3p` with `code=forcefield_water_blocked`. Use
`ff14SB + tip3p` only to reproduce pre-2019 results, overriding both sides
together. Do not substitute legacy tutorial defaults from training-data memory.

HMR is a build-time choice baked into `system.xml`; a run-side mismatch raises
`modern_system_hmr_mismatch`. Keep standard bare crystallographic ions on the
explicit path by default. Default OPC covers common ions such as NA, CL, K, MG,
CA, MN, ZN, FE/FE2, CU, CO, NI, CD, and HG through its water XML. Other water
models can differ; topology rejects retained bare ions absent from the active
water XML with `unsupported_ion_for_water_model`. Custom or
coordination-specific metal chemistry requires a pre-converted OpenMM
ForceField XML through `build_openmm_system(forcefield_xml=...)`.

## Local-Execution / Platform Policy

Before any local topology/min/eq/prod on an explicit-water system, run the
feasibility preflight:

```bash
mdclaw inspect_openmm_platforms \
  --atom-count <solv.statistics.total_atoms> \
  --solvent-type explicit
```

- If `local_feasibility` is `not_recommended` or `slow_on_cpu`, do not silently
  continue on local CPU. Tell the user whether a CUDA/OpenCL platform was
  detected and prefer `/hpc-run`, or make an explicit short smoke-test choice
  (e.g. `--nvt-time-ns 0.01 --npt-time-ns 0.01`, or `--simulation-time-ns 0.1`).
- Reducing the water box changes the system and must be stated as a debugging
  choice, not applied silently.
- Do not pass `--platform CPU` unless the user explicitly asks for CPU-only
  debugging. Prefer the tool default `--platform auto`; when an explicit platform
  is needed, use `CUDA` if available, else `OpenCL`.

## Implicit / Vacuum Topology Contract

Implicit and vacuum runs skip `solvate_structure`. `build_amber_system` builds
directly from the completed `prep` parent's `merged_pdb`:

- Implicit: pass `--implicit-solvent <MODEL>` (`HCT`/`OBC1`/`OBC2`/`GBn`/`GBn2`).
  Explicit ions must not be present (`explicit_ions_in_implicit_solvent`); run
  `prepare_complex --solvent-type implicit` so prep records and excludes them.
- Vacuum: no box and no GB model. A deliberate vacuum topology may keep explicit
  ions but is not the default MD workflow.

The run-side topology contract is always the OpenMM XML triple
(`system.system.xml` + `system.topology.pdb` + `system.state.xml`); tleap /
`parm7` / `rst7` are never produced or consumed.
