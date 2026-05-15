# P17_prep_modified_nucleic_5mc: Modified nucleic acid

You are evaluating an MD agent on `P17_prep_modified_nucleic_5mc`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Modified nucleic acid: detect 5-methylcytosine and route the structure through modified-nucleic preparation rather than silently mapping it to ordinary cytosine.

Public source anchors: PDB 6JV5.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
