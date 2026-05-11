# T08 T4 Lysozyme Dynamics Communication

You are evaluating an MD agent on `T08_communicate_t4l_dynamics`.

Use only these public files:

- `task.json`
- `input/2LZM.pdb`
- `input/analysis_request.json`

Do not read `truth/` or `scorer/` if those directories exist.

Task: produce an auditable evidence package for T4 lysozyme dynamics. Run brief MD or analyze a trajectory produced during this task, compute backbone RMSD, per-residue C-alpha RMSF, and C-alpha contact frequency, then make figures whose captions match the submitted metrics.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `figures/`

Submit at least three real PNG figures and list them in `manifest.outputs.figures`. Numeric claims in captions must match `metrics.json` within the tolerance specified in `task.json`. The evidence report should include captions, metrics, provenance links, and limitations.

