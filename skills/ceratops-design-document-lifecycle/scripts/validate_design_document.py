#!/usr/bin/env python3
"""Validate design-document mechanics and generate contract-derived resources.

The JSON contract owns topic IDs, headings, applicability, and semantic notes.
Only structure, explicit declarations, local paths, and parser results establish
mechanical outcomes. No model, network request, dependency installation, or
document mutation occurs during validation. Optional Mermaid rendering uses an
existing CLI and a helper-owned TemporaryDirectory below a caller-selected root;
that directory is removed on success, failure, and exception. Caller-selected
mapping/evidence files remain the caller's cleanup responsibility.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import textwrap
from collections import Counter
from typing import Any
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from markdown_it import MarkdownIt

SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACT_PATH = SKILL_ROOT / "references/contracts/design-document-contract.json"
FIELD_ROLES = {
    "executable": [
        "contract_format_version", "kind", "field_roles", "sections.id",
        "sections.heading", "sections.group", "sections.required",
    ],
    "annotation_only": [
        "name", "captured_on", "annotations", "sections.content", "views",
        "source_files",
    ],
}
TEXT = {"type": "string", "minLength": 1, "pattern": r"\S"}
VIEW_IDS = {
    "system_landscape",
    "context",
    "container",
    "component",
    "code",
    "deployment",
    "dynamic",
}


def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    """Close each public data object so misspelled declarations cannot be ignored."""
    return {
        "type": "object", "properties": properties,
        "required": list(properties), "additionalProperties": False,
    }


def array_schema(items: dict[str, Any], minimum: int = 1) -> dict[str, Any]:
    return {"type": "array", "items": items, "minItems": minimum}


CONTRACT_SCHEMA = object_schema({
    "contract_format_version": {"const": 1},
    "kind": {"const": "design_document_contract"},
    "name": TEXT,
    "captured_on": {"type": "string", "format": "date"},
    "field_roles": {"const": FIELD_ROLES},
    "annotations": object_schema(dict.fromkeys(
        (
            "organization",
            "completeness",
            "diagrams",
            "c4_defaults",
            "limits",
            "references",
        ),
        TEXT,
    )),
    "sections": array_schema(object_schema({
        "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
        "heading": TEXT,
        "group": {"anyOf": [TEXT, {"type": "null"}]},
        "required": {"type": "boolean"},
        "content": TEXT,
    })),
    "views": array_schema(object_schema({
        "id": {"enum": sorted(VIEW_IDS)}, "title": TEXT,
        "required_when": TEXT, "optional_when": TEXT, "unnecessary_when": TEXT,
    })),
    "source_files": array_schema(object_schema({
        "title": TEXT,
        "url": {"type": "string", "format": "uri", "pattern": "^https://"},
        "note": TEXT,
    })),
})


def read_json(path: pathlib.Path) -> Any:
    """Reject duplicate keys rather than silently accepting the last authority."""
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_keys)


def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def schema_errors(value: Any, schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [
        f"{'/'.join(map(str, error.absolute_path)) or '$'}: {error.message}"
        for error in validator.iter_errors(value)
    ]


def load_contract(path: pathlib.Path = CONTRACT_PATH) -> dict[str, Any]:
    """Validate all fields and annotation structure before using contract values."""
    contract = read_json(path)
    errors = schema_errors(contract, CONTRACT_SCHEMA)
    if errors:
        raise ValueError("invalid contract: " + "; ".join(errors[:3]))
    for collection, key in (("sections", "id"), ("sections", "heading"), ("views", "id")):
        values = [entry[key] for entry in contract[collection]]
        if len(values) != len(set(values)):
            raise ValueError(f"duplicate contract {collection}.{key}")
    if {view["id"] for view in contract["views"]} != VIEW_IDS:
        raise ValueError("contract must account for all seven C4 view decisions")
    return contract


def metadata_schema(contract: dict[str, Any]) -> dict[str, Any]:
    coverage: dict[str, Any] = {}
    for section in contract["sections"]:
        statuses = ["documented", "unverified"]
        if not section["required"]:
            statuses.append("not_applicable")
        entry = object_schema({
            "heading": {"anyOf": [TEXT, {"type": "null"}]},
            "status": {"enum": statuses},
            "reason": {"type": "string"},
        })
        entry["required"] = ["heading", "status"]
        entry["allOf"] = [
            {"if": {"properties": {"status": {"const": "documented"}}},
             "then": {"properties": {"heading": TEXT}},
             "else": {"properties": {"reason": TEXT}, "required": ["reason"]}},
        ]
        coverage[section["id"]] = entry
    return object_schema({
        "contract_version": {"const": contract["contract_format_version"]},
        "document": TEXT,
        "owners": array_schema(TEXT),
        "source_of_truth": array_schema(object_schema({"path": TEXT, "role": TEXT})),
        "update_triggers": array_schema(TEXT),
        "coverage": object_schema(coverage),
    })


def issue(code: str, message: str, line: int | None = None, *, blocked: bool = False) -> dict[str, Any]:
    return {"code": code, "message": message, "line": line, "blocked": blocked}


def local_path(root: pathlib.Path, value: str, *, base: pathlib.Path | None = None) -> pathlib.Path:
    """Resolve literal portable paths without reading through a repository escape."""
    if (not value.strip() or "\x00" in value or "\\" in value
            or pathlib.PureWindowsPath(value).drive or value.startswith("/")):
        raise ValueError(f"not a portable repository-relative path: {value}")
    target = ((base or root) / value).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"path escapes selected repository: {value}")
    if not target.exists():
        raise ValueError(f"missing or stale path: {value}")
    return target


def inline_text(token: Any) -> str:
    children = token.children or []
    return "".join(child.content for child in children if child.type in {"text", "code_inline", "image"})


def normalized_heading(text: str) -> str:
    return " ".join(text.casefold().split())


def parse_markdown(text: str) -> tuple[list[Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Use CommonMark tokens; supplement its permissive unclosed-fence handling."""
    tokens = MarkdownIt("commonmark").enable("table").parse(text)
    lines = text.splitlines()
    headings: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    slug_counts: Counter[str] = Counter()
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            assert token.map is not None
            title = inline_text(tokens[index + 1])
            slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
            suffix = slug_counts[slug]
            slug_counts[slug] += 1
            headings.append({
                "text": title, "key": normalized_heading(title),
                "slug": f"{slug}-{suffix}" if suffix else slug,
                "line": token.map[0] + 1, "level": int(token.tag[1:]),
                "start": token.map[1],
            })
        if token.type == "fence":
            assert token.map is not None
            end = token.map[1]
            closing = re.sub(r"^\s*(?:>\s*)+", "", lines[end - 1]).strip()
            pattern = re.escape(token.markup[0]) + "{" + str(len(token.markup)) + ",}"
            if end <= token.map[0] + 1 or not re.fullmatch(pattern, closing):
                issues.append(issue("unclosed-fence", "Fence has no matching explicit closer", token.map[0] + 1))
    for index, heading in enumerate(headings):
        end = len(lines)
        for candidate in headings[index + 1:]:
            if candidate["level"] <= heading["level"]:
                end = candidate["line"] - 1
                break
        body = "\n".join(lines[heading["start"]:end])
        # Comments and subordinate headings alone cannot document a topic.
        body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
        heading["has_content"] = any(line.strip() and not re.match(r"^\s*#{1,6}\s", line) for line in body.splitlines())
    return tokens, headings, issues


