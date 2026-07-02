# MDPrepBench Task Specs

These files are the compact maintenance source for MDPrepBench task contracts.
The scorer-facing files remain `tasks/<task_id>/task.json`; benchmark agents
and harnesses should continue to use those canonical task files.

Shared preparation requirements live in `defaults.json`:

- common required outputs
- artifact-integrity checks
- the OpenMM topology / minimization deterministic check bundle (which also
  includes `structure_geometry_quality`, a steric-clash / geometry sanity gate)
- common score axes, tool tags, and rubrics

Task-specific checks can accept multiple valid answers deterministically. For
example `pdb_residue_state` supports `allowed_residue_names` and
`accepted_atom_name_sets` (e.g. HID vs HIE tautomers), and any check can be
promoted to the physical-validity gate with `hard_fail: true`.

Each `tasks/<task_id>.json` contains only the task-specific metadata and
deterministic checks. The `{"$bundle": "topology_minimization"}` placeholder is
expanded into the shared topology / minimization checks during generation.

After editing specs, regenerate canonical task files from the repository root:

```bash
conda run -n mdclaw python benchmarks/mdprepbench/scripts/generate_tasks.py
```

Check for drift without rewriting files:

```bash
conda run -n mdclaw python benchmarks/mdprepbench/scripts/generate_tasks.py --check
```
