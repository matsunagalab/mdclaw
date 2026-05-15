# P14_prep_glycoprotein_glycan: Glycoprotein/glycan pass-through

You are evaluating an MD agent on `P14_prep_glycoprotein_glycan`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Glycoprotein/glycan pass-through: keep N-linked glycans as glycans rather than treating them as ordinary small-molecule ligands.

Public source anchors: PDB 6YA2.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
