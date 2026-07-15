# P38_prep_implicit_complex_mdm2_p53_1ycr: MD system preparation

You are evaluating an MD agent on `P38_prep_implicit_complex_mdm2_p53_1ycr`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Implicit-solvent complex preparation: prepare the MDM2-p53 transactivation-peptide complex from PDB 1YCR in implicit (generalized-Born) solvent, keeping both the MDM2 protein and the bound p53 peptide as two distinct chains and avoiding any explicit water box. This exercises implicit-solvent setup for an associated two-partner system rather than a single monomer.

Public source anchors: PDB 1YCR.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
