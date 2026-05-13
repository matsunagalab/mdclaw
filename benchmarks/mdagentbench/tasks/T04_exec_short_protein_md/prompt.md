# T04 Short Protein MD

You are evaluating an MD agent on `T04_exec_short_protein_md`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: retrieve wild-type T4 lysozyme from PDB entry 2LZM, prepare and equilibrate it, then run short explicit-water MD. Use ff14SB/TIP3P, 0.15 M NaCl, an approximately 12 Å buffer, positional restraints during equilibration, 300 K NVT production, 2 fs steps, PME, HBonds constraints, and at least 100 ps production with at least 50 trajectory frames.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

The manifest must point to the generated trajectory and topology under `outputs.trajectories` and `outputs.topology`. The topology should contain explicit water. Metrics should report finite energy, no NaN behavior, and simulated time. The evidence report should summarize preparation, equilibration, production, public sources retrieved, and limitations.

