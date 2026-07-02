# Literature And Database Lookup

Study planning must be grounded in current databases and literature, not in the
agent's training-data memory. Knowledge of "good PDB IDs" and "typical
comparisons" is often stale or imprecise (wrong chain, unexpected ligand,
superseded by a higher-resolution entry). Use the native tools before designing
the plan.

For a direct run of one named PDB (see `skills/md-study/direct-run.md`), this is
optional: a single `get_structure_info` is enough and `pubmed_search` can be
skipped. The contract below is for multi-system or comparative studies.

1. **Structure candidates** — run `search_structures` (use `--rank-for-md` for
   MD-suitability ordering by resolution, method, and chain composition) and/or
   `get_structure_info --pdb-id <id>` for any candidate the user named. Note
   resolution, method, chain composition, ligands, and bound cofactors that
   matter to the hypothesis (Ca2+, peptide, NADP, lipid, etc.).
2. **Sequence / functional context** — when the user names a protein but not a
   structure, run `search_proteins` and `get_protein_info` against UniProt to
   confirm the canonical sequence, isoforms, and PTM sites.
3. **Prior MD or structural work** — run `pubmed_search` on the system and
   hypothesis (e.g. `"calmodulin MLCK molecular dynamics"`) and `pubmed_fetch
   --pmids ...` on the most relevant 1-3 PMIDs. This surfaces typical
   observables, force-field choices, timescales, and known pitfalls.

Record what you consulted under `notes.references` so later agents and reviewers
can see the evidence base:

```json
"notes": {
  "references": {
    "pdb_ids": ["1CDL", "1CLL", "1CFD"],
    "pmids": ["12345678", "23456789"],
    "summary": "1CDL chosen as master start (X-ray 2.0 A, single CaM chain + 19-residue MLCK peptide, 4 Ca2+). 1CLL and 1CFD cited as references for the holo_nopep and apo_nopep cells."
  }
}
```
