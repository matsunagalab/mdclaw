# P19_prep_nmr_model_selection: Candidate/model selection

You are evaluating an MD agent on `P19_prep_nmr_model_selection`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Candidate/model selection: select model 5 from the PDB 2K39 NMR ensemble before preparation rather than silently using model 1 or averaging the ensemble. The scorer verifies model selection from the submitted coordinates against a fixed model-5 reference, so no self-reported source-selection evidence is required.

Public source anchors: PDB 2K39.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Run a short `mdclaw run_minimization` min-node step or an equivalent OpenMM minimization/finite-energy check, then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.



You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
