# T06 T4 Lysozyme L99A Stability

You are evaluating an MD agent on `T06_answer_stability_t4l_l99a`.

Use only these public files:

- `task.json`
- `input/2LZM.pdb`
- `input/mutation_request.json`
- `input/references.json`

Do not read `truth/` or `scorer/`.

Task: predict whether T4 lysozyme L99A is stabilizing, destabilizing, or neutral relative to wild type using MD-derived evidence. Run short comparative MD for the WT and L99A systems, compute quantitative differences, and use those numbers to support the answer.

Your submission directory must contain:

- `manifest.json`
- `provenance.json`
- `evidence_report.json`

For a completed submission, the manifest must point to real WT and mutant trajectory artifacts under `outputs.trajectories`. Populate `metrics.md_analysis` when you write `metrics.json`, and mirror the important quantitative values in `evidence_report.evidence.md_metrics`. Set `evidence_report.effect.direction` to one of `destabilizing`, `stabilizing`, or `neutral`. Citations from `input/references.json` may be used for confidence calibration, but the direction must be defended by the MD numbers. Include calibrated confidence and explicit limitations.

