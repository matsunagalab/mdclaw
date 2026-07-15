# P17_prep_dna_duplex_neutralization: MD system preparation

You are evaluating an MD agent on `P17_prep_dna_duplex_neutralization`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: DNA duplex chain retention and neutralization: prepare both chains of the standard B-DNA duplex in an explicit-solvent OpenMM system, select a DNA-compatible force-field library, and include explicit counterions so the submitted topology is charge-neutral rather than treating the duplex as a single protein-like chain.

Public source anchors: PDB 1BNA.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
