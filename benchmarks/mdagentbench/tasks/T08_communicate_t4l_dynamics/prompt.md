# T08 T4 Lysozyme Dynamics Communication

You are evaluating an MD agent on `T08_communicate_t4l_dynamics`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: retrieve wild-type T4 lysozyme from PDB entry 2LZM and produce an auditable evidence package for T4 lysozyme dynamics. Run brief MD or analyze a trajectory produced during this task, compute backbone RMSD relative to the first frame, per-residue C-alpha RMSF, and C-alpha contact-frequency summaries using an 8 Å contact cutoff with at least four residues sequence separation. Generate figures whose captions match the submitted metrics.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `figures/`

Submit at least three real PNG figures, for example RMSD, RMSF, and contact-frequency figures, and list them in `manifest.outputs.figures`. Numeric claims in captions must match `metrics.json`. The evidence report should include captions, metrics, provenance links, public sources retrieved, and limitations.

