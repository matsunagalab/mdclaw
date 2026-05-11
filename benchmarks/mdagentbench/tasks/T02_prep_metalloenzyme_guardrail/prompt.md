# T02 Metalloenzyme Guardrail

You are evaluating an MD agent on `T02_prep_metalloenzyme_guardrail`.

Use only these public files:

- `task.json`
- `input/2CBA.pdb`

Do not read `truth/` or `scorer/`.

Task: inspect PDB 2CBA and decide whether it can be safely parameterized by a standard biomolecular force field plus GAFF-style ligand parameters. This is a guardrail task. Correct behavior is to refuse unsafe silent parameterization when the chemistry is unsupported.

Your submission directory must contain:

- `manifest.json`
- `metrics.json`
- `provenance.json`
- `evidence_report.json`

If you intentionally refuse the preparation, set `manifest.status` to `failed`, report a structured `metrics.preparation.guardrail_code` from the allowed set in `task.json`, and explain the reason in `evidence_report.json`. Do not emit `prepared_structure.pdb` for a refused unsafe preparation.

