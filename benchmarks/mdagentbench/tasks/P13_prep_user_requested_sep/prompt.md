# P13_prep_user_requested_sep: User-requested phosphorylation

You are evaluating an MD agent on `P13_prep_user_requested_sep`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: User-requested phosphorylation: apply phosphorylation to unmodified ubiquitin Ser20 and prepare the resulting SEP-containing system.

Public source anchors: PDB 1UBQ.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
