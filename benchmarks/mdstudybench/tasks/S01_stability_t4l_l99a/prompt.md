# S01_stability_t4l_l99a: T4 Lysozyme L99A Stability

You are evaluating an MD agent on `S01_stability_t4l_l99a`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: retrieve wild-type T4 lysozyme from PDB entry 2LZM, create the L99A mutant on chain A (LEU 99 to ALA), and report a literature-calibrated stability direction for L99A relative to wild type using comparative MD as local consistency evidence.

Public source anchors: PDB 2LZM. Run comparative MD for the WT and L99A systems, compute quantitative differences such as core SASA, cavity volume or packing proxy, C-alpha RMSF near residue 99, hydrophobic contact count, and any other relevant observables, then use those numbers to explain whether the trajectories are consistent with the calibrated interpretation.

Do not claim that a short trajectory by itself proves a folding-stability delta-delta-G or experimentally resolves thermodynamic stability. Separate (1) MD-supported local observations from (2) the literature-calibrated stability direction. If the MD metrics are noisy or inconclusive, say so explicitly while still reporting the calibrated direction and limitations.

Time budget: you have up to 24 hours of wall-clock for the entire task — preparing both systems, running the comparative MD, analysing, and writing the submission. No target simulation length is prescribed: decide the production length and any replicate count yourself so the whole workflow finishes within 24 hours on your hardware. This MD planning (converting the wall-clock budget into a sampling plan) is part of the task. Aim for enough sampling — longer and/or replicated runs — to make the WT-versus-mutant comparison meaningful, but remember that no attainable trajectory here proves a converged folding-stability delta-delta-G; separate MD-supported observations from the literature-calibrated interpretation and state your limitations.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

For a completed submission, the manifest must point `outputs.trajectories` to the wild-type and mutant production trajectories (WT first, mutant second) and `outputs.topology` to the matching wild-type and mutant topologies (same order), so the scorer can reload each trajectory against its topology and verify that the two systems differ by exactly the LEU99->ALA substitution. Populate `metrics.md_analysis` and mirror the important quantitative values in `evidence_report.evidence.md_metrics`. In `evidence_report.observables`, report each discriminating observable you relied on with its wild-type and mutant mean values, an uncertainty estimate, and the direction it supports, and add a `reasoning` field explaining how those numbers lead to your `effect.direction`; the scorer recomputes your primary discriminating observable from the submitted trajectories and checks both that your reported values match and that their sign is consistent with the direction you claim. Set `evidence_report.effect.direction` to one of `destabilizing`, `stabilizing`, or `neutral`. Public literature may be cited for calibration, but the submitted MD numbers must be used as consistency evidence rather than overclaimed as a standalone thermodynamic proof. Include calibrated confidence, public sources retrieved, and explicit limitations.
