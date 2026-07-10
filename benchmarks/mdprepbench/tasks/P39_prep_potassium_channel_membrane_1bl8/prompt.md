# P39_prep_potassium_channel_membrane_1bl8: Oligomeric potassium-channel membrane preparation

You are evaluating an MD agent on `P39_prep_potassium_channel_membrane_1bl8`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Oligomeric ion-channel membrane preparation: prepare the biological tetramer of the KcsA potassium channel from PDB 1BL8 embedded in a POPC lipid bilayer, retaining the deposited pore K+ ions and neutralizing the membrane system. This exercises an oligomeric alpha-helical membrane-channel system with functional pore ions, complementing the single-chain TMEM14A and beta-barrel OmpA membrane tasks.

Public source anchors: PDB 1BL8.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Energy-minimize the built system to a relaxed state — free of steric clashes and at a stable, negative potential energy, not merely finite — then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.

You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json`.
