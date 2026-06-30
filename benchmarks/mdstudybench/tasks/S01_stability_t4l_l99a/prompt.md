# S01_stability_t4l_l99a: T4 Lysozyme L99A Stability

You are evaluating an MD agent on `S01_stability_t4l_l99a`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: retrieve wild-type T4 lysozyme from PDB entry 2LZM, create the L99A mutant on chain A (LEU 99 to ALA), and report a literature-calibrated stability direction for L99A relative to wild type using short comparative MD as local consistency evidence.

Public source anchors: PDB 2LZM. Run short comparative MD for the WT and L99A systems, compute quantitative differences such as core SASA, cavity volume or packing proxy, C-alpha RMSF near residue 99, hydrophobic contact count, and any other relevant observables, then use those numbers to explain whether the trajectories are consistent with the calibrated interpretation.

Do not claim that a short trajectory by itself proves a folding-stability delta-delta-G or experimentally resolves thermodynamic stability. Separate (1) MD-supported local observations from (2) the literature-calibrated stability direction. If the short MD metrics are noisy or inconclusive, say so explicitly while still reporting the calibrated direction and limitations.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

For a completed submission, the manifest must point `outputs.trajectories` to the wild-type and mutant production trajectories (WT first, mutant second) and `outputs.topology` to the matching wild-type and mutant topologies (same order), so the scorer can reload each trajectory against its topology and verify that the two systems differ by exactly the LEU99->ALA substitution. Populate `metrics.md_analysis` and mirror the important quantitative values in `evidence_report.evidence.md_metrics`. Set `evidence_report.effect.direction` to one of `destabilizing`, `stabilizing`, or `neutral`. Public literature may be cited for calibration, but the submitted MD numbers must be used as consistency evidence rather than overclaimed as a standalone thermodynamic proof. Include calibrated confidence, public sources retrieved, and explicit limitations.
