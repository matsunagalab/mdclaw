# S04_stability_nuclease_h124l: Staphylococcal Nuclease H124L Stability

You are evaluating an MD agent on `S04_stability_nuclease_h124l`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: retrieve wild-type staphylococcal nuclease from PDB entry 1STN (His124), create the H124L variant (HIS 124 to LEU), and report a literature-calibrated stability direction for H124L relative to wild type using short comparative MD as local consistency evidence. Run short comparative MD for the WT and H124L systems, compute quantitative differences such as local RMSF and SASA around residue 124, secondary-structure persistence, and packing, then use those numbers to explain whether the trajectories are consistent with the calibrated interpretation.

Public source anchors: PDB 1STN.

Note: not every mutation is destabilizing. Report the calibrated direction supported by the literature, not a default assumption.

Do not claim that a short trajectory by itself proves a folding-stability delta-delta-G. Separate (1) MD-supported local observations from (2) the literature-calibrated stability direction. If the short MD metrics are noisy or inconclusive, say so explicitly while still reporting the calibrated direction and limitations.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

For a completed submission, the manifest must point `outputs.trajectories` to the wild-type and variant production trajectories (WT first, variant second) and `outputs.topology` to the matching wild-type and variant topologies (same order), so the scorer can reload each trajectory against its topology and verify that the two systems differ by exactly the HIS124->LEU substitution. Populate `metrics.md_analysis` and mirror the important quantitative values in `evidence_report.evidence.md_metrics`. Set `evidence_report.effect.direction` to one of `destabilizing`, `stabilizing`, or `neutral`. Public literature may be cited for calibration, but the submitted MD numbers must be used as consistency evidence rather than overclaimed as a standalone thermodynamic proof. Include calibrated confidence, public sources retrieved, and explicit limitations.
