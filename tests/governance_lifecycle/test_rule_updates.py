from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tomllib
from unittest import mock

import pytest

from tests.governance_lifecycle.support import (
    RULE_CANDIDATE_VALIDATOR,
    run_rule_candidate_validator,
    target_repository_markdown_policy,
    write_rule_candidate,
)


def test_rule_candidate_repairs_multiple_targets_and_is_idempotent(
    tmp_path: pathlib.Path,
) -> None:
    first_repo = tmp_path / "first-repo"
    second_repo = tmp_path / "second-repo"
    first_repo.mkdir()
    second_repo.mkdir()
    first = first_repo / "contract.md"
    second = second_repo / "contract.md"
    first_text = (
        "# First\n\n"
        "Old prose.\n\n"
        "- Old nested item.\n\n"
        "> > - Old quoted item.\n\n"
        "````text\n"
        "```\n"
        "protected code line that is intentionally much longer than the limit\n"
        "````\n\n"
        "A [sample link][sample].\n\n"
        "[sample]: https://example.test/reference\n"
        "  \"Old reference title.\"\n"
    )
    second_text = "# Second\n\nOld alpha.\n\nOld beta.\n"
    first.write_text(first_text, encoding="utf-8", newline="\n")
    second.write_text(second_text, encoding="utf-8", newline="\r\n")
    candidate = tmp_path / "candidate.json"
    evidence = tmp_path / "evidence.json"
    inline_fragments = (
        "`name`,",
        "(`call`)",
        "`module`.",
        "`one`/`two`;",
        "prefix`code`suffix,",
        "``two  words``:",
        "**`bold`**!",
        '[guide](https://example.test/guide "Guide title"),',
        "(<https://example.test/guide>).",
    )
    first_replacements = [
        {
            "expected_old": "Old prose.",
            "replacement": (
                "Safe ordinary prose wraps deterministically while preserving "
                "every original non-whitespace content character: "
                + " ".join(inline_fragments)
                + " Existing spaces remain in `left` / `right`."
            ),
        },
        {
            "expected_old": "- Old nested item.",
            "replacement": (
                "- Nested list continuation wrapping preserves its exact "
                "nesting and marker structure."
            ),
        },
        {
            "expected_old": "> > - Old quoted item.",
            "replacement": (
                "> > - Nested blockquote continuation wrapping preserves "
                "both quote depths and list nesting."
            ),
        },
        {
            "expected_old": (
                "````text\n"
                "```\n"
                "protected code line that is intentionally much longer than the limit\n"
                "````"
            ),
            "replacement": (
                "````text\n"
                "```\n"
                "protected code line that is intentionally much longer than the limit\n"
                "````"
            ),
        },
        {
            "expected_old": '  "Old reference title."',
            "replacement": (
                '  "A reference definition continuation remains byte-for-byte '
                'unwrapped under its surrounding Markdown context."'
            ),
        },
    ]
    second_replacements = [
        {
            "expected_old": "Old alpha.",
            "replacement": "Alpha stays on one line under its wider configured policy.",
        },
        {
            "expected_old": "Old beta.",
            "replacement": (
                "Beta remains independently replaceable and wraps with the "
                "second target's CRLF convention when it exceeds that policy."
            ),
        },
    ]
    write_rule_candidate(
        candidate,
        rule_stack=[first, second],
        targets=[
            {
                "rules": str(first.resolve()),
                "history": None,
                "source_sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
                "markdown_policy": None,
                "replacements": first_replacements,
            },
            {
                "rules": str(second.resolve()),
                "history": None,
                "source_sha256": hashlib.sha256(second.read_bytes()).hexdigest(),
                "markdown_policy": None,
                "replacements": second_replacements,
            },
        ],
    )
    original_sources = (first.read_bytes(), second.read_bytes())
    validated = run_rule_candidate_validator(candidate, evidence)
    assert validated.returncode == 0, validated.stderr
    assert validated.stdout.strip() == "OK"
    fixed = json.loads(candidate.read_text(encoding="utf-8"))
    fixed_first = fixed["targets"][0]["replacements"]
    fixed_second = fixed["targets"][1]["replacements"]
    assert "\n" in fixed_first[0]["replacement"]
    assert all(fragment in fixed_first[0]["replacement"] for fragment in inline_fragments)
    assert " ".join(fixed_first[0]["replacement"].split()) == " ".join(
        first_replacements[0]["replacement"].split()
    )
    assert "\n  " in fixed_first[1]["replacement"]
    assert "\n> >   " in fixed_first[2]["replacement"]
    assert fixed_first[3]["replacement"] == first_replacements[3]["replacement"]
    assert fixed_first[4]["replacement"] == first_replacements[4]["replacement"]
    assert "\n" not in fixed_second[0]["replacement"]
    assert "\r\n" in fixed_second[1]["replacement"]
    assert "\n" not in fixed_second[1]["replacement"].replace("\r\n", "")
    assert (first.read_bytes(), second.read_bytes()) == original_sources
    detail = json.loads(evidence.read_text(encoding="utf-8"))
    assert detail["status"] == "passed" and "idempotent" not in detail
    assert not fixed_first[0]["replacement"].startswith(" ")
    # The policy engine uses this source checkout's npm dependency, regardless
    # of target configuration. Verify the resolved command can validate Markdown.
    command = pathlib.Path(fixed["targets"][0]["markdown_policy"]["validate_command"][0])
    source_root = RULE_CANDIDATE_VALIDATOR.parents[3]
    assert command == (source_root / "scripts/node_modules/.bin" / ("markdownlint.cmd" if os.name == "nt" else "markdownlint")).resolve()
    first_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
    second_evidence = tmp_path / "second-evidence.json"
    repeated = run_rule_candidate_validator(candidate, second_evidence)
    assert repeated.returncode == 0, repeated.stderr
    assert hashlib.sha256(candidate.read_bytes()).hexdigest() == first_hash
    assert json.loads(second_evidence.read_text(encoding="utf-8"))["changed"] is False

    for name, newline, bom in (
        ("automation.toml", "\n", b""),
        ("automation.TOML", "\r\n", b"\xef\xbb\xbf"),
    ):
        target = first_repo / name
        prompt = 'Run "helper.py" with C:\\repo\\old-evidence\\snapshot.json.\n' + "word " * 60
        document = 'name = "Audit"\nprompt = ' + json.dumps(prompt) + '\nstatus = "ACTIVE"\n'
        original = bom + document.replace("\n", newline).encode("utf-8")
        target.write_bytes(original)
        toml_candidate = tmp_path / (name + "-candidate.json")
        toml_evidence = tmp_path / (name + "-evidence.json")
        write_rule_candidate(
            toml_candidate,
            rule_stack=[target],
            targets=[{
                "rules": str(target.resolve()),
                "history": None,
                "source_sha256": hashlib.sha256(original).hexdigest(),
                "markdown_policy": None,
                "replacements": [{
                    "expected_old": "old-evidence",
                    "replacement": "task-evidence",
                }],
            }],
        )
        # TOML-only validation must work with no Markdown executable on PATH.
        arguments = [sys.executable, str(RULE_CANDIDATE_VALIDATOR),
                     "--candidate", str(toml_candidate), "--evidence", str(toml_evidence)]
        environment = {**os.environ, "PATH": str(tmp_path / "no-tools")}
        validated = subprocess.run(arguments, env=environment, capture_output=True, text=True)
        assert validated.returncode == 0, validated.stderr
        result = json.loads(toml_candidate.read_text(encoding="utf-8"))
        assert result["targets"][0]["markdown_policy"] is None
        assert result["targets"][0]["replacements"] == [
            {"expected_old": "old-evidence", "replacement": "task-evidence"}
        ]
        detail = json.loads(toml_evidence.read_text(encoding="utf-8"))
        assert detail["targets"][0]["validation"]["format"] == "toml"
        assert detail["targets"][0]["fix"] is None
        assert target.read_bytes() == original
        prospective = document.replace("old-evidence", "task-evidence")
        parsed = tomllib.loads(prospective)
        assert parsed == {"name": "Audit", "prompt": prompt.replace("old-evidence", "task-evidence"), "status": "ACTIVE"}
        candidate_bytes = toml_candidate.read_bytes()
        checked = subprocess.run(arguments + ["--check-only"], env=environment, capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr
        assert toml_candidate.read_bytes() == candidate_bytes


@pytest.mark.parametrize("newline,bom", [("\n", b""), ("\r\n", b""), ("\r\n", b"\xef\xbb\xbf")])
@pytest.mark.parametrize("prefix", ["", "- ", "  - ", "> ", "> > - "])
def test_formatter_is_stable_and_preserves_surrounding_bytes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, newline: str, bom: bytes, prefix: str,
) -> None:
    monkeypatch.syspath_prepend(str(RULE_CANDIDATE_VALIDATOR.parent))
    import validate_rule_candidate as producer

    target = tmp_path / "contract.md"
    old = prefix + "Old prose."
    before = "# Contract" + newline * 2 + old + newline * 2 + "Untouched `code`." + newline
    original = bom + before.encode("utf-8")
    target.write_bytes(original)
    policy = producer.resolve_target_policy(None, target=target)
    assert policy is not None
    replacement = prefix + "Changed prose " + "with protected `two  words` and punctuation, " * 8
    repaired = producer._repair_fragment(replacement, newline=newline, policy=policy, target=target, replacement=0)
    assert repaired.startswith(prefix + "Changed")
    assert producer._repair_fragment(repaired, newline=newline, policy=policy, target=target, replacement=0) == repaired
    assert "`two  words`" in repaired
    source = producer.read_source(target, "target")
    prospective, _ = producer.construct_prospective(source, [{"expected_old": old, "replacement": repaired}])
    assert prospective == before.replace(old, repaired)
    assert target.read_bytes() == original
    assert "\n" not in repaired.replace(newline, "")


