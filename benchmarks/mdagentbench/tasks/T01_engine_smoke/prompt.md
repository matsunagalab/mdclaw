# T01 Engine Smoke

You are evaluating an MD agent on `T01_engine_smoke`.

Use only these public files:

- `task.json`
- `input/5AWL.pdb`
- `input/solvent_spec.json`
- `input/md_protocol.json`

Do not read `truth/` or `scorer/` if those directories exist.

Task: run a tiny 10 ps Langevin NVT MD simulation of chignolin (CLN025, PDB 5AWL) in explicit TIP3P water. The purpose is to show that your MD engine can prepare a solvated system, run without numerical failure, and write a trajectory that the scorer can reload.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

The manifest must point to the generated trajectory and topology under `outputs.trajectories` and `outputs.topology`. The metrics should report at least `execution.completed`, `execution.finite_energy`, and `execution.no_nan`. The evidence report should briefly summarize what was run and note any limitations.

