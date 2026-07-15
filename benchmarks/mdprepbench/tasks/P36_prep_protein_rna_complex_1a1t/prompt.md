# P36_prep_protein_rna_complex_1a1t: MD system preparation

You are evaluating an MD agent on `P36_prep_protein_rna_complex_1a1t`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Protein-RNA complex preparation: prepare model 1 of the HIV-1 nucleocapsid protein NCp7 bound to the SL3 RNA stem-loop from the PDB 1A1T NMR ensemble, keeping the protein and its bound RNA stem-loop together in one mixed-polymer explicit-solvent system, retaining both structural Zn2+ zinc-knuckle ions, and neutralizing the charged nucleic-acid system.

Public source anchors: PDB 1A1T.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