@pytest.mark.parametrize("marker", [">", "*", "1.", "#", "```code```"])
def test_wrapping_cannot_turn_prose_into_markdown_structure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, marker: str,
) -> None:
    monkeypatch.syspath_prepend(str(RULE_CANDIDATE_VALIDATOR.parent))
    import validate_rule_candidate as producer

    line = "Ordinary safe prose " + marker + " after a long sentence"
    assert producer._wrap_line(line, 20, target=tmp_path / "contract.md", replacement=0) == [line]


def test_new_candidate_invokes_each_external_validator_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(RULE_CANDIDATE_VALIDATOR.parent))
    import validate_rule_candidate as producer

    target = tmp_path / "contract.md"
    target.write_bytes(b"# Contract\n\nOld prose.\n")
    candidate = tmp_path / "candidate.json"
    write_rule_candidate(candidate, rule_stack=[target], targets=[{
        "rules": str(target), "history": None,
        "source_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "markdown_policy": None,
        "replacements": [{"expected_old": "Old prose.", "replacement": "New prose."}],
    }])
    with mock.patch.object(producer, "_run_policy_command", wraps=producer._run_policy_command) as external:
        producer.validate_rule_candidate(candidate, tmp_path / "evidence.json")
    assert external.call_count == 1
    assert target.read_bytes() == b"# Contract\n\nOld prose.\n"


