# P02_prep_1ake_chain_ap5: Chain and ligand selection

You are evaluating an MD agent on `P02_prep_1ake_chain_ap5`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Chain and ligand selection: prepare adenylate kinase chain A while retaining the AP5 ligand, even if the ligand is represented under a separate mmCIF label chain.

Public source anchors: PDB 1AKE.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Run a short OpenMM minimization/finite-energy check, then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.



You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
