"""Exercise document outcomes, parser boundaries, and generated resources.

pytest owns all temporary files through tmp_path. The optional real Mermaid
integration uses an explicitly supplied installed CLI; routine tests do not
download software or require a browser.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills/ceratops-design-document-lifecycle"
SCRIPT = SKILL / "scripts/validate_design_document.py"
SPEC = importlib.util.spec_from_file_location("design_document_validator", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def sample(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, dict[str, Any], dict[str, Any]]:
    root = tmp_path / "repository"
    (root / "docs").mkdir(parents=True)
    (root / "service.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    contract = VALIDATOR.load_contract()
    metadata = {
        "contract_version": 1, "document": "docs/design.md", "owners": ["Platform team"],
        "source_of_truth": [{"path": "service.py", "role": "current implementation"}],
        "update_triggers": ["Behavior or interface changes"],
        "coverage": {entry["id"]: {"heading": entry["heading"], "status": "documented", "reason": ""} for entry in contract["sections"]},
    }
    document = root / "docs/design.md"
    write_document(document, metadata)
    return root, document, contract, metadata


def write_document(document: pathlib.Path, metadata: dict[str, Any], *, suffix: str = "", embedded: bool = True) -> None:
    text = "# Example design\n\n"
    if embedded:
        text += "```design-document\n" + json.dumps(metadata) + "\n```\n\n"
    for heading in dict.fromkeys(entry["heading"] for entry in metadata["coverage"].values()):
        if heading:
            text += f"## {heading}\n\nEvidence-backed design account.\n\n"
    document.write_text(text + suffix, encoding="utf-8")


def codes(result: dict[str, Any]) -> set[str]:
    return {entry["code"] for entry in result["issues"]}


def invoke(root: pathlib.Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--repo-root", str(root), "--document", "docs/design.md", *arguments],
        capture_output=True, text=True, check=False,
    )


def test_cli_pass_preserves_document(tmp_path: pathlib.Path) -> None:
    root, document, _, _ = sample(tmp_path)
    before = document.read_bytes()
    result = invoke(root)
    assert result.returncode == 0, result.stdout
    assert result.stdout.strip() == "OK"
    assert document.read_bytes() == before


def test_existing_layout_uses_temporary_mapping_without_rewrite(tmp_path: pathlib.Path) -> None:
    root, document, _, metadata = sample(tmp_path)
    for entry in metadata["coverage"].values():
        entry["heading"] = "Project-specific overview"
    write_document(document, metadata, embedded=False)
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps(metadata), encoding="utf-8")
    before = document.read_bytes()
    result = invoke(root, "--mapping", str(mapping))
    assert result.returncode == 0, result.stdout
    assert document.read_bytes() == before


@pytest.mark.parametrize("field", ["contract_version", "document", "owners", "source_of_truth", "update_triggers", "coverage"])
def test_missing_contract_fields_are_detected(tmp_path: pathlib.Path, field: str) -> None:
    root, document, contract, metadata = sample(tmp_path)
    del metadata[field]
    original = document.read_text(encoding="utf-8")
    opening, _, rest = original.partition("```design-document\n")
    _, _, tail = rest.partition("\n```\n")
    document.write_text(opening + "```design-document\n" + json.dumps(metadata) + "\n```\n" + tail, encoding="utf-8")
    result = VALIDATOR.validate_document(root, document, contract)
    assert "missing-or-invalid-field" in codes(result)


def test_declared_unverified_is_not_silent_success(tmp_path: pathlib.Path) -> None:
    root, document, _, metadata = sample(tmp_path)
    metadata["coverage"]["quality"].update(status="unverified", reason="Load test unavailable")
    write_document(document, metadata)
    result = invoke(root)
    assert result.returncode == 0
    assert json.loads(result.stdout)["unverified"] == ["quality"]


def test_documented_topics_do_not_require_reasons(tmp_path: pathlib.Path) -> None:
    root, document, _, metadata = sample(tmp_path)
    for entry in metadata["coverage"].values():
        del entry["reason"]
    write_document(document, metadata)
    assert invoke(root).returncode == 0


def test_unverified_topics_require_explicit_reasons(tmp_path: pathlib.Path) -> None:
    root, document, contract, metadata = sample(tmp_path)
    metadata["coverage"]["quality"]["status"] = "unverified"
    del metadata["coverage"]["quality"]["reason"]
    write_document(document, metadata)
    assert "missing-or-invalid-field" in codes(VALIDATOR.validate_document(root, document, contract))


def test_only_conditional_topics_allow_reasoned_inapplicability(tmp_path: pathlib.Path) -> None:
    root, document, contract, metadata = sample(tmp_path)
    metadata["coverage"]["data"].update(heading=None, status="not_applicable", reason="Pure calculation without persistent state")
    write_document(document, metadata)
    assert VALIDATOR.validate_document(root, document, contract)["status"] == "mechanical-pass"
    metadata["coverage"]["purpose"].update(status="not_applicable", reason="No purpose recorded")
    write_document(document, metadata)
    assert "missing-or-invalid-field" in codes(VALIDATOR.validate_document(root, document, contract))


def test_unknown_coverage_and_empty_reasons_fail(tmp_path: pathlib.Path) -> None:
    root, document, contract, metadata = sample(tmp_path)
    metadata["coverage"]["data"].update(status="not_applicable", reason="")
    metadata["coverage"]["invented"] = {"heading": "Invented", "status": "documented", "reason": ""}
    write_document(document, metadata)
    assert "missing-or-invalid-field" in codes(VALIDATOR.validate_document(root, document, contract))


@pytest.mark.parametrize("suffix, expected", [
    ("```python\nprint(1)\n", {"unclosed-fence"}),
    ("~~~~python\nprint(1)\n~~~\n", {"unclosed-fence"}),
    ("````text\n```nested\n````\n", set()),
    ("~~~text\ntext\n~~~\n", set()),
    ("> ```text\n> text\n> ```\n", set()),
    ("```mermaid\n```\n", {"malformed-mermaid"}),
    ("```mermaid\n%%{init: {}}%%\ngraph TD\nA --> B\n```\n", {"mermaid-configuration"}),
])
def test_fence_and_mermaid_boundaries(tmp_path: pathlib.Path, suffix: str, expected: set[str]) -> None:
    root, document, contract, metadata = sample(tmp_path)
    write_document(document, metadata, suffix=suffix)
    assert codes(VALIDATOR.validate_document(root, document, contract)) == expected


def test_headings_in_code_do_not_satisfy_missing_sections(tmp_path: pathlib.Path) -> None:
    root, document, contract, _ = sample(tmp_path)
    text = document.read_text(encoding="utf-8").replace("## 1 Purpose and goals", "```text\n## 1 Purpose and goals\n```")
    document.write_text(text, encoding="utf-8")
    assert "missing-section" in codes(VALIDATOR.validate_document(root, document, contract))


def test_empty_and_ambiguous_documented_sections_fail(tmp_path: pathlib.Path) -> None:
    root, document, contract, _ = sample(tmp_path)
    text = document.read_text(encoding="utf-8").replace("## 1 Purpose and goals\n\nEvidence-backed design account.", "## 1 Purpose and goals\n\n<!-- comment only -->")
    document.write_text(text + "\n## 2 Constraints and assumptions\n\nDuplicate.\n", encoding="utf-8")
    assert {"empty-section", "ambiguous-section"} <= codes(VALIDATOR.validate_document(root, document, contract))


@pytest.mark.parametrize("link", ["../missing.py", "../../escape.md", "#unknown-anchor", "../service.py/child", "C:/private.md"])
def test_invalid_local_links_are_detected(tmp_path: pathlib.Path, link: str) -> None:
    root, document, contract, metadata = sample(tmp_path)
    write_document(document, metadata, suffix=f"[source]({link})\n")
    assert "invalid-link" in codes(VALIDATOR.validate_document(root, document, contract))


def test_valid_links_reference_links_and_duplicate_heading_anchors(tmp_path: pathlib.Path) -> None:
    root, document, contract, metadata = sample(tmp_path)
    (root / "docs/other doc.md").write_text("# Other\n\n## Repeat\n\nA\n\n## Repeat\n\nB\n", encoding="utf-8")
    write_document(document, metadata, suffix="[other](other%20doc.md#repeat-1)\n\n[code][source]\n\n[source]: ../service.py\n\n[remote](https://example.invalid/missing)\n")
    assert VALIDATOR.validate_document(root, document, contract)["status"] == "mechanical-pass"


@pytest.mark.parametrize("declaration", ["deleted.py", "../outside.py", "C:/private.py", "docs\\old.md"])
def test_stale_and_escaping_declared_paths(tmp_path: pathlib.Path, declaration: str) -> None:
    root, document, contract, metadata = sample(tmp_path)
    metadata["source_of_truth"][0]["path"] = declaration
    write_document(document, metadata)
    assert "invalid-declared-path" in codes(VALIDATOR.validate_document(root, document, contract))


def test_stale_document_identity_detected_even_when_old_file_exists(tmp_path: pathlib.Path) -> None:
    root, document, contract, metadata = sample(tmp_path)
    (root / "docs/old.md").write_text("Old design", encoding="utf-8")
    metadata["document"] = "docs/old.md"
    write_document(document, metadata)
    assert "invalid-declared-path" in codes(VALIDATOR.validate_document(root, document, contract))


def test_metadata_cannot_have_two_authorities_or_duplicate_keys(tmp_path: pathlib.Path) -> None:
    root, document, contract, metadata = sample(tmp_path)
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps(metadata), encoding="utf-8")
    assert "invalid-metadata" in codes(VALIDATOR.validate_document(root, document, contract, mapping=mapping))
    document.write_text(document.read_text(encoding="utf-8").replace('"contract_version": 1', '"contract_version": 1, "contract_version": 2'), encoding="utf-8")
    assert "invalid-metadata" in codes(VALIDATOR.validate_document(root, document, contract))


def test_missing_mermaid_renderer_is_blocked(tmp_path: pathlib.Path) -> None:
    root, document, contract, metadata = sample(tmp_path)
    write_document(document, metadata, suffix="```mermaid\nflowchart LR\n A --> B\n```\n")
    result = VALIDATOR.validate_document(root, document, contract, mermaid_cli=str(tmp_path / "absent"), temp_root=tmp_path)
    assert result["status"] == "blocked"
    assert "mermaid-unverified" in codes(result)


@pytest.mark.parametrize("mode, expected", [("valid", "mechanical-pass"), ("syntax", "failed"), ("browser", "blocked"), ("timeout", "blocked"), ("empty", "blocked")])
def test_renderer_outcomes_and_cleanup(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, mode: str, expected: str) -> None:
    root, document, contract, metadata = sample(tmp_path)
    write_document(document, metadata, suffix="```mermaid\nflowchart LR\n A --> B\n```\n")
    renderer_temp = tmp_path / "renderer"
    renderer_temp.mkdir()
    monkeypatch.setattr(VALIDATOR, "mermaid_command", lambda cli: ["existing-renderer"])

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["shell"] is False
        config = pathlib.Path(command[command.index("-c") + 1])
        assert json.loads(config.read_text(encoding="utf-8"))["securityLevel"] == "strict"
        if mode == "timeout":
            raise subprocess.TimeoutExpired(command, 60)
        if mode == "valid":
            pathlib.Path(command[command.index("-o") + 1]).write_text("<svg></svg>", encoding="utf-8")
        error = "Parse error on line 2" if mode == "syntax" else "Could not find Chrome" if mode == "browser" else ""
        return subprocess.CompletedProcess(command, 1 if error else 0, "", error)

    monkeypatch.setattr(VALIDATOR.subprocess, "run", run)
    result = VALIDATOR.validate_document(root, document, contract, temp_root=renderer_temp)
    assert result["status"] == expected
    assert list(renderer_temp.iterdir()) == []


def test_evidence_is_complete_and_never_overwrites_inputs(tmp_path: pathlib.Path) -> None:
    root, document, _, _ = sample(tmp_path)
    before = document.read_bytes()
    assert invoke(root, "--evidence-file", str(document)).returncode == 2
    assert document.read_bytes() == before
    evidence = tmp_path / "result.json"
    assert invoke(root, "--evidence-file", str(evidence)).returncode == 0
    assert json.loads(evidence.read_text(encoding="utf-8"))["status"] == "mechanical-pass"
    original_evidence = evidence.read_bytes()
    assert invoke(root, "--evidence-file", str(evidence)).returncode == 2
    assert evidence.read_bytes() == original_evidence


@pytest.mark.parametrize("mutation", ["field", "role", "type", "duplicate", "view"])
def test_contract_structure_and_field_consumers(tmp_path: pathlib.Path, mutation: str) -> None:
    contract = copy.deepcopy(VALIDATOR.load_contract())
    if mutation == "field":
        contract["unused_behavior"] = True
    elif mutation == "role":
        contract["field_roles"]["executable"].append("views")
    elif mutation == "type":
        contract["sections"][0]["required"] = "yes"
    elif mutation == "duplicate":
        contract["sections"].append(contract["sections"][0])
    else:
        contract["views"].pop()
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(ValueError):
        VALIDATOR.load_contract(path)


def test_contract_covers_current_c4_view_and_notation_sources() -> None:
    contract = VALIDATOR.load_contract()

    assert {view["id"] for view in contract["views"]} == {
        "system_landscape",
        "context",
        "container",
        "component",
        "code",
        "deployment",
        "dynamic",
    }
    assert contract["annotations"]["c4_defaults"].startswith(
        "Treat system context, container, and deployment views"
    )
    assert "Use a labelled, unidirectional line for each C4 relationship." in contract["annotations"]["diagrams"]
    source_urls = {source["url"] for source in contract["source_files"]}
    assert {
        "https://c4model.com/diagrams/notation",
        "https://c4model.com/diagrams/system-landscape",
        "https://c4model.com/diagrams/code",
    } <= source_urls
    writing_sources = {
        "https://developers.google.com/tech-writing/one/documents",
        "https://diataxis.fr/explanation/",
        "https://www.writethedocs.org/guide/writing/beginners-guide-to-docs/",
    }
    assert writing_sources <= source_urls
    assert all("not an architecture standard" in source["note"]
               for source in contract["source_files"] if source["url"] in writing_sources)


def test_generated_resources_match_contract_and_seed_requires_evidence(tmp_path: pathlib.Path) -> None:
    contract = VALIDATOR.load_contract()
    expected = VALIDATOR.render_resources(contract)
    VALIDATOR.generate_resources(contract, tmp_path / "generated")
    for relative, content in expected.items():
        assert (SKILL / relative).read_text(encoding="utf-8") == content
        assert (tmp_path / "generated" / relative).read_text(encoding="utf-8") == content
    root = tmp_path / "seed-repo"
    (root / "docs").mkdir(parents=True)
    document = root / "docs/design.md"
    document.write_text(expected["assets/design-document-template.md"], encoding="utf-8")
    assert invoke(root).returncode == 1


@pytest.mark.parametrize("preexisting", [False, True])
@pytest.mark.parametrize("failure_index", [0, 1])
def test_resource_generation_restores_partial_writes(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, preexisting: bool, failure_index: int) -> None:
    contract = VALIDATOR.load_contract()
    targets = [tmp_path / name for name in VALIDATOR.render_resources(contract)]
    originals = {path: b"Previous resource\r\n" if preexisting else None for path in targets}
    for path, content in originals.items():
        if content is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    write_text = pathlib.Path.write_text

    def partial_write(path: pathlib.Path, data: str, **kwargs: Any) -> int:
        if path == targets[failure_index]:
            path.write_bytes(b"Partial replacement")
            raise OSError("Simulated interrupted write")
        return write_text(path, data, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", partial_write)
    with pytest.raises(OSError, match="Simulated interrupted write"):
        VALIDATOR.generate_resources(contract, tmp_path)
    for path, content in originals.items():
        assert path.read_bytes() == content if content is not None else not path.exists()


@pytest.mark.skipif(not os.environ.get("DESIGN_DOCUMENT_MERMAID_CLI"), reason="Optional real Mermaid CLI/browser not supplied")
def test_real_mermaid_parser_accepts_and_rejects_syntax(tmp_path: pathlib.Path) -> None:
    root, document, contract, metadata = sample(tmp_path)
    renderer_temp = tmp_path / "renderer"
    renderer_temp.mkdir()
    cli = os.environ["DESIGN_DOCUMENT_MERMAID_CLI"]
    for source, expected in [("flowchart LR\n A --> B\n", "mechanical-pass"), ("flowchart LR\n A --> [broken\n", "failed")]:
        write_document(document, metadata, suffix=f"```mermaid\n{source}```\n")
        result = VALIDATOR.validate_document(root, document, contract, mermaid_cli=cli, temp_root=renderer_temp)
        assert result["status"] == expected, result
        assert list(renderer_temp.iterdir()) == []
