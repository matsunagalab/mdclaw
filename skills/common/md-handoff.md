# Handoff To MD Preparation

Canonical handoff used by every source-producing prediction skill (Boltz,
MODELLER, BioEmu) once a `source` node holds one or more candidate structures.
Reference this page instead of repeating the wording.

Present the candidate IDs and any confidence scores to the user. In
`autonomous` mode, use the default (rank-1) candidate for a simple first MD
setup; prepare multiple jobs/candidates only when the scientific question needs
ensemble comparison.

To continue to MD simulation, create the `prep` node (its parent auto-resolves
to the `source` node), then run `prepare_complex` with the returned node id and
the chosen candidate:

```bash
mdclaw create_node --job-dir <job_dir> --node-type prep
mdclaw --job-dir <job_dir> --node-id <prep_node_id> prepare_complex \
  --source-candidate-id <candidate_id>
```

Then follow `skills/md-prepare/SKILL.md` on the same `job_dir`. If the harness
provides slash commands, `/md-prepare` is the interactive shortcut for the same
skill. Prediction does not auto-start MD preparation beyond this step.
