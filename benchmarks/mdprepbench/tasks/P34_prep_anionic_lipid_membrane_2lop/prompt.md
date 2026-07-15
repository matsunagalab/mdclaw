# P34_prep_anionic_lipid_membrane_2lop: MD system preparation

You are evaluating an MD agent on `P34_prep_anionic_lipid_membrane_2lop`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Anionic-lipid membrane preparation: prepare model 1 of TMEM14A from the PDB 2LOP NMR ensemble in a mixed POPC:POPG membrane containing anionic POPG lipids (nominal 3:1 POPC:POPG or an equivalent membrane-builder setting), and add counterions so the anionic bilayer system is net neutral. Realized integer lipid counts may follow the builder's box-size and area-per-lipid behavior.

Public source anchors: PDB 2LOP.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
