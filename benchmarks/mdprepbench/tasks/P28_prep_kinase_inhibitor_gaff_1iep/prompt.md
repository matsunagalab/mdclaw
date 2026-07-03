# P28_prep_kinase_inhibitor_gaff_1iep: Custom ligand parameterization

You are evaluating an MD agent on `P28_prep_kinase_inhibitor_gaff_1iep`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Custom ligand parameterization: prepare the Abl kinase-imatinib complex from PDB 1IEP. Keep protein chain A together with the deposited imatinib ligand (residue name STI), generate small-molecule force-field parameters for the drug-like ligand so every ligand atom is parameterized, and preserve the crystallographic imatinib binding pose. Do not drop the ligand or submit a ligand-only structure. Some tools may list STI separately from the protein during inspection, so make sure it is still included in the built system.

Public source anchors: PDB 1IEP.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Run a short OpenMM minimization/finite-energy check, then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.

You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json`.
