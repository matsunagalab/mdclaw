# P32_prep_sidechain_completion_1csp: MD system preparation

You are evaluating an MD agent on `P32_prep_sidechain_completion_1csp`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Missing side-chain reconstruction: prepare cold-shock protein CspB (PDB 1CSP), whose deposited structure is missing side-chain atoms for several surface glutamates (Glu3, Glu21, Glu36, Glu66 lack CG/CD/OE1/OE2). Rebuild the truncated side chains to their full heavy-atom set before building the topology, rather than capping or deleting the incomplete residues.

Public source anchors: PDB 1CSP.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
