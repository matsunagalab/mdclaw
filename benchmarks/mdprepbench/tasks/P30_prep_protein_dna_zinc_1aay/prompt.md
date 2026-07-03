# P30_prep_protein_dna_zinc_1aay: Protein-DNA complex with metal

You are evaluating an MD agent on `P30_prep_protein_dna_zinc_1aay`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Protein-DNA complex with metal: prepare the Zif268 zinc-finger-DNA complex from PDB 1AAY. Keep the DNA duplex (both strands), all three structural Zn2+ ions, and the zinc-finger protein together in one mixed-polymer system. Select DNA-compatible and metal-ion-compatible force-field libraries, and neutralize the highly charged nucleic-acid system with counterions.

Public source anchors: PDB 1AAY.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Run a short OpenMM minimization/finite-energy check, then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.

You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json`.
