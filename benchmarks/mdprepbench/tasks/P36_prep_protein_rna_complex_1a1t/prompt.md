# P36_prep_protein_rna_complex_1a1t: Protein-RNA complex preparation

You are evaluating an MD agent on `P36_prep_protein_rna_complex_1a1t`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Protein-RNA complex preparation: prepare model 1 of the HIV-1 nucleocapsid protein NCp7 bound to the SL3 RNA stem-loop from the PDB 1A1T NMR ensemble, keeping the protein and its bound RNA stem-loop together in one mixed-polymer explicit-solvent system, retaining both structural Zn2+ zinc-knuckle ions, and neutralizing the charged nucleic-acid system.

Public source anchors: PDB 1A1T.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Energy-minimize the built system to a relaxed state — free of steric clashes and at a stable, negative potential energy, not merely finite — then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.

You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json`.
