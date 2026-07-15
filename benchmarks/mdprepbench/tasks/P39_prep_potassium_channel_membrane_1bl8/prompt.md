# P39_prep_potassium_channel_membrane_1bl8: MD system preparation

You are evaluating an MD agent on `P39_prep_potassium_channel_membrane_1bl8`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Oligomeric ion-channel membrane preparation: prepare the biological tetramer of the KcsA potassium channel from PDB 1BL8 embedded in a POPC lipid bilayer, retaining the deposited pore K+ ions and neutralizing the membrane system. This exercises an oligomeric alpha-helical membrane-channel system with functional pore ions, complementing the single-chain TMEM14A and beta-barrel OmpA membrane tasks.

Public source anchors: PDB 1BL8.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
