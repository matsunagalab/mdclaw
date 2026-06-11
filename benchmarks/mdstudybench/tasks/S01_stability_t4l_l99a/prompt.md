# S01 T4 Lysozyme L99A Stability

You are evaluating an MD agent on `S01_stability_t4l_l99a`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/`.

Task: retrieve wild-type T4 lysozyme from PDB entry 2LZM, create the L99A mutant on chain A (LEU 99 to ALA), and predict whether L99A is stabilizing, destabilizing, or neutral relative to wild type using MD-derived evidence. Run short comparative MD for the WT and L99A systems, compute quantitative differences such as core SASA, cavity volume or packing proxy, C-alpha RMSF near residue 99, hydrophobic contact count, and any other relevant observables, then use those numbers to support the answer.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

For a completed submission, the manifest must point to real WT and mutant trajectory artifacts under `outputs.trajectories`. Populate `metrics.md_analysis` and mirror the important quantitative values in `evidence_report.evidence.md_metrics`. Set `evidence_report.effect.direction` to one of `destabilizing`, `stabilizing`, or `neutral`. Public literature may be cited for confidence calibration, but the direction must be defended by submitted MD numbers and artifacts. Include calibrated confidence, public sources retrieved, and explicit limitations.
