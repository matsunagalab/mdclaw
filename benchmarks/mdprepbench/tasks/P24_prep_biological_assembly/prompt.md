# P24_prep_biological_assembly: MD system preparation

You are evaluating an MD agent on `P24_prep_biological_assembly`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Assembly/biological unit choice: generate or select biological assembly 1 of PDB 1STP. The scorer verifies the submitted coordinates against a fixed assembly-1 reference and checks that the submitted structure contains four protein chains, so assembly identity is not accepted from self-reported JSON alone.

Public source anchors: PDB 1STP assembly 1; stress variant PDB 2MS2 assembly 1.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
