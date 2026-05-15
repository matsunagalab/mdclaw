# P07_prep_rna_crystallographic_ions: Crystallographic ion triage

You are evaluating an MD agent on `P07_prep_rna_crystallographic_ions`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Crystallographic ion triage: prepare oligo(U) RNA while preserving prompt-designated crystallographic K+ ions and excluding irrelevant solvent or buffer components.

Public source anchors: PDB 4RBQ.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
