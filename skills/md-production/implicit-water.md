# Production MD: Implicit Solvent

Officially supported implicit-water models: **HCT, OBC1, OBC2, GBn, GBn2**.

Standard recipe: `build_amber_system --implicit-solvent <MODEL>` on the
topo node, then `run_production --implicit-solvent <MODEL>` on the prod
node. `build_amber_system` bakes the matching GB force
(`implicit/gbn2.xml` etc.) into `system.xml`, stamps the canonical
model name on `metadata.implicit_solvent`, and the run side validates
the chain in three layers: a topology guard
(`implicit_solvent_topology_mismatch` if the topo metadata and runtime
flag disagree after canonicalization), a runtime lookup
(`implicit_solvent_model_unsupported` for unknown names — no silent
OBC2 fallback), and the shim's GB-force presence check
(`modern_system_implicit_solvent_unsupported` if the saved System has
no GB force).

Research-mode shipped XML path:
`build_openmm_system --forcefield-xml … implicit/<model>.xml --implicit-solvent <MODEL>`.
Same metadata contract, but the user owns the bundle. Missing XML
returns `implicit_solvent_xml_missing`; bundling two shipped GB XMLs
without an explicit `--implicit-solvent` returns
`implicit_solvent_xml_ambiguous`.

External GB XML (third-party, e.g. the Greener group's `GB99dms.xml`)
is an advanced escape hatch through `build_openmm_system`. mdclaw
cannot canonicalize a non-catalog GB XML, so the topo node's
`metadata.implicit_solvent` stays `None` and the run-side topology
guard cannot validate the build/runtime match — the user must manage
XML correctness, GB-force presence, and consistency between build and
run themselves.

## System Configuration

| Parameter | Value | Notes |
|---|---|---|
| Electrostatics | **NoCutoff** or **CutoffNonPeriodic** | NoCutoff for small systems; CutoffNonPeriodic (cutoff ~2 nm) for large systems |
| Force field | ff14SB | ff19SB was optimized for explicit OPC water |
| GB model | GBn2 (igb=8, default) | `implicit/gbn2.xml` in OpenMM |
| Integrator | LangevinMiddleIntegrator | Friction 1/ps |
| Thermostat | Langevin (built into integrator) | |
| Barostat | **None** | No periodic box → no pressure coupling |
| Constraints | HBonds | Allows up to 4 fs with LangevinMiddle |
| Ensemble | NVT | No NPT for implicit solvent |

### Implicit Solvent Models (fastest → most accurate)

| Model | OpenMM XML | igb | Notes |
|---|---|---|---|
| HCT | `implicit/hct.xml` | 1 | Fastest, least accurate |
| OBC1 | `implicit/obc1.xml` | 2 | Good balance |
| OBC2 | `implicit/obc2.xml` | 5 | Better than OBC1 |
| GBn | `implicit/gbn.xml` | 7 | Improved neck correction |
| GBn2 | `implicit/gbn2.xml` | 8 | **Recommended** |

### Timestep Guide

The MDClaw default is HBonds + HMR=True at 4 fs. HMR is a build-time
choice — it must match what `build_amber_system` / `build_openmm_system`
baked into `system.xml`, otherwise the run-side XML system validator
raises `modern_system_hmr_mismatch`.

| Constraints | HMR    | Max Timestep | Recommended                      |
|-------------|--------|--------------|----------------------------------|
| HBonds      | True   | 4 fs         | **4 fs** (MDClaw default)        |
| HBonds      | False  | 2 fs         | 2 fs (no HMR baked into XML)     |
| AllBonds    | True   | 4 fs         | 4 fs                             |
| None        | False  | 1 fs         | Not recommended                  |

---

## Production Run

### Local Execution (node-based)

```bash
mdclaw --job-dir <job_dir> --node-id <prod_node_id> run_production \
  --simulation-time-ns <user_specified> \
  --temperature-kelvin <T> \
  --pressure-bar 0 \
  --implicit-solvent GBn2 \
  --output-frequency-ps 10.0
```

If the user does not specify a run length and `execution_mode=autonomous`,
use `--simulation-time-ns 0.1` as the default sanity check.

> `--pressure-bar 0` disables the barostat (no periodic box in implicit solvent).

`system_xml_file`, `topology_pdb_file`, `state_xml_file`, and `restart_from` auto-resolve from DAG
ancestors. For extension/retry details, read
`skills/md-production/restart.md`.

### GBn2 Ligand Fallback

GBn2/GBn use neck-correction radii tables that may not cover every GAFF or
curated ligand atom type. For ligand-containing systems, especially highly
charged ligands such as AP5/ATP/ADP, OpenMM may fail with:

```text
Radii must be between 1 and 2 Angstroms for neck lookup
```

Do not retry the same prod command with identical parameters. Create a new
`min` node from the same `topo` parent, then a new `eq` node from that `min`
node, and run both equilibration and production with `OBC2`:

```bash
mdclaw create_node --job-dir <job_dir> --node-type min \
  --parent-node-ids <topo_node_id> \
  --label "min_OBC2" \
  --conditions '{"max_iterations": 5000, "implicit_solvent": "OBC2"}'

mdclaw --job-dir <job_dir> --node-id <obc2_min_node_id> run_minimization \
  --implicit-solvent OBC2

mdclaw create_node --job-dir <job_dir> --node-type eq \
  --parent-node-ids <obc2_min_node_id> \
  --label "300K_OBC2" \
  --conditions '{"temperature_kelvin": 300, "pressure_bar": 0, "implicit_solvent": "OBC2"}'

mdclaw --job-dir <job_dir> --node-id <obc2_eq_node_id> run_equilibration \
  --temperature-kelvin 300 \
  --pressure-bar 0 \
  --implicit-solvent OBC2

mdclaw create_node --job-dir <job_dir> --node-type prod \
  --parent-node-ids <obc2_eq_node_id> \
  --label "1ns_OBC2" \
  --conditions '{"simulation_time_ns": 1.0, "pressure_bar": 0, "implicit_solvent": "OBC2"}'

mdclaw --job-dir <job_dir> --node-id <obc2_prod_node_id> run_production \
  --simulation-time-ns 1.0 \
  --temperature-kelvin 300 \
  --pressure-bar 0 \
  --implicit-solvent OBC2
```

### SLURM Execution (HPC)

```bash
mdclaw submit_job \
  --script "mdclaw --job-dir <job_dir> --node-id <prod_node_id> run_production \
    --simulation-time-ns <user_specified> \
    --temperature-kelvin <T> \
    --pressure-bar 0 \
    --implicit-solvent GBn2 \
    --platform CUDA" \
  --job-name md_<name> \
  --partition <partition> --gpus 1 \
  --time-limit <estimated> --memory "32G"
```

`--job-dir` is auto-resolved to an absolute path by the CLI, so SLURM
compute nodes can find all files without manual `realpath` conversion.

---

## Common Run Lengths

| Purpose | Time | Notes |
|---|---|---|
| Sanity check | 0.1 ns | Quick validation; default when autonomous and omitted |
| Conformational sampling | 10-100 ns | Faster than explicit, good for screening |
| Folding study | 100 ns - 1 us | GB allows longer effective sampling |
| Mutant screening | 10 ns x N | Quick comparative runs |

---

## HPC / GPU Usage

### GPU Selection

```bash
mdclaw --job-dir <job_dir> --node-id <prod_node_id> run_production \
  --platform CUDA --device-index "0" \
  --simulation-time-ns 100.0 --pressure-bar 0 --implicit-solvent GBn2
```

### HMR (default: enabled)

HMR and 4 fs timestep are defaults. To disable:

```bash
mdclaw --job-dir <job_dir> --node-id <prod_node_id> run_production \
  --no-hmr --timestep-fs 2.0 \
  --simulation-time-ns 100.0 --pressure-bar 0 --implicit-solvent GBn2
```

---

## Restart / Extension

For planned extensions, create a new prod node with `--continue-from`:

```bash
mdclaw create_node --job-dir <job_dir> --node-type prod \
  --continue-from <completed_prod_node_id> --label "+50ns" \
  --conditions '{"simulation_time_ns": 50}'

mdclaw --job-dir <job_dir> --node-id <extension_prod_node_id> run_production \
  --simulation-time-ns 50.0 --platform CUDA \
  --pressure-bar 0 --implicit-solvent GBn2
```

For state-vs-checkpoint behavior, same-node retries, and stale-artifact
handling, read `skills/md-production/restart.md`.

---

## When to Use Implicit Solvent

**Good for:**
- Rapid conformational sampling (folding studies)
- Large systems where explicit water is too expensive
- Screening many mutants or ligands quickly
- Systems where water-mediated interactions are not critical

**Limitations:**
- No explicit water-mediated interactions
- Salt bridges may be overstabilized
- Less accurate for surface-exposed residues
- Membrane systems not supported
- Solvation free energies less accurate than explicit water

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| SHAKE constraint failure | Bad geometry | Reduce to 2 fs, or re-prepare structure |
| Unrealistic compaction | GB artifacts | Consider explicit water for this system |
| Salt bridges too stable | GB dielectric overestimation | Validate with explicit water run |
| Slow performance | GPU not detected | Check `--platform CUDA` and `nvidia-smi` |

---

## Verify Output

Read `nodes/<prod_node_id>/node.json`:

- `status`: `"completed"`
- `artifacts`: `trajectory`, `final_structure`, `checkpoint`, `energy`
- `metadata`: `simulation_time_ns`, `temperature_kelvin`, `platform`,
  `hmr`, `timestep_fs`, `num_steps`, `start_step`, `start_time_ns`
  (non-zero only for extension runs), `continued_from` (set when the
  node was created via `--continue-from`)
