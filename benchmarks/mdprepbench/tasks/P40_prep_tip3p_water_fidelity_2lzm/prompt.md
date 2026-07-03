# P40_prep_tip3p_water_fidelity_2lzm: Water-model fidelity (non-OPC)

You are evaluating an MD agent on `P40_prep_tip3p_water_fidelity_2lzm`.

Use this prompt as the task statement. Retrieve public sources as needed, and do not read `truth/` or `scorer/` if those directories exist.

Task: Water-model fidelity (non-OPC): honor a supported user-specified force-field/water pair using ff14SB with the 3-site TIP3P water model, rather than silently falling back to a different water model such as OPC.

Public source anchors: PDB 2LZM.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `prepared_structure.pdb`
- `minimized_structure.pdb`
- `minimization_report.json`

Your `manifest.json` must also point `outputs.topology` to an OpenMM topology bundle and `outputs.minimized_structure` to a structure after minimization. For prep battery v0.1, `outputs.topology` must be a JSON list containing the OpenMM `system.xml`, `topology.pdb`, and `state.xml` artifact triple. Run a short OpenMM minimization/finite-energy check, then record the result in `minimization_report.json` and `metrics.json`. Full equilibration and production MD are not required for this prep task.

You may use MDClaw, direct OpenMM scripts, or another preparation workflow upstream, but the final submitted topology must be an OpenMM artifact triple that the scorer can reload. Record sources retrieved, commands or tool actions, preparation decisions, limitations, and any non-default choices in `provenance.json`.
