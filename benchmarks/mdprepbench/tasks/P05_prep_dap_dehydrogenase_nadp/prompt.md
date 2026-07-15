# P05_prep_dap_dehydrogenase_nadp: MD system preparation

You are evaluating an MD agent on `P05_prep_dap_dehydrogenase_nadp`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Cofactor parameterization stress: prepare DAP dehydrogenase in explicit solvent with exactly the two deposited NDP cofactors (NADPH dihydro-nicotinamide-adenine-dinucleotide phosphate; chains C and F, auth chains A and B), parameterizing every atom while excluding other deposited ligands or organic co-solutes.

Public source anchors: PDB 1DAP.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
