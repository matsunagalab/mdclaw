# P35_prep_rna_structural_mg_1y26: RNA with structural metal ions

You are evaluating an MD agent on `P35_prep_rna_structural_mg_1y26`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: RNA with structural metal ions: prepare the adenine riboswitch aptamer RNA from PDB 1Y26 as an explicit-solvent system, keeping the RNA aptamer and at least one structural Mg2+ ion (rather than discarding the ordered metals), and neutralizing the highly charged nucleic-acid system. Exclude the crystallographic adenine ligand and any buffer components.

Public source anchors: PDB 1Y26.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Energy-minimize the built system to a relaxed state — free of steric clashes and at a stable, negative potential energy, not merely finite — then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.

You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json`.
