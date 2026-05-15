# P19_prep_nmr_model_selection: Candidate/model selection

You are evaluating an MD agent on `P19_prep_nmr_model_selection`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Candidate/model selection: select a specified NMR model/candidate before preparation rather than silently using model 1 or averaging the ensemble.

Public source anchors: PDB 2K39.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
