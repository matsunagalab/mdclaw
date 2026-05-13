# Structure Acquisition

Use a `source` node for the initial structural source bundle.

Common sources:

```bash
mdclaw create_node --job-dir <job_dir> --node-type source
mdclaw --job-dir <job_dir> --node-id source_001 fetch_structure --source pdb --pdb-id 1AKE
mdclaw --job-dir <job_dir> --node-id source_001 fetch_structure --source alphafold --uniprot-id P12345
mdclaw --job-dir <job_dir> --node-id source_001 fetch_structure --source local --file-path /abs/input.pdb
```

Rules:

- Copy the target identifier exactly from the user's request.
- One job has one `source` node. That node may contain multiple structures:
  NMR models, assembly/chain candidates, or generated ensemble members.
- The source tool normalizes executable candidates under
  `artifacts/candidates/` and records their provenance in `source_bundle.json`.
- Use `list_source_candidates` to show the selectable candidates, ranks,
  files, and any generator metrics such as Boltz confidence.
- If the source bundle contains multiple structures, pass an explicit
  `prepare_complex` selector such as `--source-structure-id candidate_002`.
- For Boltz-2 generated structures, create/register the output as the job's
  source bundle before continuing into preparation; the candidate metadata
  should carry Boltz rank, model index, confidence file, and confidence score
  when available.
- Run `inspect_molecules` after acquisition when chains, ligands, metals,
  glycans, nucleic acids, or PTMs may affect choices. Pass the same
  `--source-structure-id` selector to inspect a specific candidate.

```bash
mdclaw list_source_candidates --job-dir <job_dir> --node-id source_001
mdclaw inspect_molecules --job-dir <job_dir> --node-id source_001 --source-structure-id candidate_002
```
