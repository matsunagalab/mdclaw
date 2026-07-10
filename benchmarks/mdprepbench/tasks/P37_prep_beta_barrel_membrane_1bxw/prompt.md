# P37_prep_beta_barrel_membrane_1bxw: Beta-barrel membrane protein preparation

You are evaluating an MD agent on `P37_prep_beta_barrel_membrane_1bxw`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Beta-barrel membrane protein preparation: prepare the transmembrane domain of outer-membrane protein A (OmpA) from PDB 1BXW embedded in a POPC lipid bilayer, excluding the crystallographic detergent (C8E), and neutralizing the system. This exercises membrane embedding for a beta-barrel architecture rather than an alpha-helical membrane protein.

Public source anchors: PDB 1BXW.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Energy-minimize the built system to a relaxed state — free of steric clashes and at a stable, negative potential energy, not merely finite — then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.

You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json`.
