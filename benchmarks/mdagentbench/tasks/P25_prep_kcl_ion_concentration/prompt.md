# P25_prep_kcl_ion_concentration: Specified ion concentration

You are evaluating an MD agent on `P25_prep_kcl_ion_concentration`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Specified ion concentration: build an explicit-solvent chignolin system that honors 0.30 M KCl while preserving net neutrality.

Public source anchors: PDB 5AWL.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`

The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
