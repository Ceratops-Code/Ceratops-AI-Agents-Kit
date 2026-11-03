# Review Action

## Goal

Find consequential disagreements between a design document, implementation,
and declared sources of truth. Report evidence and the smallest credible fix.

## Inputs

Resolve repository, design document, comparison scope, and intended baseline.
Read `contracts/design-document-contract.md` and the Mechanical Checks section
below. Preserve the document, code, configuration, and Git state throughout
review.

## Workflow

1. Read governing instructions, the authoritative document, its references, and
   declared ownership and precedence. Distinguish implemented design, accepted
   future design, and proposals before judging contradictions. When ownership
   is unclear, report the exact competing artifacts and affected decision.
2. Map the contract topics to existing headings. Preserve project terminology.
   If the document lacks embedded machine metadata, pass `--mapping` with a
   caller-owned temporary JSON projection of its existing facts and headings.
   Mark missing facts as unverified; do not invent owners or authority. Missing
   mapping syntax alone is a mechanical limitation, not an architectural defect.
3. Run the mechanical helper and retain its bounded result. Continue semantic
   review even when a parser or mapping is unavailable; report precisely which
   checks could not run. Do not modify or install dependencies in the target
   repository during review. Remove owned temporary evidence when consumed.
4. Compare important claims with targeted code, configuration, tests, deployment,
   and normative documents. Inspect happy paths and material failure/recovery
   paths. Check view/prose agreement, data ownership, boundaries, integration
   contracts, migrations, quality claims, and decisions using the contract.
   Prefer existing behavior tests or structured configuration evidence for a
   disputed executable claim; source text alone does not establish behavior.
5. For each suspected inconsistency, try to disprove it using version, scope,
   environment, source precedence, and intended-versus-current distinctions.
   Confirm a finding only when the evidence demonstrates conflicting claims in
   the same relevant scope. Otherwise record the precise unverified claim and
   missing evidence, without inflating it into a confirmed inconsistency.
6. Rank confirmed findings by consequence using the rubric below. Name each
   conflicting artifact with exact path and line or section, explain the risk,
   and propose the smallest credible correction in its owning surface. Separate
   evidence-backed missing required content from speculative improvements.
   Do not add generic style advice, reformat the document, or implement fixes.

## Mechanical Checks

This helper-use section is shared by `create` and `review`. Its instructions
apply to both actions; the review workflow above applies only to `review`.

### Invocation

Run the examples from the installed skill directory. Read `python_runtime` from
the installed skill's `.runtime-manifest.json` and
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

### Mechanical Scope

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

### Maintenance

`references/contracts/design-document-contract.json` owns the content contract.
The `field_roles` declaration explicitly separates executable values from
annotations. Every allowed field is consumed by validation or resource
generation; semantic annotations are structure-checked but never used as a
mechanical claim of design quality. Unknown fields and duplicate IDs fail.

After an approved contract change, regenerate the human reference and template:

```text
uv run --no-project --python <python_runtime> python scripts/validate_design_document.py resources --output-root SKILL_ROOT
```

The generator owns the current readable contract under `references/contracts/`
and template under `assets/`, with no resource history or persistent checkpoints.
It prepares both resources from the contract and writes only changed content,
preserving existing line endings and restoring prior bytes after a failed write.
It does not change the contract or other skill files. Tests in
`tests/design_document_lifecycle/test_validator.py` cover CLI behavior,
preserved layouts, malformed inputs, dependencies, path boundaries, and
generated-resource agreement. Mermaid integration can use the optional
`DESIGN_DOCUMENT_MERMAID_CLI` test environment variable with an installed CLI.

Parser references: [markdown-it-py](https://markdown-it-py.readthedocs.io/en/latest/using.html)
and [official Mermaid CLI](https://github.com/mermaid-js/mermaid-cli).

## Severity and Finding Format

| Severity | Consequence |
| --- | --- |
| Critical | Credible immediate exposure to severe security compromise, irreversible data loss, or a system-wide operational failure. |
| High | A material architectural or contract mismatch can break an important workflow, security boundary, migration, or recovery promise. |
| Medium | A bounded inconsistency can mislead implementation or operations, or a required design decision is missing with a concrete risk. |
| Low | A precise, consequential reference or narrow factual defect with limited impact. |

For each confirmed finding, provide severity, claim, exact conflicting
artifacts, evidence, risk, and smallest fix. Use a separate unverified-coverage
list with topic, inspected scope, missing evidence, and next verification step.
Severity reflects consequence, not the number of missing headings.

## Completion Gate

Every reported inconsistency has scoped evidence, risk, and a credible owning
fix. Contract topics and required view decisions have been reviewed or marked
unverified. State which mechanical checks passed or could not run. A result
with no confirmed inconsistencies must still identify coverage limits and must
not imply exhaustive correctness. Target files and Git state remain unchanged.

## Output Contract

Return confirmed findings in severity order, then material unverified coverage
and the mechanical-check result. If none are confirmed, say so with the reviewed
scope. Write a persistent report only when requested.
