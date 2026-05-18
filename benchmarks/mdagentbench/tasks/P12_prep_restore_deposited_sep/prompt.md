# P12_prep_restore_deposited_sep: Phosphorylated residue restore

You are evaluating an MD agent on `P12_prep_restore_deposited_sep`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Phosphorylated residue restore: detect deposited SEP, clean the standard protein, restore phosphorylation, and build a topology-ready structure.

Public source anchors: PDB 5K9P.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to the backend-specific topology artifacts and `outputs.minimized_structure` to a structure after minimization. For OpenMM or MDClaw submissions, `outputs.topology` should be a JSON list containing the `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Run a short minimization or equivalent backend-native energy check and record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.



The submission must be backend-neutral. You may use MDClaw, OpenMM scripts, Amber, GROMACS, MDCrow, or another MD-preparation workflow, but the final files must satisfy the artifact contract above. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
