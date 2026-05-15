# P04_prep_multi_ligand_filter_3pwb: Multi-ligand inclusion and exclusion

You are evaluating an MD agent on `P04_prep_multi_ligand_filter_3pwb`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Multi-ligand inclusion and exclusion: retain requested BEN/GOL-like ligands while excluding irrelevant buffer molecules and unrequested heterogens.

Public source anchors: PDB 3PWB.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
