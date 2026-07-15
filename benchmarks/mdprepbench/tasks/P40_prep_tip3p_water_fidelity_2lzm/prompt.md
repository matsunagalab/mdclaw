# P40_prep_tip3p_water_fidelity_2lzm: MD system preparation

You are evaluating an MD agent on `P40_prep_tip3p_water_fidelity_2lzm`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: TIP3P water-model fidelity: build an explicit-solvent system with the requested 3-site TIP3P water model rather than silently falling back to a different water model such as OPC.

Public source anchors: PDB 2LZM.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
