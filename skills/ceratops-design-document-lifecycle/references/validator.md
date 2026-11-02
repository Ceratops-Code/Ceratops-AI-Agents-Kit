# Design Document Validator

## Invocation

Read `python_runtime` from the installed skill's `.runtime-manifest.json` and
run the helper with that interpreter through uv. The declarations remain in
the skills source repository under `scripts`; deployment prepares
a versioned shared environment. Source development uses the repository's
`scripts/pyproject.toml`. The validator itself makes no model or network calls
and never edits a reviewed document.

```text
uv run --no-project --python <python_runtime> python scripts/validate_design_document.py validate --repo-root REPO --document docs/design.md --evidence-file EVIDENCE
```

`--document` is relative to the repository. Embedded metadata uses one fenced
`design-document` JSON block with `contract_version`, `document`, `owners`,
`source_of_truth`, `update_triggers`, and `coverage`. The template provides the
shape. Paths in `document` and `source_of_truth` are literal repository-relative
paths. Their existence checks catch renamed or stale declared filenames.
`owners` and `update_triggers` are nonempty string arrays. Each `source_of_truth`
entry has the shape `{"path": "src/app.py", "role": "implemented behavior"}`.
Keep entries in precedence order; each role explains the source's authority.

For an established document format, `--mapping FILE` accepts the same JSON as a
temporary projection of its existing prose. It cannot coexist with embedded
metadata. The caller owns that file and removes it after consuming the result;
never introduce a persistent mapping sidecar as a second source of truth.
Coverage maps each contract topic to a heading and a declared status:
`documented`, `unverified`, or `not_applicable`. The latter two require a reason;
only conditional topics allow `not_applicable`. Several topics may share a
heading. A declaration is not evidence that its semantic obligation is met.

## Mechanical Scope

The helper validates the contract's closed structure and field roles, metadata
fields, mapped section presence, nonempty documented sections, balanced fenced
code blocks, local Markdown links and heading anchors, declared paths, and
Mermaid parser results. It resolves paths inside the selected repository and
rejects escaping or private absolute paths. External links are not fetched.
Unlinked filenames in arbitrary prose, raw HTML links, semantic completeness,
diagram/prose agreement, and actual runtime behavior require agent review.

For Mermaid, supply an existing official Mermaid CLI using `--mermaid-cli PATH`
or put `mmdc` on PATH. A `.js` entry point is invoked with Node; no shell command
string is evaluated. The CLI and its browser are prerequisites; review does not
install them. `--temp-root DIR` is required when Mermaid occurs and must name
the caller's verified task temp root. The helper owns a unique directory below
it and removes generated input, configuration, and SVG files on every exit.
An unavailable CLI/browser or timeout is a blocked syntax check, not an invalid
diagram or a pass. Rendering uses strict security configuration. The helper
rejects diagram-level configuration overrides before invoking the renderer.

Exit `0` means mechanical checks passed, `1` means concrete mechanical defects,
and `2` means a required check was blocked or input was invalid. A document with
declared unverified topics can pass mechanics but returns their IDs. `OK` is
payload-free success only. Otherwise stdout gives bounded issues and counts;
`--evidence-file` writes the complete structured result to the caller-selected
path. The caller owns and removes that evidence after use. The helper refuses
to overwrite the document, mapping, contract, or any existing evidence file.

## Maintenance

`references/contracts/design-document-contract.json` owns the content contract.
The `field_roles` declaration explicitly separates executable values from
annotations. Every allowed field is consumed by validation or resource
generation; semantic annotations are structure-checked but never used as a
mechanical claim of design quality. Unknown fields and duplicate IDs fail.

After an approved contract change, regenerate the human reference and template:

```text
python scripts/validate_design_document.py resources --output-root SKILL_ROOT
```

The generator renders both resources from the validated contract before
replacing them. It does not change the contract or other skill files. Tests in
`tests/design_document_lifecycle/test_validator.py` cover CLI behavior,
preserved layouts, malformed inputs, dependencies, path boundaries, and
generated-resource agreement. Mermaid integration can use the optional
`DESIGN_DOCUMENT_MERMAID_CLI` test environment variable with an installed CLI.

Parser references: [markdown-it-py](https://markdown-it-py.readthedocs.io/en/latest/using.html)
and [official Mermaid CLI](https://github.com/mermaid-js/mermaid-cli).
