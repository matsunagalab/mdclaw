# Testing

Run checks through the conda environment unless a task explicitly says
otherwise:

```bash
conda run -n mdclaw ruff check mdclaw/
conda run -n mdclaw pytest tests/test_registry.py tests/test_cli.py tests/test_guardrails.py tests/test_slurm_server.py -v
```

## Test Levels

- Level 1: fast unit tests with no external scientific dependencies.
- Level 2: server smoke tests; requires the full scientific environment.
- Level 3: pipeline tests; may require network, OpenMM, the
  `openmmforcefields` + `openff-pablo` stack, AmberTools (for
  `cpptraj` / `pdb4amber`), or optional data.
- Level 4: manual agent workflow checks.

## Common Commands

```bash
# Level 1
conda run -n mdclaw pytest tests/test_registry.py tests/test_cli.py -v

# Level 1 plus existing non-slow tests
conda run -n mdclaw pytest tests/ -v -m "not slow and not integration"

# Level 2
conda run -n mdclaw pytest tests/test_server_smoke.py -v

# Level 3: production continuation DAG
conda run -n mdclaw pytest tests/test_pipeline_prod_continue_dag.py -v

# Level 3: standard nucleic acid topology DAGs
conda run -n mdclaw pytest tests/test_pipeline_nucleic_dag.py -v

# Level 3: legacy/experimental modified nucleic acid branch
conda run -n mdclaw pytest tests/test_pipeline_modxna_dag.py -v

# All tests
conda run -n mdclaw pytest tests/ -v
```

Markers are configured in `pyproject.toml`:

- `slow`: Level 2 and higher.
- `integration`: Level 3 pipeline tests.

## Test Patterns

### MDDB export schema compatibility

```bash
pixi run pytest tests/test_mddb_export.py -q
```

Every generated `inputs.yaml` in these tests is checked against a pinned copy
of the official MDDB schema, including effective metadata overrides on each
MD entry. The same test file runs in the standard CI matrix on macOS, Linux
x86-64, and Linux ARM64. Invalid project fields, reference metadata, and MD
overrides have explicit rejection tests.

Validation runs offline with the existing development dependencies. It does
not require installing the MDDB workflow or running ingestion. See the
[schema snapshot](../../tests/data/mddb_schema/README.md) for provenance,
limitations, and the manual update procedure.

### Shared conventions

- Tool functions are called directly, e.g. `tool_name(param=value)`.
- Shared fixtures such as `small_pdb` and `alanine_dipeptide_pdb` live in
  `tests/conftest.py`.
- Pipeline tests use class attributes to pass state between ordered steps.
- Shared DAG helpers live in `tests/pipeline_helpers.py`.
