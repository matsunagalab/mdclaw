# Multi-Stage Equilibration Chaining

Advanced: read only when you need finer control than a single
`run_minimization` + `run_equilibration` pair. The default pair already runs
`min -> NVT -> optional NPT`; most workflows do not need this page.

For an explicit `NPT (compress) -> NVT (thermalize) -> NPT (relax)` protocol,
chain multiple `eq` nodes by parenting each onto the prior `eq` after the
initial `min -> eq`. The auto-resolver surfaces the parent's `state.xml` as
`restart_from`, so each new `eq` node skips minimization/warmup and inherits
positions, velocities, and box vectors. The loader is ensemble-agnostic (uses
`XmlSerializer.deserialize`), so an NPT-saved state can resume into an NVT stage
and vice versa: barostat parameters are dropped or introduced as needed.

```bash
# Stage 1: NPT compression with strong heavy-atom restraints
mdclaw create_node --job-dir <job_dir> --node-type eq \
  --parent-node-ids <min_node_id> --label "stage1_npt_compress" \
  --conditions '{"temperature_kelvin": 300, "pressure_bar": 1.0,
                 "nvt_time_ns": 0, "npt_time_ns": 0.2,
                 "restraint_atoms": "heavy", "restraint_force_constant": 500.0}'

# Stage 2: NVT thermalization with weaker CA restraints
mdclaw create_node --job-dir <job_dir> --node-type eq \
  --parent-node-ids <stage1_eq_node_id> --label "stage2_nvt_thermalize" \
  --conditions '{"temperature_kelvin": 300, "pressure_bar": 0,
                 "nvt_time_ns": 0.2, "npt_time_ns": 0,
                 "restraint_atoms": "CA", "restraint_force_constant": 50.0}'

# Stage 3: NPT density relaxation, no restraints
mdclaw create_node --job-dir <job_dir> --node-type eq \
  --parent-node-ids <stage2_eq_node_id> --label "stage3_npt_relax" \
  --conditions '{"temperature_kelvin": 300, "pressure_bar": 1.0,
                 "nvt_time_ns": 0, "npt_time_ns": 0.2,
                 "restraint_force_constant": 0.0}'
```

The first `eq` node auto-resumes from the `min` node's `state` artifact and
therefore skips coordinate minimization but still runs low-temperature warmup.
Each downstream `eq` node auto-resumes from its parent's `state` artifact; no
`--restart-from` flag is needed in node mode.
