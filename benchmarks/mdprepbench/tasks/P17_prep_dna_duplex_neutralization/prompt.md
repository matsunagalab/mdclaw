# P17_prep_dna_duplex_neutralization: DNA duplex chain retention and neutralization

You are evaluating an MD agent on `P17_prep_dna_duplex_neutralization`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: DNA duplex chain retention and neutralization: prepare both chains of the standard B-DNA duplex in an explicit-solvent OpenMM system, select a DNA-compatible force-field library, and include explicit counterions so the submitted topology is charge-neutral rather than treating the duplex as a single protein-like chain.

Public source anchors: PDB 1BNA.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Energy-minimize the built system to a relaxed state — free of steric clashes and at a stable, negative potential energy, not merely finite — then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.

The submitted topology must represent an explicit-solvent, periodic system with explicit counterions and approximately neutral total charge. Do not satisfy the neutralization requirement only by describing a future downstream solvation step, by using a vacuum or `NoCutoff` topology, or by reporting neutralization without counterions in the submitted OpenMM artifact triple.



You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
