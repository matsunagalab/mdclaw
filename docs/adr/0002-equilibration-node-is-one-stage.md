# Equilibration node owns one restrained protocol

MDClaw treats each Equilibration Node as one restrained equilibration protocol
over one prepared topology. The default protocol may include an NVT heating
stage followed by an optional NPT density stage, controlled explicitly by
`nvt_time_ns` / `npt_time_ns` or low-level `nvt_steps` / `npt_steps`.

This keeps the user-facing skill contract simple: agents pass
`--nvt-time-ns` and, for explicit-solvent NPT equilibration, `--npt-time-ns`.
Implicit-solvent equilibration passes only `--nvt-time-ns` and
`--pressure-bar 0`.

When a workflow needs finer control, such as NPT compression followed by NVT
thermalization and a later NPT relaxation, represent that as multiple chained
`eq` nodes. Each node still owns its own protocol settings and restart
evidence.
