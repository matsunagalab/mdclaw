# Structure Acquisition

Use a `source` node for the initial structure.

Common sources:

```bash
mdclaw create_node --job-dir <job_dir> --node-type source
mdclaw --job-dir <job_dir> --node-id source_001 fetch_structure --source pdb --pdb-id 1AKE
mdclaw --job-dir <job_dir> --node-id source_001 fetch_structure --source alphafold --uniprot-id P12345
mdclaw --job-dir <job_dir> --node-id source_001 fetch_structure --source local --file-path /abs/input.pdb
```

Rules:

- Copy the target identifier exactly from the user's request.
- One job has one `source` root.
- For Boltz-2 generated structures, create/register the output as the job's
  source before continuing into preparation.
- Run `inspect_molecules` after acquisition when chains, ligands, metals,
  glycans, nucleic acids, or PTMs may affect choices.
