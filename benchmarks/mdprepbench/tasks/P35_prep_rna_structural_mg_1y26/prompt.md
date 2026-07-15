# P35_prep_rna_structural_mg_1y26: MD system preparation

You are evaluating an MD agent on `P35_prep_rna_structural_mg_1y26`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: RNA with structural metal ions: prepare the adenine riboswitch aptamer RNA from PDB 1Y26 as an explicit-solvent system, keeping the RNA aptamer and at least one structural Mg2+ ion (rather than discarding the ordered metals), and neutralizing the highly charged nucleic-acid system. Exclude the crystallographic adenine ligand and any buffer components.

Public source anchors: PDB 1Y26.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
