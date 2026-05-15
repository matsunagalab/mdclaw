# P06_prep_calmodulin_ca_ions: Supported metal ion retention

You are evaluating an MD agent on `P06_prep_calmodulin_ca_ions`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Supported metal ion retention: prepare calcium-bound calmodulin while treating Ca2+ as supported ions rather than generic ligands.

Public source anchors: PDB 1CLL.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
