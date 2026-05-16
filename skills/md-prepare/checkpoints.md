# Human-In-The-Loop Checkpoints

`execution_mode=autonomous` is the default. Ask only when a required choice is
missing, the target is ambiguous, or a structured failure requires a decision.

`execution_mode=human_in_the_loop` pauses before each major transition:

1. Source acquisition.
2. Chain and ligand selection after inspection.
3. Initial `prepare_complex`.
4. Optional branches: mutation and PTM restoration.
5. Solvation mode and topology build.
6. Handoff to `skills/md-equilibration/SKILL.md` (`/md-equilibration` if the
   harness provides slash commands).

Persist the mode after source creation:

```bash
mdclaw update_job_params --job-dir <job_dir> \
  --params '{"execution_mode":"autonomous"}'
```
