# P27_prep_manganese_metalloenzyme_3cna: MD system preparation

You are evaluating an MD agent on `P27_prep_manganese_metalloenzyme_3cna`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Non-zinc metal cofactor preparation: prepare concanavalin A (PDB 3CNA) while retaining both its structural Mn2+ and Ca2+ ions as supported metal ions and keeping the Mn coordination shell (including His24), rather than dropping the metals or treating them as generic ligands. Neutralize the resulting system.

Public source anchors: PDB 3CNA.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
