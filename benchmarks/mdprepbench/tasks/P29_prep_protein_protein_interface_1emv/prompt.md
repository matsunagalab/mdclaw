# P29_prep_protein_protein_interface_1emv: MD system preparation

You are evaluating an MD agent on `P29_prep_protein_protein_interface_1emv`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Protein-protein complex preparation: prepare the colicin E9 DNase domain in complex with its Im9 immunity protein from PDB 1EMV, keeping both binding partners as two distinct protein chains rather than dropping one partner or merging them. Exclude crystallographic buffer components and neutralize the system.

Public source anchors: PDB 1EMV.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
