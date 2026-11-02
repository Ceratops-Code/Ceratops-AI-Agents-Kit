## Changes

- 

## Validation

- [ ] `npm --prefix scripts ci`
- [ ] `npm --prefix scripts run lint:markdown`
- [ ] `uv sync --project scripts --locked`
- [ ] `uv run --project scripts --locked python -m yamllint
  --config-file scripts/.yamllint.yml .`
- [ ] `uv run --project scripts --locked python -m mypy
  --config-file scripts/pyproject.toml`
- [ ] `uv run --project scripts --locked python scripts/validate-repository.py`
- [ ] `uv run --project scripts --locked python scripts/testing/run-tests.py --all`

## Release impact

- [ ] No release or external registry artifact required.