def check_links(root: pathlib.Path, document: pathlib.Path, tokens: list[Any], headings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    anchors = {document: {entry["slug"] for entry in headings}}
    for token in tokens:
        for child in token.children or []:
            if child.type not in {"link_open", "image"}:
                continue
            href = child.attrGet("href" if child.type == "link_open" else "src") or ""
            line = token.map[0] + 1 if token.map else None
            try:
                parsed = urlsplit(href)
                if parsed.scheme in {"https", "http", "mailto"} or href.startswith("//"):
                    continue
                if parsed.scheme:
                    raise ValueError(f"unsupported or absolute local link: {href}")
                path = unquote(parsed.path)
                if not path:
                    target = document
                elif path.startswith("/"):
                    target = local_path(root, path.lstrip("/"))
                else:
                    target = local_path(root, path, base=document.parent)
                if parsed.fragment and target.suffix.lower() == ".md":
                    if target not in anchors:
                        _, target_headings, _ = parse_markdown(target.read_text(encoding="utf-8-sig"))
                        anchors[target] = {entry["slug"] for entry in target_headings}
                    if unquote(parsed.fragment) not in anchors[target]:
                        raise ValueError(f"missing Markdown heading anchor: {href}")
            except (OSError, UnicodeError, ValueError) as exc:
                issues.append(issue("invalid-link", str(exc), line))
    return issues


def mermaid_command(cli: str | None) -> list[str] | None:
    selected = cli or shutil.which("mmdc")
    if not selected:
        return None
    resolved = pathlib.Path(selected).resolve()
    if not resolved.is_file():
        return None
    if resolved.suffix.lower() in {".js", ".mjs"}:
        node = shutil.which("node")
        return [node, str(resolved)] if node else None
    return [str(resolved)]


def check_mermaid(tokens: list[Any], cli: str | None, temp_root: pathlib.Path | None) -> list[dict[str, Any]]:
    """Render only supplied Mermaid through its official parser, with no installs."""
    blocks = [token for token in tokens if token.type == "fence" and token.info.strip().lower() == "mermaid"]
    if not blocks:
        return []
    issues: list[dict[str, Any]] = []
    pending = []
    for block in blocks:
        line = block.map[0] + 1
        if not block.content.strip():
            issues.append(issue("malformed-mermaid", "Empty Mermaid block", line))
        elif re.search(r"%%\s*\{", block.content) or block.content.lstrip().startswith("---"):
            issues.append(issue("mermaid-configuration", "Diagram configuration overrides are outside this validator's strict rendering contract", line))
        else:
            pending.append(block)
    if not pending:
        return issues
    command = mermaid_command(cli)
    if not command or temp_root is None or not temp_root.is_dir():
        issues.append(issue("mermaid-unverified", "Mermaid requires an existing CLI/browser and --temp-root pointing to the verified task temp root", blocked=True))
        return issues
    # This is the only validation-created temporary directory. Its lifetime owns
    # every input, config, and rendered output, including partial failed output.
    with tempfile.TemporaryDirectory(prefix="design-mermaid-", dir=temp_root.resolve()) as folder:
        stage = pathlib.Path(folder)
        config = stage / "strict.json"
        config.write_text(json.dumps({"securityLevel": "strict"}), encoding="utf-8")
        for index, block in enumerate(pending):
            source = stage / f"diagram-{index}.mmd"
            output = stage / f"diagram-{index}.svg"
            source.write_text(block.content, encoding="utf-8")
            try:
                result = subprocess.run(
                    [*command, "-i", str(source), "-o", str(output), "-c", str(config), "-q"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=60, check=False, shell=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                issues.append(issue("mermaid-unverified", f"Renderer unavailable: {type(exc).__name__}", block.map[0] + 1, blocked=True))
                break
            if result.returncode:
                details = (result.stderr or result.stdout).strip()
                syntax_error = bool(re.search(r"parse error|syntax error|lexical error|unknowndiagramerror|no diagram type detected", details, re.IGNORECASE))
                issues.append(issue(
                    "malformed-mermaid" if syntax_error else "mermaid-unverified",
                    details[:2000] or f"Renderer exited {result.returncode}",
                    block.map[0] + 1, blocked=not syntax_error,
                ))
                if not syntax_error:
                    break
            elif not output.is_file() or not output.stat().st_size:
                issues.append(issue("mermaid-unverified", "Renderer returned no output", block.map[0] + 1, blocked=True))
                break
    return issues


def validate_document(
    root: pathlib.Path, document: pathlib.Path, contract: dict[str, Any], *,
    mapping: pathlib.Path | None = None, mermaid_cli: str | None = None,
    temp_root: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Return complete mechanical evidence; declared coverage stays separate."""
    tokens, headings, issues = parse_markdown(document.read_text(encoding="utf-8-sig"))
    issues.extend(check_links(root, document, tokens, headings))
    metadata_blocks = [token for token in tokens if token.type == "fence" and token.info.strip() == "design-document"]
    metadata = None
    try:
        if len(metadata_blocks) > 1 or (metadata_blocks and mapping is not None):
            raise ValueError("use one embedded metadata block or one external mapping, never both")
        if mapping is not None:
            metadata = read_json(mapping)
        elif metadata_blocks:
            metadata = json.loads(metadata_blocks[0].content, object_pairs_hook=unique_keys)
        else:
            raise ValueError("missing design-document metadata; map existing prose using --mapping when necessary")
        metadata_errors = schema_errors(metadata, metadata_schema(contract))
        if metadata_errors:
            for error in metadata_errors:
                issues.append(issue("missing-or-invalid-field", error))
            metadata = None
    except (OSError, UnicodeError, ValueError) as exc:
        issues.append(issue("invalid-metadata", str(exc)))
    unverified: list[str] = []
    if metadata is not None:
        for declaration in [metadata["document"], *(item["path"] for item in metadata["source_of_truth"])]:
            try:
                target = local_path(root, declaration)
                if declaration == metadata["document"] and target != document:
                    raise ValueError(f"declared document filename is stale: {declaration}")
            except (OSError, ValueError) as exc:
                issues.append(issue("invalid-declared-path", str(exc)))
        for section in contract["sections"]:
            topic = section["id"]
            entry = metadata["coverage"][topic]
            if entry["status"] == "unverified":
                unverified.append(topic)
            if entry["heading"] is None and entry["status"] == "not_applicable":
                continue
            matched = [heading for heading in headings if entry["heading"] is not None and heading["key"] == normalized_heading(entry["heading"])]
            if not matched:
                issues.append(issue("missing-section", f"{topic}: mapped heading is absent: {entry['heading']}"))
            elif len(matched) != 1:
                issues.append(issue("ambiguous-section", f"{topic}: mapped heading occurs more than once"))
            elif entry["status"] == "documented" and not matched[0]["has_content"]:
                issues.append(issue("empty-section", f"{topic}: documented section contains no body", matched[0]["line"]))
    else:
        unverified = [section["id"] for section in contract["sections"]]
    issues.extend(check_mermaid(tokens, mermaid_cli, temp_root))
    status = "blocked" if any(item["blocked"] for item in issues) else "failed" if issues else "mechanical-pass"
    return {"status": status, "document": str(document.relative_to(root)).replace("\\", "/"), "issues": issues, "unverified": unverified}


def render_resources(contract: dict[str, Any]) -> dict[str, str]:
    """Generate the readable projection and an intentionally incomplete seed."""
    reference = ["# Design Document Contract", "", "Generated from `contracts/design-document-contract.json`; edit that source.", ""]
    for paragraph in contract["annotations"].values():
        reference.extend([textwrap.fill(paragraph, width=78), ""])
    reference += ["## Content Coverage", "", "| Topic | Applicability | Content |", "| --- | --- | --- |"]
    for section in contract["sections"]:
        reference.append(f"| `{section['id']}` | {'Required' if section['required'] else 'Conditional'} | {section['content']} |")
    reference += ["", "## C4 View Selection", "", "| View | Required | Optional | Unnecessary |", "| --- | --- | --- | --- |"]
    for view in contract["views"]:
        reference.append(f"| {view['title']} | {view['required_when']} | {view['optional_when']} | {view['unnecessary_when']} |")
    reference += ["", "## Authoritative References", "", f"Captured {contract['captured_on']}. Recheck official sources for concrete", "standards ambiguities; do not perform a standards refresh on every invocation.", ""]
    for source in contract["source_files"]:
        reference += [f"- [{source['title']}]({source['url']}):", textwrap.fill(source["note"], width=76, initial_indent="  ", subsequent_indent="  ")]
    metadata = {
        "contract_version": contract["contract_format_version"], "document": "docs/design.md",
        "owners": [], "source_of_truth": [], "update_triggers": [],
        "coverage": {section["id"]: {"heading": section["heading"], "status": "unverified", "reason": "Awaiting repository evidence"} for section in contract["sections"]},
    }
    template = ["# Software Design", "", "Replace the empty declarations with evidence; this seed is not a completed", "design. Keep the declared source hierarchy in precedence order and distinguish", "current implementation from approved intent in each relevant section.", "", "```design-document"]
    # Compact entries keep the seed readable without duplicating its contract.
    serialized = json.dumps(metadata, ensure_ascii=False, indent=2)
    serialized = re.sub(r'\{\n\s+"heading":.*?\n\s+\}', lambda match: json.dumps(json.loads(match.group()), ensure_ascii=False), serialized, flags=re.DOTALL)
    template += [serialized, "```", ""]
    groups: set[str] = set()
    for section in contract["sections"]:
        group = section["group"]
        if group and group not in groups:
            template += [f"## {group}", ""]
            groups.add(group)
        template += [f"{'###' if group else '##'} {section['heading']}", "", textwrap.fill(section["content"], width=78), ""]
    return {
        "references/design-document-contract.md": "\n".join(reference).rstrip() + "\n",
        "assets/design-document-template.md": "\n".join(template).rstrip() + "\n",
    }


def generate_resources(contract: dict[str, Any], output_root: pathlib.Path) -> None:
    """Prepare both projections before replacement; restore prior bytes on error."""
    generated = render_resources(contract)
    output_root = output_root.resolve()
    targets = {output_root / name: text for name, text in generated.items()}
    originals: dict[pathlib.Path, bytes | None] = {}
    for path in targets:
        if not path.resolve().is_relative_to(output_root):
            raise ValueError("generated resource target escapes output root")
        originals[path] = path.read_bytes() if path.exists() else None
    written: list[pathlib.Path] = []
    try:
        for path, text in targets.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            previous = originals[path]
            newline = "\r\n" if previous and b"\r\n" in previous else "\n"
            # A failed write can already have truncated or partially created its
            # target, so register it for rollback before attempting replacement.
            written.append(path)
            path.write_text(text, encoding="utf-8", newline=newline)
    except OSError:
        for path in reversed(written):
            previous = originals[path]
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    resources = actions.add_parser("resources", help="Generate the readable contract and seed template")
    resources.add_argument("--output-root", required=True, type=pathlib.Path)
    validate = actions.add_parser("validate", help="Check one document without modifying it")
    validate.add_argument("--repo-root", required=True, type=pathlib.Path)
    validate.add_argument("--document", required=True)
    validate.add_argument("--mapping", type=pathlib.Path)
    validate.add_argument("--mermaid-cli")
    validate.add_argument("--temp-root", type=pathlib.Path)
    validate.add_argument("--evidence-file", type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        contract = load_contract()
        if args.action == "resources":
            generate_resources(contract, args.output_root)
            print("OK")
            return 0
        root = args.repo_root.resolve(strict=True)
        document = local_path(root, args.document)
        if not document.is_file():
            raise ValueError("document must be a file")
        if args.evidence_file:
            evidence = args.evidence_file.resolve()
            protected = {document, CONTRACT_PATH.resolve()}
            if args.mapping:
                protected.add(args.mapping.resolve())
            if evidence in protected or evidence.exists():
                raise ValueError("evidence target must be new and cannot overwrite an input")
        result = validate_document(root, document, contract, mapping=args.mapping, mermaid_cli=args.mermaid_cli, temp_root=args.temp_root)
        if args.evidence_file:
            args.evidence_file.parent.mkdir(parents=True, exist_ok=True)
            with args.evidence_file.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(result, stream, indent=2)
                stream.write("\n")
        if result["status"] == "mechanical-pass" and not result["unverified"]:
            print("OK")
        else:
            compact = dict(result)
            compact["issue_count"] = len(result["issues"])
            compact["issues"] = [dict(item, message=item["message"][:500]) for item in result["issues"][:8]]
            if args.evidence_file:
                compact["evidence_file"] = str(args.evidence_file)
            print(json.dumps(compact, separators=(",", ":")))
        return {"mechanical-pass": 0, "failed": 1, "blocked": 2}[result["status"]]
    except (OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)[:1000]}, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
