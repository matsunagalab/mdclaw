# Solvent Regimes And Local-Run Policy

Single source of truth for solvent-regime intent, explicit-water defaults, and
local-execution policy shared by prepare, equilibration, and production. Stage
pages link here; Python signatures and guardrails remain authoritative.

## Regime -> Tool Calls

`solvent_regime` is study/job-level intent decided at bootstrap/planning:

| `solvent_regime` | prep call | next structural step | topology mode |
|---|---|---|---|
| `explicit` (default) | `prepare_complex --solvent-type explicit` | `solvate_structure` | `build_amber_system` with `box_dimensions` |
| `implicit` | `prepare_complex --solvent-type implicit` | skip solv | `build_amber_system --implicit-solvent <MODEL>` |
| `vacuum` | `prepare_complex --solvent-type vacuum` | skip solv | `build_amber_system` without box or GB |
| `membrane` | `prepare_complex --solvent-type explicit` | `embed_in_membrane` | `build_amber_system` with membrane box |

Default to `explicit` unless the user asks for implicit solvent, vacuum, or a
membrane. It controls prep-time component disposition, including retained ions; decide it before topology.

## Explicit-Water Constant Defaults

| Parameter | Default | User cues to override |
|---|---|---|
| Protein force field | `ff19SB` | "ff14SB" |
| Water model | `opc` | "tip3p", "spce", "tip4p-ew" |
| Buffer distance | `15 Å` | "buffer 20", "20A" |
| Box | cubic | "octahedral", "truncated octahedron" |
| Temperature | `300 K` | user value |
| Pressure | `1 bar` | user value |
| Ensemble | NPT for prod, NVT/NPT for eq | |
| Electrostatics | PME (cutoff 1.0 nm) | |
| Constraints | HBonds | |
| Integrator | `LangevinMiddleIntegrator` (friction 1/ps) | |
| HMR | enabled, `4 fs`, `hydrogenMass=4 amu` | `--no-hmr --timestep-fs 2.0` |

## Ion Intent -> Exact Flags

Ion placement belongs to `solvate_structure` / `embed_in_membrane`; do not search for a later add-ion tool.

| User wording | Exact flags |
|---|---|
| no ion/salt instruction, or `neutralised` alone | `--salt --saltcon 0.15` (default NaCl) |
| `physiological salt` | `--salt --saltcon 0.15` |
| `150 mM KCl` | `--salt --saltcon 0.15 --salt-c K+ --salt-a Cl-` |
| explicit `no salt` / `counterions only` / `neutralised only, no bulk salt` | `--salt --saltcon 0` |
| `no ions` | `--no-salt` |

Never turn `neutralised` alone into `--saltcon 0`: the 0.15 M NaCl default still
applies. With `--salt`, counterions come from prepared, protonated Amber residue
names plus `charge_pdb_delta` corrections for ligands, nucleic acids, lipids,
and metals. Charge 0 means zero *counterions*; only `--saltcon 0` means no bulk pairs.

`--no-salt` skips bulk salt and counterions; a charged topology is rejected as
`neutralization_charge_mismatch`. `--salt-override` allows neutralization to
exceed `--saltcon`; otherwise MDClaw tries the target, then retries once only for that condition.

Verify CLI JSON: `solute_net_charge_e` is the solute charge used for
counterions; `ion_counts` gives requested species and output-PDB counts. Node
mode copies them to `metadata.solute_net_charge_e` and `metadata.ion_counts` in `nodes/<solv_id>/node.json`.
`auto_charge_pdb_delta` is only a correction to the residue estimate, never the net charge.
`--notprotonate` preserves prepared protonation; it does not mean the input is unprotonated.

**Standard pair is `ff19SB + opc`** (Amber Manual 2024); ff19SB was parameterized
against OPC. `ff19SB + tip3p` is blocked as `forcefield_water_blocked`; use
`ff14SB + tip3p` only for pre-2019 reproduction, overriding both sides together.

HMR is baked into `system.xml`; a run-side mismatch raises
`modern_system_hmr_mismatch`. Keep standard crystallographic ions on the explicit
path. OPC covers NA, CL, K, MG, CA, MN, ZN, FE/FE2, CU, CO, NI, CD, and HG.
Topology rejects ions absent from the selected water XML as
`unsupported_ion_for_water_model` (OPC uses `I`, TIP3P-like XMLs `IOD`); switch
models or use the equivalent template name. Custom metals need a converted XML via
`build_openmm_system(forcefield_xml=...)`; do not parameterize standard bare ions.

## Local-Execution / Platform Policy

Before local topology/min/eq/prod on explicit water, run:

```bash
mdclaw inspect_openmm_platforms \
  --atom-count <solv.statistics.total_atoms> \
  --solvent-type explicit
```

- For `not_recommended` or `slow_on_cpu`, report CUDA/OpenCL availability and
  prefer `/hpc-run`, or explicitly choose a short 0.01 ns eq / 0.1 ns prod smoke test.
- State any debugging-only box reduction; never apply it silently.
- Use `--platform auto` (CUDA, else OpenCL); pass `CPU` only when requested.

## Implicit / Vacuum Topology Contract

Implicit/vacuum runs skip `solvate_structure` and build from prep's `merged_pdb`:

- Implicit: use `--implicit-solvent <MODEL>` (`HCT`/`OBC1`/`OBC2`/`GBn`/`GBn2`)
  and prep with `--solvent-type implicit`; otherwise `explicit_ions_in_implicit_solvent` blocks.
- Vacuum: no box or GB model; keeping explicit ions is a deliberate choice.

The run-side contract is `system.system.xml` + `system.topology.pdb` +
`system.state.xml`; tleap / `parm7` / `rst7` are never produced or consumed.
