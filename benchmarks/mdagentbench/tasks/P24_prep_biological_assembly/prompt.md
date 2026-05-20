# P24_prep_biological_assembly: Assembly/biological unit choice

You are evaluating an MD agent on `P24_prep_biological_assembly`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Assembly/biological unit choice: generate or select the requested biological assembly by assembly_id while preserving source auth/label/operator provenance and stable chain identity.

Public source anchors: PDB 1STP, PDB 2MS2.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Run a short minimization or OpenMM energy check and record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.

For machine-readable scoring, record `preparation.source_pdb_id = "1STP"`, `preparation.assembly_id = "1"`, and `preparation.assembly_chain_identity_map` in `metrics.json`. The identity map should cover generated output chains and include `source_pdb_id`, `assembly_id`, `source_auth_asym_id`, `source_label_asym_id` or `source_subchain_id`, `operator_id`, `output_chain_id`, and `naming_policy`. The submitted structure should represent assembly 1 rather than the asymmetric unit alone.

You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json` and `evidence_report.json`.
