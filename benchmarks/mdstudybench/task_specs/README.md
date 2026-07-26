# MDStudyBench Task Specs

The active generator builds only the S01 v2 pilot. S02-S04 remain frozen
`grounded_correct_v1` fixtures in `tasks/` and are not regenerated.

`defaults.json` contains the shared submission files, held-out truth check, and
reject policy. The S01 task spec owns the scientific target and the deterministic
primary-evidence contract:

- native verifier;
- direction-to-outcome mapping;
- confidence and equivalence rule;
- fixed observable semantics and minimum sampling adequacy;
- task-owned validity-control semantics; and
- certified execution adapter.

It does not own a structure, PDB ID, preparation workflow, replica layout, or
sampling strategy above the published minimum adequacy floor. Those remain
agent choices.

After changing the pilot spec, regenerate and check the canonical task:

```bash
PYTHONPATH="$PWD" python benchmarks/mdstudybench/scripts/generate_tasks.py
PYTHONPATH="$PWD" python benchmarks/mdstudybench/scripts/generate_tasks.py --check
```
