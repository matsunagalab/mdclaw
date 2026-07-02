# Structure Acquisition

Use a `source` node for the initial structural source bundle.

Common sources:

```bash
mdclaw create_node --job-dir <job_dir> --node-type source
mdclaw --job-dir <job_dir> --node-id <source_node_id> fetch_structure --source pdb --pdb-id 1AKE
mdclaw --job-dir <job_dir> --node-id <source_node_id> fetch_structure --source pdb --pdb-id 1AKE --assembly-ids 1
mdclaw --job-dir <job_dir> --node-id <source_node_id> fetch_structure --source alphafold --uniprot-id P12345
mdclaw --job-dir <job_dir> --node-id <source_node_id> fetch_structure --source local --file-path /abs/input.pdb
mdclaw --job-dir <job_dir> --node-id <source_node_id> modeller_from_alignment --template-pdb /abs/template.pdb --target-sequence MVLSPADK...
```

Rules:

- Use the exact `node_id` returned by `create_node`; do not leave `--node-id`
  empty and do not create a second `source` node for the same job.
- Copy the target identifier exactly from the user's request.
- Default PDB/local fetch records the deposited asymmetric unit only. If the
  user asks for a biological assembly, or the PDB/mmCIF entry says a specific
  assembly is the intended biological unit, request it during `fetch_structure`
  with `--assembly-ids <id...>` (for example `--assembly-ids 1`) or
  `--assembly-mode preferred|all`.
- One job has one `source` node. That node may contain multiple structures:
  NMR models, assembly/chain candidates, or generated ensemble members.
- The source tool normalizes executable candidates under
  `artifacts/candidates/` and records their provenance in `source_bundle.json`.
- Gemmi-generated assembly candidates carry `origin.assembly_id`, source
  chain/subchain/operator metadata, output chain names, and the copied-chain
  naming policy. Run `list_source_candidates` after fetch and select the
  intended assembly explicitly during `prepare_complex`.
- Use `list_source_candidates` to show the selectable candidates, ranks,
  files, and any generator metrics such as Boltz confidence.
- If the source bundle contains multiple structures, pass an explicit
  `prepare_complex` selector such as `--source-candidate-id <candidate_id>`.
- For Boltz-2 generated structures, create/register the output as the job's
  source bundle before continuing into preparation; the candidate metadata
  should carry Boltz rank, model index, confidence file, and confidence score
  when available.
- For MODELLER comparative models, use `skills/modeller-predict/SKILL.md` and
  register the selected model on the same `source` node before preparation.
- Run `inspect_molecules` after acquisition when chains, ligands, metals,
  glycans, nucleic acids, or PTMs may affect choices. Pass the same
  `--source-candidate-id` selector to inspect a specific candidate.

```bash
mdclaw list_source_candidates --job-dir <job_dir> --node-id <source_node_id>
mdclaw inspect_molecules --job-dir <job_dir> --node-id <source_node_id> --source-candidate-id <candidate_id>
```
