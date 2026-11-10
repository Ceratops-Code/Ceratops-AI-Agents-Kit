# Contributing

Contributions should keep skills practical, current, and safe.

## Rules

- Keep each skill self-contained under `skills/<skill-name>/`.
- Keep source `SKILL.md` files as portable delta-only skill definitions; runtime
  `SKILL.md` files are generated during install.
- Keep `agents/openai.yaml` aligned with the skill when changing trigger
  behavior.
- Keep reusable automation-run policy in `skills/ceratops-automation-run/`
  instead of duplicating the same alert, memory, and completion rules across
  automation prompts.
- Keep live shared Ceratops rules in `skills/sections/` and assignments in
  `skills/skill-sections.json`; keep the universal `core` section focused,
  keep GH-only wording in GH-only sections, keep GH org/repo/PR/code/artifact
  contract review in
  `skills/ceratops-repo-lifecycle/references/repo-contracts-review.md`, keep
  repository skill consistency and contract compliance in
  `skills/ceratops-skill-lifecycle/references/skills-consistency-review.md`, and
  keep skill-design standards refresh in
  `skills/ceratops-skill-lifecycle/references/skills-contract-review.md`.
- Keep live deployment and release operations in `sdlc/sdlc.yml` as structured
  argv steps. Keep reusable section and SDLC templates under
  `skills/ceratops-repo-lifecycle/references/templates/`; neither is live
  repository configuration.
- Do not add secrets, private endpoints, local machine paths, or org-internal
  procedures.
- Prefer current official docs over memory when changing GitHub, registry, or
  agent behavior, and use installed OpenAI skills only as local pattern examples
  for skill-design review.
- Add checklist items only when they are durable and broadly useful.
- Do not add boilerplate that is not relevant to these workflows.

## Validation

Run before opening a pull request:

```powershell
npm --prefix scripts ci
uv sync --project scripts --locked
npm --prefix scripts run lint:markdown
uv run --project scripts --locked python scripts/run-actionlint.py
uv run --project scripts --locked python -m yamllint --config-file scripts/.yamllint.yml . skills/ceratops-repo-lifecycle/references/templates/sdlc.yml.tmpl
uv run --project scripts --locked python -m ruff check --config scripts/pyproject.toml scripts mcp-servers skills/ceratops-repo-lifecycle/references/templates/deploy-skills.py.tmpl
uv run --project scripts --locked python -m mypy --config-file scripts/pyproject.toml --platform linux
uv run --project scripts --locked python -m mypy --config-file scripts/pyproject.toml --platform win32
uv run --project scripts --locked python scripts/testing/run-tests.py --all
```

CI runs these source checks and the separate test group from `sdlc/sdlc.yml`.
The test suite needs `rg` (ripgrep) on `PATH`.

Run full skill-source validation separately when skill source, metadata, shared
sections, runtime inputs, or skill contracts change:

```powershell
uv run --project scripts --locked python .\skills\ceratops-skill-lifecycle\scripts\skills-consistency-source-validator.py --mode full
```

If the change affects workflow behavior, include a short test note in the PR
explaining how the skill was exercised or reviewed.
For targeted skill work, run `python
.\skills\ceratops-skill-lifecycle\scripts\skills-consistency-source-validator.py
--mode sections` only when shared section source files or
`skills/skill-sections.json` changed. Run the touched package or repository
lifecycle helper command with `--help` from
`skills/ceratops-repo-lifecycle/scripts` when its code or related skill claims
changed.
The section mode validates section assignments and rejects stale source files
that still contain generated runtime blocks.
`skills/ceratops-skill-lifecycle/scripts/runtime/managed_runtime_builder.py`
composes runtime `SKILL.md` files and copies declared payloads during install.
