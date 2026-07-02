# Minimal Plan Schema

## Planning goal

The goal is not a grant-style research plan. Record just enough intent that
later agents can see: what question was asked, what MD can realistically test,
which jobs to prepare and why, which observables to analyze, and what results
would support, argue against, or leave the question unresolved.

## Schema

Keep the JSON small so weaker agents and re-entry flows can preserve it.
Required fields:

```json
{
  "plan_schema_version": 2,
  "question": "...",
  "md_goal": "...",
  "solvent_regime": "explicit",
  "jobs": [
    {
      "job_id": "main",
      "purpose": "..."
    }
  ],
  "analysis": ["..."],
  "decision": {
    "support": "...",
    "against": "...",
    "inconclusive": "..."
  }
}
```

Optional detail belongs under `notes` or extra per-job fields. Do not invent
precise replicate counts, production lengths, protonation states, or controls
unless the user requested them or they are clearly part of the design. Use
`unknown` or `to_be_decided` for uncertain details.

An optional top-level `budget` block records the user's compute budget and the
derived (replicates × length) plan. Include it only when the user mentioned
compute; see `skills/md-study/compute-budget.md`.

## Solvent regime (required study-level intent)

Decide the regime here, not at topology generation; it affects prep-time
component disposition such as whether explicit ion components are retained.

| `solvent_regime` | Downstream prepare behavior |
|---|---|
| `explicit` (default) | `prepare_complex --solvent-type explicit`, then `solvate_structure` |
| `implicit` | `prepare_complex --solvent-type implicit`, skip explicit solvation, topology needs `--implicit-solvent <MODEL>` |
| `vacuum` | `prepare_complex --solvent-type vacuum`, skip solvation and GB |
| `membrane` | `prepare_complex --solvent-type explicit`, then `embed_in_membrane` |

Default to `explicit` unless the user explicitly asks for implicit solvent,
vacuum/no-solvent, or a membrane workflow. Record the reason briefly in `notes`
when it is not the default.
