# P05_prep_dap_dehydrogenase_nadp: Charged cofactor-like ligand stress

You are evaluating an MD agent on `P05_prep_dap_dehydrogenase_nadp`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Charged cofactor-like ligand stress: prepare DAP dehydrogenase with its NADP-like dinucleotide cofactor without silently dropping the cofactor or changing its charge without provenance.

Public source anchors: PDB 1DAP.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
