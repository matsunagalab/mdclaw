# P29_prep_protein_protein_interface_1emv: Protein-protein complex preparation

You are evaluating an MD agent on `P29_prep_protein_protein_interface_1emv`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Protein-protein complex preparation: prepare the colicin E9 DNase domain in complex with its Im9 immunity protein from PDB 1EMV. Keep both binding partners as two distinct protein chains so the interface is retained, rather than dropping one partner or merging them into a single chain. Exclude crystallographic buffer components and neutralize the system.

Public source anchors: PDB 1EMV.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Run a short OpenMM minimization/finite-energy check, then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.

You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json`.
