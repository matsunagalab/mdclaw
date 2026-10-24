---
title: Official MDDB schema snapshot
updated: 2026-10-24
---

# Official MDDB schema snapshot

Source: [mmb-irb/MDDB-workflow](https://github.com/mmb-irb/MDDB-workflow/tree/2b380ae29b682c97665c8290fce06da46f49eb57),
commit `2b380ae29b682c97665c8290fce06da46f49eb57`.
The upstream Apache 2.0 license is included in `LICENSE`.

- `inputs_schema.py.txt` is a byte-for-byte copy of
  `mddb_workflow/core/inputs_schema.py`. The `.txt` suffix keeps formatters and
  lint tools from modifying the upstream source.
- `constants.json` contains the four values imported by that schema:
  `PDB_ID_FORMAT`, `AVAILABLE_CHECKINGS`, `AVAILABLE_FAILURES`, and
  `AUTOMATIC_FLAG`. They were extracted from assignments in upstream
  `mddb_workflow/utils/constants.py`, resolving literal values, references to
  other constants, and list concatenation without executing that module.
- `provenance.json` records source paths, commit, the original constants file's
  SHA-256, and checksums for the three bundled files. The test loader verifies
  the bundled checksums before loading the schema.

`mdclaw/evidence/mddb_schema.py` executes the unchanged schema with its constants
and local substitutes for `InputError` and `warn`. Errors and unexpected
warnings fail validation. No MDDB workflow installation, GROMACS, database, or
network access or pytest is needed. Imports are scoped to the schema's own
namespace, without modifying process-wide imports or installed MDDB modules.
Project metadata and each MD's effective overrides
are checked with `validate_inputs(strict_unknown=True)`; inputs are copied
because upstream validation can normalize ligand entries in place.

`export_mddb` validates each generated YAML before publishing the output
directory. A validation or schema-loading failure returns `mddb_export_failed`
and cleans up staging files. The manifest records the schema commit and hash;
`mddb_server_validated` remains false because no ingestion has been run.

The autouse fixture in `tests/test_mddb_export.py` uses the same validator on every `inputs.yaml`
left under each test's temporary directory. This covers Python and CLI exports,
replicas, separate projects, combined trajectories, and reference metadata.
It is a pinned schema compatibility test, not a full MDDB ingestion or a check
that database identifiers exist. Upstream updates are reviewed manually.

## Updating the snapshot

1. Choose and record an explicit upstream commit. Review changes to the schema,
   its imported constants, and the license.
2. Copy the schema and license directly from that commit with `git show`,
   preserving bytes. Extract the four constant values from the same commit;
   avoid importing the upstream constants module, which checks runtime tools.
3. Update `provenance.json` with the commit and SHA-256 hashes of the upstream
   constants source and all bundled files. Keep this source link in sync.
4. Run `pixi run pytest tests/test_mddb_export.py -q`. Fix compatibility changes
   deliberately rather than skipping validation or loosening the schema.
