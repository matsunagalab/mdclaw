# Production MD: Implicit Solvent

Read `skills/common/implicit-solvent-contract.md` first for the supported models
(HCT, OBC1, OBC2, GBn, GBn2; GBn2 recommended) and the build/run validation
contract. `prod` inherits the GB model and HMR setting from the topology.

Implicit solvent has no periodic box: no barostat, NVT only, and
NoCutoff / CutoffNonPeriodic electrostatics — always pass `--pressure-bar 0`.

---

## Production Run

### Local Execution (node-based)

```bash
mdclaw --job-dir <job_dir> --node-id <prod_node_id> run_production \
  --simulation-time-ns <ns> \
  --temperature-kelvin <T> \
  --pressure-bar 0 \
  --output-frequency-ps 10.0
```

Choose the run length with the Default Decision Rule in `SKILL.md`.
`system_xml_file`, `topology_pdb_file`, `state_xml_file`, and `restart_from`
auto-resolve from DAG ancestors. For extension (`--continue-from`) and retry
details, read `skills/md-production/restart.md`; keep `--pressure-bar 0` on
extension nodes. GPU selection and HMR flags are the same as
`skills/md-production/explicit-water.md` "GPU / HMR". To bias production with a
custom force / CV, read and follow `skills/md-production/custom-force.md`.

### GBn2 Ligand Fallback

GBn/GBn2 neck-correction radii tables may not cover every GAFF or curated
ligand atom type; ligand systems (especially highly charged ligands such as
AP5/ATP/ADP) can fail with `Radii must be between 1 and 2 Angstroms for neck
lookup`. Do not retry the same command. Branch a new `min` node from the same
`topo` parent, then a new `eq` node from that `min`, and run min, eq, and prod
with `--implicit-solvent OBC2` (keep `--pressure-bar 0`; label the branch,
e.g. `min_OBC2`).

### SLURM Execution (HPC)

For long runs, multi-replicate sweeps, or fan-out across many systems, hand off
to HPC execution instead of duplicating sbatch patterns here: follow
`skills/hpc-run/SKILL.md` and its submit/monitor/extension pages, and keep
`--pressure-bar 0` in the job-script command.

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
