# S02_ppi_hotspot_barnase_d39a: Barnase-Barstar D39A Binding Effect

You are evaluating an MD agent on `S02_ppi_hotspot_barnase_d39a`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: retrieve the barnase-barstar complex from PDB entry 1BRS, create the D39A mutant on barstar (ASP 39 to ALA; barstar is chain D in 1BRS, paired with barnase chain A), and predict whether the mutation weakens binding, strengthens binding, or has neutral effect on barnase-barstar binding using MD-derived evidence. Compare WT and D39A complex simulations and compute interface-focused metrics such as interface SASA, inter-chain contact count, hydrogen-bond count, salt-bridge count, interface water occupancy, and local flexibility changes.

Public source anchors: PDB 1BRS.

Time budget: complete this task within 30 minutes of wall-clock — preparing both complexes, running the short comparative MD, analysing, and writing the submission. Size your MD (production length and any replicate count) to finish within that budget; short consistency-evidence MD is expected here, not a converged free energy.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

For a completed submission, the manifest must point `outputs.trajectories` to the WT-complex and D39A-complex production trajectories (WT first, mutant second) and `outputs.topology` to the matching WT and mutant topologies (same order), so the scorer can reload each trajectory against its topology and verify that the two systems differ by exactly the ASP39->ALA substitution. Populate `metrics.md_analysis` and mirror the important quantitative values in `evidence_report.evidence.md_metrics`. Set `evidence_report.effect.direction` to one of `weakened_binding`, `strengthened_binding`, or `neutral`. Public literature may be cited for confidence calibration, but the direction must be defended by submitted MD numbers and artifacts. Include calibrated confidence, public sources retrieved, and explicit limitations.
