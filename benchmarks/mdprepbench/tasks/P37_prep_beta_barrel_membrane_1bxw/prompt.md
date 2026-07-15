# P37_prep_beta_barrel_membrane_1bxw: MD system preparation

You are evaluating an MD agent on `P37_prep_beta_barrel_membrane_1bxw`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Beta-barrel membrane protein preparation: prepare the transmembrane domain of outer-membrane protein A (OmpA) from PDB 1BXW embedded in a POPC lipid bilayer, excluding the crystallographic detergent (C8E), and neutralizing the system. This exercises membrane embedding for a beta-barrel architecture rather than an alpha-helical membrane protein.

Public source anchors: PDB 1BXW.

Prepare the requested system and energy-minimize it. Write only these raw artifacts to the exact submission directory:

- `topology/system.xml`
- `topology/topology.pdb`
- `topology/state.xml`
- `prepared_structure.pdb`

`topology/state.xml` must contain the post-minimization OpenMM state and must be self-consistent with `topology/system.xml` and `topology/topology.pdb`. Full equilibration and production MD are not required.

Do not write `manifest.json`, `metrics.json`, `provenance.json`, `minimized_structure.pdb`, `minimization_report.json`, `evidence_report.json`, a command log, walltime estimates, or artifact hashes. The evaluator derives the normalized metadata, minimized view, minimization report, and hashes from the raw artifacts. Evidence reports and solver command logs are not part of MDPrepBench v0.3. The harness owns the final record and measures walltime; non-MDClaw stage labels are solver-declared.
