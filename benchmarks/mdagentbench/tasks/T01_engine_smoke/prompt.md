# T01 Engine Smoke

You are evaluating an MD agent on `T01_engine_smoke`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: retrieve chignolin CLN025 from PDB entry 5AWL and run a tiny 10 ps Langevin NVT MD simulation in explicit TIP3P water. Use 0.15 M NaCl, an approximately 12 Å solvent buffer, 300 K, 2 fs steps, PME, HBonds constraints, and a DCD stride that gives at least 5 frames. The purpose is to show that your MD engine can prepare a solvated system, run without numerical failure, and write a trajectory that the scorer can reload.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

The manifest must point to the generated trajectory and topology under `outputs.trajectories` and `outputs.topology`. The metrics should report at least `execution.completed`, `execution.finite_energy`, and `execution.no_nan`. The evidence report should briefly summarize what was run, list public sources retrieved, and note limitations.

