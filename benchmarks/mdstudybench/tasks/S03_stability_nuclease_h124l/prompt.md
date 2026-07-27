# S03_stability_nuclease_h124l: Staphylococcal Nuclease H124L Stability

You are evaluating an MD agent on `S03_stability_nuclease_h124l`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: retrieve wild-type staphylococcal nuclease from PDB entry 1STN (His124), create the H124L variant (HIS 124 to LEU), and report a literature-calibrated stability direction for H124L relative to wild type using comparative MD as local consistency evidence. Run comparative MD for the WT and H124L systems, compute quantitative differences such as local RMSF and SASA around residue 124, secondary-structure persistence, and packing, then use those numbers to explain whether the trajectories are consistent with the calibrated interpretation.

Public source anchors: PDB 1STN.

Note: not every mutation is destabilizing. Report the calibrated direction supported by the literature, not a default assumption.

Do not claim that a short trajectory by itself proves a folding-stability delta-delta-G. Separate (1) MD-supported local observations from (2) the literature-calibrated stability direction. If the MD metrics are noisy or inconclusive, say so explicitly while still reporting the calibrated direction and limitations.

Time budget: you have up to 24 hours of wall-clock for the entire task — preparing both systems, running the comparative MD, analysing, and writing the submission. No target simulation length is prescribed: decide the production length and any replicate count yourself so the whole workflow finishes within 24 hours on your hardware. This MD planning (converting the wall-clock budget into a sampling plan) is part of the task. Aim for enough sampling — longer and/or replicated runs — to make the WT-versus-variant comparison meaningful, but remember that no attainable trajectory here proves a converged folding-stability delta-delta-G; separate MD-supported observations from the literature-calibrated interpretation and state your limitations.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

For a completed submission, the manifest must point `outputs.trajectories` to the wild-type and variant production trajectories (WT first, variant second) and `outputs.topology` to the matching wild-type and variant topologies (same order), so the scorer can reload each trajectory against its topology and verify that the two systems differ by exactly the HIS124->LEU substitution. Populate `metrics.md_analysis` and mirror the important quantitative values in `evidence_report.evidence.md_metrics`. In `evidence_report.observables`, report each discriminating observable you relied on with its wild-type and variant mean values, an uncertainty estimate, and the direction it supports, and add a `reasoning` field explaining how those numbers lead to your `effect.direction`; the scorer recomputes your primary discriminating observable from the submitted trajectories and checks both that your reported values match and that their sign is consistent with the direction you claim. Set `evidence_report.effect.direction` to one of `destabilizing`, `stabilizing`, or `neutral`. Public literature may be cited for calibration, but the submitted MD numbers must be used as consistency evidence rather than overclaimed as a standalone thermodynamic proof. Include calibrated confidence, public sources retrieved, and explicit limitations.
