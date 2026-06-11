# S02 Barnase D39A Binding Effect

You are evaluating an MD agent on `S02_ppi_hotspot_barnase_d39a`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/`.

Task: retrieve the barnase-barstar complex from PDB entry 1BRS, create the D39A mutant on barnase chain A (ASP 39 to ALA), and predict whether the mutation weakens binding, strengthens binding, or has neutral effect on barnase-barstar binding using MD-derived evidence. Compare WT and D39A complex simulations and compute interface-focused metrics such as interface SASA, inter-chain contact count, hydrogen-bond count, salt-bridge count, interface water occupancy, and local flexibility changes.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

For a completed submission, the manifest must point to real WT and mutant trajectory artifacts under `outputs.trajectories`. Populate `metrics.md_analysis` and mirror the important quantitative values in `evidence_report.evidence.md_metrics`. Set `evidence_report.effect.direction` to one of `weakened_binding`, `strengthened_binding`, or `neutral`. Public literature may be cited for confidence calibration, but the direction must be defended by submitted MD numbers and artifacts. Include calibrated confidence, public sources retrieved, and explicit limitations.
