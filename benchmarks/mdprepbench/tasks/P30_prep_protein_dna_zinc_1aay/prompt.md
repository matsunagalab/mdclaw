# P30_prep_protein_dna_zinc_1aay: MD system preparation

You are evaluating an MD agent on `P30_prep_protein_dna_zinc_1aay`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Protein-DNA complex with metal: prepare the Zif268 zinc-finger-DNA complex from PDB 1AAY, keeping the DNA duplex (both strands), all three structural Zn2+ ions, and the protein together in one mixed-polymer system. Select DNA-compatible and metal-ion-compatible force-field libraries and neutralize the highly charged nucleic-acid system.

Public source anchors: PDB 1AAY.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
