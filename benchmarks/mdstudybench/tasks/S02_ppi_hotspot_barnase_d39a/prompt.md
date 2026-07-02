# S02_ppi_hotspot_barnase_d39a: Barnase-Barstar D39A Binding Effect

You are evaluating an MD agent on `S02_ppi_hotspot_barnase_d39a`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: retrieve the barnase-barstar complex from PDB entry 1BRS, create the D39A mutant on barstar (ASP 39 to ALA; barstar is chain D in 1BRS, paired with barnase chain A), and predict whether the mutation weakens binding, strengthens binding, or has neutral effect on barnase-barstar binding using MD-derived evidence. Compare WT and D39A complex simulations and compute interface-focused metrics such as interface SASA, inter-chain contact count, hydrogen-bond count, salt-bridge count, interface water occupancy, and local flexibility changes.

Public source anchors: PDB 1BRS.

Time budget: you have up to 24 hours of wall-clock for the entire task — preparing both complexes, running the comparative MD, analysing, and writing the submission. No target simulation length is prescribed: decide the production length and any replicate count yourself so the whole workflow finishes within 24 hours on your hardware. This MD planning (converting the wall-clock budget into a sampling plan) is part of the task. Aim for enough sampling — longer and/or replicated runs — to make the WT-versus-mutant interface comparison meaningful, but remember that no attainable trajectory here proves a converged binding delta-delta-G; separate MD-supported observations from the literature-calibrated interpretation and state your limitations.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

For a completed submission, the manifest must point `outputs.trajectories` to the WT-complex and D39A-complex production trajectories (WT first, mutant second) and `outputs.topology` to the matching WT and mutant topologies (same order), so the scorer can reload each trajectory against its topology and verify that the two systems differ by exactly the ASP39->ALA substitution. Populate `metrics.md_analysis` and mirror the important quantitative values in `evidence_report.evidence.md_metrics`. In `evidence_report.observables`, report each discriminating observable you relied on with its wild-type and mutant mean values, an uncertainty estimate, and the direction it supports, and add a `reasoning` field explaining how those numbers lead to your `effect.direction`; the scorer recomputes your primary discriminating observable from the submitted trajectories and checks both that your reported values match and that their sign is consistent with the direction you claim. Set `evidence_report.effect.direction` to one of `weakened_binding`, `strengthened_binding`, or `neutral`. Public literature may be cited for confidence calibration, but the direction must be defended by submitted MD numbers and artifacts. Include calibrated confidence, public sources retrieved, and explicit limitations.
