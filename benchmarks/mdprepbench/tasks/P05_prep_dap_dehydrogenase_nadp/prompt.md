# P05_prep_dap_dehydrogenase_nadp: Charged cofactor-like ligand stress

You are evaluating an MD agent on `P05_prep_dap_dehydrogenase_nadp`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Charged cofactor-like ligand stress: prepare DAP dehydrogenase with both deposited NDP cofactors (NADPH dihydro-nicotinamide-adenine-dinucleotide phosphate; chains C and F, auth chains A and B) without silently dropping either cofactor or changing its charge without provenance.

The only deposited ligand/cofactor expected in the submitted artifacts is NDP. Retain exactly the two deposited NDP cofactors, and do not include other deposited ligands or organic co-solutes in the final prepared structure, minimized structure, or OpenMM topology bundle. Added solvent and neutralizing ions, if you choose to use them, are not considered deposited ligands.

Public source anchors: PDB 1DAP.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Energy-minimize the built system to a relaxed state — free of steric clashes and at a stable, negative potential energy, not merely finite — then record the result in `minimization_report.json` and `metrics.json`. Full equilibration, production MD, and explicit solvent are not required for this prep task; a compact vacuum or implicit-solvent OpenMM topology is acceptable if the scorer can reload it, the energy is finite, and both NDP cofactors are retained.



You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
