# S04_affinity_t4l_l99a_alkylbenzene: T4 Lysozyme L99A Apolar Ligand Affinity

You are evaluating an MD agent on `S04_affinity_t4l_l99a_alkylbenzene`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: using the engineered T4 lysozyme L99A apolar cavity, compare the binding of benzene (PDB 4W52, also 181L) with n-butylbenzene (PDB 4W57) and report which ligand binds more tightly. Run comparative MD of the two L99A-ligand complexes and compute interaction evidence such as ligand-cavity contact counts, buried apolar surface, and ligand occupancy or retention in the cavity, then use those numbers to explain the relative affinity. Report `effect.direction` for n-butylbenzene RELATIVE TO benzene.

Public source anchors: PDB 4W52 (benzene), PDB 4W57 (n-butylbenzene).

Do not claim that a short trajectory by itself yields a quantitative binding delta-delta-G. Separate (1) MD-supported interaction observations from (2) the literature-calibrated affinity direction. If the MD metrics are noisy or inconclusive, say so explicitly while still reporting the calibrated direction and limitations.

Time budget: you have up to 24 hours of wall-clock for the entire task — preparing both ligand complexes, running the comparative MD, analysing, and writing the submission. No target simulation length is prescribed: decide the production length and any replicate count yourself so the whole workflow finishes within 24 hours on your hardware. This MD planning (converting the wall-clock budget into a sampling plan) is part of the task. Aim for enough sampling — longer and/or replicated runs — to make the two-ligand comparison meaningful, but remember that no attainable trajectory here proves a converged binding delta-delta-G; separate MD-supported observations from the literature-calibrated interpretation and state your limitations.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

For a completed submission, the manifest must point `outputs.trajectories` to the benzene-complex and n-butylbenzene-complex production trajectories (benzene first, n-butylbenzene second) and `outputs.topology` to the matching topologies (same order), so the scorer can reload each trajectory against its topology and verify that the two complexes differ by exactly the bound ligand. Populate `metrics.md_analysis` and mirror the important quantitative values in `evidence_report.evidence.md_metrics`. In `evidence_report.observables`, report each discriminating observable you relied on with its benzene-complex and n-butylbenzene-complex mean values, an uncertainty estimate, and the direction it supports, and add a `reasoning` field explaining how those numbers lead to your `effect.direction`; the scorer recomputes your primary discriminating observable from the submitted trajectories and checks both that your reported values match and that their sign is consistent with the direction you claim. Set `evidence_report.effect.direction` to one of `stronger_binding`, `weaker_binding`, or `similar`. Public literature may be cited for calibration, but the submitted MD numbers must be used as consistency evidence rather than overclaimed as a standalone binding free energy. Include calibrated confidence, public sources retrieved, and explicit limitations.
