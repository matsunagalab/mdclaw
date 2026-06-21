# P08_prep_t4l_l99a_branch: Point mutation branch

You are evaluating an MD agent on `P08_prep_t4l_l99a_branch`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Point mutation branch: prepare WT T4 lysozyme and a branched L99A mutant without overwriting the WT artifacts or shifting residue numbering.

Public source anchors: PDB 2LZM.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`
- `wt_prepared_structure.pdb`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle, `outputs.minimized_structure` to a structure after minimization, and `outputs.parent_prepared_structure` to `wt_prepared_structure.pdb`. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Run a short `mdclaw run_minimization` min-node step or an equivalent OpenMM minimization/finite-energy check, then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.



You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