@pytest.mark.parametrize("authorized", [False, True])
def test_generate_request_is_self_contained_and_honors_application_authority(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, authorized: bool,
) -> None:
    monkeypatch.syspath_prepend(str(RULE_CANDIDATE_VALIDATOR.parent))
    import apply_rules_update as application

    root = tmp_path / "task-temp"
    root.mkdir()
    target = tmp_path / "contract.md"
    target.write_bytes(b"# Contract\r\n\r\nOld prose.\r\n")
    candidate = tmp_path / "candidate.json"
    write_rule_candidate(candidate, rule_stack=[target], targets=[{
        "rules": str(target), "history": None,
        "source_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "markdown_policy": None,
        "replacements": [{"expected_old": "Old prose.", "replacement": "New prose."}],
    }])
    result = subprocess.run([
        sys.executable, str(RULE_CANDIDATE_VALIDATOR.with_name("proposal-workflow.py")),
        "generate-update-request", "--candidate", str(candidate), "--task-temp-root", str(root),
        *(["--mutation-authorized"] if authorized else []),
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    if authorized:
        assert result.stdout.strip() == "OK"
    else:
        assert json.loads(result.stdout)["status"] == "awaiting_approval"
        request_path = root / "update-request.json"
        assert set(root.iterdir()) == {request_path}
        assert b"Old prose." in target.read_bytes()
        frozen = json.loads(request_path.read_bytes())["accepted_candidate"]["acceptance"]
        candidate.unlink()
        # Neither the input file nor newer content checks are needed to apply.
        with mock.patch.object(application, "validate_rule_candidate", side_effect=AssertionError("revalidated")):
            application.apply_update_request(request_path)
        assert frozen["check_versions"]
    assert target.read_bytes() == b"# Contract\r\n\r\nNew prose.\r\n"
    assert not list(root.iterdir())


def test_application_failure_and_interrupted_cleanup_preserve_recovery(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(RULE_CANDIDATE_VALIDATOR.parent))
    import apply_rules_update as application

    root = tmp_path / "task-temp"
    root.mkdir()
    target = tmp_path / "contract.md"
    original = b"# Contract\n\nOld prose.\n"
    target.write_bytes(original)
    candidate = tmp_path / "candidate.json"
    write_rule_candidate(candidate, rule_stack=[target], targets=[{
        "rules": str(target), "history": None,
        "source_sha256": hashlib.sha256(original).hexdigest(), "markdown_policy": None,
        "replacements": [{"expected_old": "Old prose.", "replacement": "New prose."}],
    }])
    request_path = application.generate_candidate_update_request(candidate, root)
    frozen = request_path.read_bytes()
    target.write_bytes(original + b"\nAnother writer.\n")
    with pytest.raises(application.ApplicationError, match="source changed since acceptance"):
        application.apply_update_request(request_path)
    assert request_path.read_bytes() == frozen
    assert b"Another writer." in target.read_bytes()
    target.write_bytes(original)
    unlink = pathlib.Path.unlink

    def interrupted_cleanup(path, *args, **kwargs):
        if path == request_path:
            raise OSError("simulated cleanup interruption")
        return unlink(path, *args, **kwargs)

    with mock.patch.object(pathlib.Path, "unlink", interrupted_cleanup):
        with pytest.raises(OSError, match="cleanup interruption"):
            application.apply_update_request(request_path)
    expected = b"# Contract\n\nNew prose.\n"
    assert target.read_bytes() == expected and request_path.read_bytes() == frozen
    with (
        mock.patch.object(application, "validate_rule_candidate", side_effect=AssertionError("revalidated")),
        mock.patch.object(application.os, "replace", side_effect=AssertionError("rewrote completed update")),
    ):
        application.apply_update_request(request_path)
    assert target.read_bytes() == expected and not list(root.iterdir())


def test_rule_candidate_failures_are_atomic_and_actionable(
    tmp_path: pathlib.Path,
) -> None:
    safe_repo = tmp_path / "safe-repo"
    blocked_repo = tmp_path / "blocked-repo"
    safe_repo.mkdir()
    blocked_repo.mkdir()
    safe = safe_repo / "contract.md"
    blocked = blocked_repo / "contract.md"
    safe.write_text("Old safe.\n", encoding="utf-8", newline="\n")
    blocked.write_text("Old blocked.\n", encoding="utf-8", newline="\n")
    candidate = tmp_path / "atomic-candidate.json"
    evidence = tmp_path / "atomic-evidence.json"
    token = "https://example.test/" + "x" * 70
    write_rule_candidate(
        candidate,
        rule_stack=[safe, blocked],
        targets=[
            {
                "rules": str(safe.resolve()),
                "history": None,
                "source_sha256": hashlib.sha256(safe.read_bytes()).hexdigest(),
                "markdown_policy": None,
                "replacements": [
                    {
                        "expected_old": "Old safe.",
                        "replacement": (
                            "Safe prose would wrap if every target completed "
                            "mechanical validation."
                        ),
                    }
                ],
            },
            {
                "rules": str(blocked.resolve()),
                "history": None,
                "source_sha256": hashlib.sha256(blocked.read_bytes()).hexdigest(),
                "markdown_policy": None,
                "replacements": [
                    {"expected_old": "Old blocked.", "replacement": token}
                ],
            },
        ],
    )
    before = candidate.read_bytes()
    failed = run_rule_candidate_validator(candidate, evidence)
    assert failed.returncode == 1
    assert str(blocked.resolve()) in failed.stderr
    assert "replacement=0" in failed.stderr
    assert "configured limit 80" in failed.stderr
    assert "indivisible token" in failed.stderr
    assert candidate.read_bytes() == before
    assert json.loads(evidence.read_text(encoding="utf-8"))["status"] == "failed"

    target_policy = target_repository_markdown_policy(safe_repo)
    write_rule_candidate(
        candidate,
        rule_stack=[safe],
        targets=[
            {
                "rules": str(safe.resolve()),
                "history": None,
                "source_sha256": hashlib.sha256(safe.read_bytes()).hexdigest(),
                "markdown_policy": target_policy,
                "replacements": [
                    {"expected_old": "Old safe.", "replacement": "safe value"}
                ],
            }
        ],
    )
    before_mutation = candidate.read_bytes()
    mutated = run_rule_candidate_validator(candidate, tmp_path / "mutation.json")
    assert mutated.returncode == 1
    assert "skill-owned policy" in mutated.stderr
    assert candidate.read_bytes() == before_mutation

    toml = blocked_repo / "automation.toml"
    toml.write_text('prompt = "Original"\n', encoding="utf-8")
    for replacement in ('Broken"quote', "bad\\q"):
        write_rule_candidate(
            candidate,
            rule_stack=[safe, toml],
            targets=[
                {
                    "rules": str(safe.resolve()), "history": None,
                    "source_sha256": hashlib.sha256(safe.read_bytes()).hexdigest(),
                    "markdown_policy": None,
                    "replacements": [{"expected_old": "Old safe.", "replacement": "Safe prose " * 20}],
                },
                {
                    "rules": str(toml.resolve()), "history": None,
                    "source_sha256": hashlib.sha256(toml.read_bytes()).hexdigest(),
                    "markdown_policy": None,
                    "replacements": [{"expected_old": "Original", "replacement": replacement}],
                },
            ],
        )
        original_candidate = candidate.read_bytes()
        original_sources = (safe.read_bytes(), toml.read_bytes())
        failed = run_rule_candidate_validator(candidate, tmp_path / "toml-failure.json")
        assert failed.returncode == 1
        assert "toml-syntax invalid TOML" in failed.stderr
        assert str(toml.resolve()) in failed.stderr
        assert candidate.read_bytes() == original_candidate
        assert (safe.read_bytes(), toml.read_bytes()) == original_sources


def test_rule_candidate_rejects_stale_and_duplicate_expected_old(
    tmp_path: pathlib.Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    target = repository / "contract.md"
    target.write_text("Old value.\n", encoding="utf-8", newline="\n")
    candidate = tmp_path / "candidate.json"
    target_entry: dict[str, object] = {
        "rules": str(target.resolve()),
        "history": None,
        "source_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "markdown_policy": None,
        "replacements": [
            {"expected_old": "Old value.", "replacement": "New value."},
            {"expected_old": "Old value.", "replacement": "Other value."},
        ],
    }
    write_rule_candidate(
        candidate,
        rule_stack=[target],
        targets=[target_entry],
    )
    duplicate = run_rule_candidate_validator(candidate, tmp_path / "duplicate.json")
    assert duplicate.returncode == 1
    assert "duplicates expected_old" in duplicate.stderr

    target_entry["replacements"] = [
        {"expected_old": "Old value.", "replacement": "New value."}
    ]
    write_rule_candidate(candidate, rule_stack=[target], targets=[target_entry])
    target.write_text("Changed value.\n", encoding="utf-8", newline="\n")
    stale = run_rule_candidate_validator(candidate, tmp_path / "stale.json")
    assert stale.returncode == 1
    assert "source-hash" in stale.stderr and "source is stale" in stale.stderr
