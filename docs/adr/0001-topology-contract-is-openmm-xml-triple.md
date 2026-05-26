# Topology contract is the OpenMM XML triple

MDClaw topology Nodes emit a force-field-applied OpenMM `system.xml` + `topology.pdb` + `state.xml` triple, and equilibration / production Nodes consume that triple without reconstructing the System from ForceField XML or falling back to Amber `parm7` / `rst7`. This keeps the run-side contract portable and builder-agnostic across `build_amber_system` and `build_openmm_system`, at the cost of no longer treating legacy Amber topology files as normal run inputs.
