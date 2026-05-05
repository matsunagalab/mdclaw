# Testing

Run checks through the conda environment unless a task explicitly says
otherwise:

```bash
conda run -n mdclaw ruff check mdclaw/
conda run -n mdclaw pytest tests/test_mcp_server.py tests/test_cli.py tests/test_guardrails.py tests/test_slurm_server.py -v
```

## Test Levels

- Level 1: fast unit tests with no external scientific dependencies.
- Level 2: server smoke tests; requires the full scientific environment.
- Level 3: pipeline tests; may require network, `tleap`, OpenMM, or optional data.
- Level 4: manual agent workflow checks.

## Common Commands

```bash
# Level 1
conda run -n mdclaw pytest tests/test_mcp_server.py tests/test_cli.py -v

# Level 1 plus existing non-slow tests
conda run -n mdclaw pytest tests/ -v -m "not slow and not integration"

# Level 2
conda run -n mdclaw pytest tests/test_server_smoke.py -v

# Level 3: production continuation DAG
conda run -n mdclaw pytest tests/test_pipeline_prod_continue_dag.py -v

# Level 3: standard nucleic acid topology DAGs
conda run -n mdclaw pytest tests/test_pipeline_nucleic_dag.py -v

# Level 3: modified nucleic acid DAG
conda run -n mdclaw pytest tests/test_pipeline_modxna_dag.py -v

# All tests
conda run -n mdclaw pytest tests/ -v
```

Markers are configured in `pyproject.toml`:

- `slow`: Level 2 and higher.
- `integration`: Level 3 pipeline tests.

## Test Patterns

- Tool functions are called directly, e.g. `tool_name(param=value)`.
- Shared fixtures such as `small_pdb` and `alanine_dipeptide_pdb` live in
  `tests/conftest.py`.
- Pipeline tests use class attributes to pass state between ordered steps.
- Shared DAG helpers live in `tests/pipeline_helpers.py`.
