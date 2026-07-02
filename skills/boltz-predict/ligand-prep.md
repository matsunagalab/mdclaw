# Ligand SMILES Preparation

Protein-ligand mode only. Always resolve and validate SMILES before prediction.

If the user gives a chemical name (e.g. "aspirin"):

```bash
mdclaw pubchem_get_smiles_from_name --chemical-name "aspirin"
```

If it returns `success: True`, use the returned SMILES. If it fails, ask the
user for the SMILES directly or to check the compound name spelling.

If the user gives SMILES directly, validate it:

```bash
mdclaw rdkit_validate_smiles --smiles "CCO"
```

If validation fails, show the error and ask for a correction.
