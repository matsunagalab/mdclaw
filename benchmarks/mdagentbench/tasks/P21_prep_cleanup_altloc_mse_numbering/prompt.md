# P21_prep_cleanup_altloc_mse_numbering: PDB cleanup, missing residues, and numbering

You are evaluating an MD agent on `P21_prep_cleanup_altloc_mse_numbering`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: PDB cleanup, missing residues, and numbering: resolve altloc choice, author numbering, MSE-to-MET handling, missing loops, termini, and whether to model or block.

Public source anchors: PDB 4Q5T.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
