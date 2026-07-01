# S04_affinity_t4l_l99a_alkylbenzene: T4 Lysozyme L99A Apolar Ligand Affinity

You are evaluating an MD agent on `S04_affinity_t4l_l99a_alkylbenzene`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: using the engineered T4 lysozyme L99A apolar cavity, compare the binding of benzene (PDB 4W52, also 181L) with n-butylbenzene (PDB 4W57) and report which ligand binds more tightly. Run short comparative MD of the two L99A-ligand complexes and compute interaction evidence such as ligand-cavity contact counts, buried apolar surface, and ligand occupancy or retention in the cavity, then use those numbers to explain the relative affinity. Report `effect.direction` for n-butylbenzene RELATIVE TO benzene.

Public source anchors: PDB 4W52 (benzene), PDB 4W57 (n-butylbenzene).

Do not claim that a short trajectory by itself yields a quantitative binding delta-delta-G. Separate (1) MD-supported interaction observations from (2) the literature-calibrated affinity direction. If the short MD metrics are noisy or inconclusive, say so explicitly while still reporting the calibrated direction and limitations.

Time budget: complete this task within 30 minutes of wall-clock — preparing both ligand complexes, running the short comparative MD, analysing, and writing the submission. Size your MD (production length and any replicate count) to finish within that budget; short consistency-evidence MD is expected here, not a converged free energy.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

For a completed submission, the manifest must point `outputs.trajectories` to the benzene-complex and n-butylbenzene-complex production trajectories (benzene first, n-butylbenzene second) and `outputs.topology` to the matching topologies (same order), so the scorer can reload each trajectory against its topology and verify that the two complexes differ by exactly the bound ligand. Populate `metrics.md_analysis` and mirror the important quantitative values in `evidence_report.evidence.md_metrics`. Set `evidence_report.effect.direction` to one of `stronger_binding`, `weaker_binding`, or `similar`. Public literature may be cited for calibration, but the submitted MD numbers must be used as consistency evidence rather than overclaimed as a standalone binding free energy. Include calibrated confidence, public sources retrieved, and explicit limitations.
