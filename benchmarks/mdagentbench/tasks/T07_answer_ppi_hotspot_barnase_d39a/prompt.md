# T07 Barnase D39A Binding Effect

You are evaluating an MD agent on `T07_answer_ppi_hotspot_barnase_d39a`.

Use only these public files:

- `task.json`
- `input/1BRS.pdb`
- `input/mutation_request.json`
- `input/references.json`

Do not read `truth/` or `scorer/`.

Task: predict whether barnase D39A strengthens, weakens, or has neutral effect on barnase-barstar binding using MD-derived evidence. Run short comparative MD for the WT complex and the D39A mutant complex, compute interface-focused quantitative differences, and use those numbers to support the answer.

Your submission directory must contain:

- `manifest.json`
- `provenance.json`
- `evidence_report.json`

For a completed submission, the manifest must point to real WT and mutant trajectory artifacts under `outputs.trajectories`. Populate `metrics.md_analysis` when you write `metrics.json`, and mirror the important quantitative values in `evidence_report.evidence.md_metrics`. Set `evidence_report.effect.direction` to one of the allowed values in `task.json`. Citations from `input/references.json` may be used for confidence calibration, but the direction must be defended by the MD numbers. Include calibrated confidence and explicit limitations.

