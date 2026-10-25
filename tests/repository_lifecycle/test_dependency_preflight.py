from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
from typing import Any, Self

import pytest

from tests.repository_lifecycle.support import load_pr_workflow_module
from tests.support.repositories import run_git


@pytest.mark.parametrize("wait_status", ["ready", "timed_out", "failed"])
@pytest.mark.parametrize("annotation_state", ["clear", "warning", "unavailable"])
@pytest.mark.parametrize("interruption", ["none", "before_write", "after_write"])
def test_dependency_preflight_collects_evidence_after_rebase_wait(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, wait_status: str,
    annotation_state: str, interruption: str,
) -> None:
    queue = load_pr_workflow_module(monkeypatch, "dependency_queue")
    common = load_pr_workflow_module(monkeypatch, "dependency_common")
    order = []
    old = {
        "title": "Bump demo from 1.0.0 to 1.0.1",
        "head_oid": "a" * 40, "files": [{"path": "old.txt"}],
    }
    new = {
        **old, "head_oid": "b" * 40,
        "head_ref": "dependabot/npm_and_yarn/demo-1.0.1",
        "files": [{"path": ".github/workflows/ci.yml"}, {"path": "package.json"}],
        "rebase_wait": {"status": wait_status},
    }
    pr = {"repo": "owner/repo", "number": 1, "title": old["title"]}
    snapshot = {
        "outcome": {"blocked": False}, "summary": {"org": "owner"},
        "open_dependabot_prs": [pr], "open_dependabot_alerts": [],
    }
    monkeypatch.setattr(
        queue, "refresh_snapshot",
        lambda *_: (snapshot, subprocess.CompletedProcess([], 0, "", "")),
    )
    monkeypatch.setattr(queue, "fetch_pr_batch", lambda *_: ({("owner/repo", 1): old}, []))

    def wait(details: dict[Any, Any]):
        assert details[("owner/repo", 1)]["head_oid"] == "a" * 40
        order.append("wait")
        return {("owner/repo", 1): new}, (
            [{"repo": "owner/repo", "pr": 1, "check": "branch_freshness", "message": "unavailable"}]
            if wait_status == "failed" else []
        )

    monkeypatch.setattr(queue, "wait_for_dependabot_rebases", wait)
    monkeypatch.setattr(queue, "queued_repositories", lambda _: [{"repo": "owner/repo", "name": "repo"}])
    monkeypatch.setattr(
        common, "run_command",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "https://github.com/owner/repo.git" if "remote" in command else str(tmp_path), ""
        ),
    )

    def ci(_: pathlib.Path):
        assert order == ["wait"]
        order.append("ci")
        return {"status": "ok"}

    monkeypatch.setattr(queue, "exact_ci_evidence", ci)
    monkeypatch.setattr(queue, "registry_evidence", lambda _: {"status": "ok"})
    annotations: dict[str, Any] = {
        "status": "unavailable" if annotation_state == "unavailable" else "ok",
        "counts": {"warning": int(annotation_state == "warning"), "failure": 0, "notice": 1},
        "annotations": [], "error": "GitHub messages unavailable",
    }

    def messages(repo: str, head: str):
        assert repo == "owner/repo" and head == new["head_oid"]
        return annotations

    monkeypatch.setattr(queue, "fetch_ci_annotations", messages)

    def tree(checkout: pathlib.Path, update: dict[str, Any], files: list[Any], alerts: list[Any], commit: str):
        assert files == new["files"]
        assert update["ecosystem"] == "npm"
        assert commit == new["head_oid"]
        assert order == ["wait", "ci"]
        order.append("tree")
        return {"status": "ok"}

    monkeypatch.setattr(queue, "dependency_tree_evidence", tree)
    monkeypatch.setattr(queue, "emit_result", lambda *_: None)
    output = tmp_path / "preflight.json"
    args = queue.build_parser().parse_args([
        "preflight", "--org", "owner",
        "--snapshot-helper", str(tmp_path / "snapshot.py"),
        "--snapshot", str(tmp_path / "snapshot.json"), "--output", str(output),
        "--workspace-root", str(tmp_path),
        "--checkout", f"owner/repo={tmp_path}",
    ])
    write = queue.write_json

    def interrupted_write(path: pathlib.Path, payload: dict[str, Any]):
        if interruption == "after_write":
            write(path, payload)
        raise RuntimeError("interrupted report write")

    if interruption != "none":
        monkeypatch.setattr(queue, "write_json", interrupted_write)
        with pytest.raises(RuntimeError, match="interrupted report write"):
            queue.preflight(args)
        assert output.exists() == (interruption == "after_write")
        monkeypatch.setattr(queue, "write_json", write)
        order.clear()
    assert queue.preflight(args) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    repository = payload["repositories"][0]
    assert repository["checkout"]["explicit"] is True
    result_pr = repository["pull_requests"][0]
    assert result_pr["live"]["head_oid"] == "b" * 40
    assert result_pr["decision_gates"]["rebase_repair_required"] == (wait_status == "timed_out")
    assert result_pr["ci_annotations"] == annotations
    assert result_pr["decision_gates"]["ci_warning_review_required"] == (annotation_state == "warning")
    assert payload["summary"]["ci_warning_annotations"] == int(annotation_state == "warning")
    assert payload["summary"]["ci_annotation_reads_unavailable"] == int(annotation_state == "unavailable")
    assert payload["outcome"]["blocked"] == (wait_status == "failed" or annotation_state == "unavailable")
    assert order == ["wait", "ci", "tree"]
    order.clear()
    assert queue.preflight(args) == 0
    repeated = json.loads(output.read_text(encoding="utf-8"))
    assert repeated["repositories"] == payload["repositories"]
    assert repeated["summary"] == payload["summary"]


@pytest.mark.parametrize("head_ref,expected", [
    ("dependabot/npm_and_yarn/vendor/demo-2", "npm"),
    ("dependabot/uv/scripts/ruff-0.17.1", "pip"),
    ("dependabot/pip/pypdf-6.21.2", "pip"),
    ("dependabot/github_actions/owner/action-v2", "github-actions"),
    ("dependabot/gradle/wrapper-9.8", "gradle"),
    ("dependabot/composer/vendor/package-2", "composer"),
    ("codex/dependency-repairs", None),
    ("dependabot//unknown", None),
    ("", None),
])
def test_dependabot_branch_keeps_its_package_manager(monkeypatch, head_ref, expected):
    evidence = load_pr_workflow_module(monkeypatch, "dependency_evidence")
    for _ in range(2):
        assert evidence.dependabot_branch_ecosystem(head_ref) == expected


@pytest.mark.parametrize(
    "mode", ["passed", "empty", "overflow", "denied", "timeout", "bad_json", "wrong_head",
             "missing_page", "missing_annotations", "bad_annotation", "bad_count"],
)
def test_dependency_ci_annotations_keep_successful_job_warnings_and_read_failures(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    evidence = load_pr_workflow_module(monkeypatch, "dependency_evidence")
    head = "a" * 40
    warning = {"annotation_level": "warning", "message": "deprecated input", "path": "ci.yml",
               "start_line": 7, "end_line": 8, "title": "Deprecation"}
    annotations = [warning, {"annotation_level": "notice", "message": "runner update"},
                   {"annotation_level": "failure", "message": "failed input"}]
    if mode == "overflow":
        annotations = [{**warning, "message": "x" * 2001}] * 101
    check: dict[str, Any] = {"id": 42, "head_sha": "b" * 40 if mode == "wrong_head" else head,
             "name": "ci", "conclusion": "success", "html_url": "https://github.com/owner/repo/runs/42",
             "output": {"annotations_count": len(annotations)}}
    calls: list[list[str]] = []

    def query(command: list[str], **kwargs: Any):
        calls.append(command)
        assert command[-2:] == ["--paginate", "--slurp"]
        assert 0 < kwargs["timeout"] <= 30
        if "/commits/" in command[2]:
            assert f"/commits/{head}/" in command[2]
            payload: Any = [{"total_count": 0 if mode == "empty" else 1,
                             "check_runs": [] if mode in {"empty", "missing_page"} else [check]}]
            if mode == "bad_count":
                check["output"]["annotations_count"] = -1
        else:
            assert command[2] == "repos/owner/repo/check-runs/42/annotations?per_page=100"
            if mode == "timeout":
                raise subprocess.TimeoutExpired(command, 30)
            if mode == "denied":
                return subprocess.CompletedProcess(command, 1, "", "HTTP 403")
            if mode == "bad_json":
                return subprocess.CompletedProcess(command, 0, "broken JSON", "")
            payload = [annotations[:100], annotations[100:]] if mode == "overflow" else [annotations]
            if mode == "missing_annotations":
                payload = [[]]
            elif mode == "bad_annotation":
                payload = [[{"annotation_level": "surprise", "message": "bad"}]]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(evidence, "run_command", query)
    first = evidence.fetch_ci_annotations("owner/repo", head)
    repeated = evidence.fetch_ci_annotations("owner/repo", head)
    assert repeated == first
    assert first["head_oid"] == head
    if mode in {"passed", "overflow"}:
        assert first["status"] == "ok"
        assert first["counts"]["warning"] == (101 if mode == "overflow" else 1)
        assert first["annotations"][0]["check_conclusion"] == "success"
        assert first["annotations"][0]["path"] == "ci.yml"
        assert first["annotations"][0]["start_line"] == 7
        assert first["annotations"][0]["check_run_id"] == 42
        if mode == "overflow":
            assert len(first["annotations"]) == 50 and first["omitted_annotations"] == 51
            assert first["annotations"][0]["message_truncated"]
            assert len(first["annotations"][0]["message"]) == 2000
        else:
            assert first["counts"] == {"warning": 1, "failure": 1, "notice": 1}
            assert len(first["annotations"]) == 2
    elif mode == "empty":
        assert first["status"] == "ok" and first["check_runs"] == 0
        assert first["counts"] == {"warning": 0, "failure": 0, "notice": 0}
        assert len(calls) == 2
    else:
        assert first["status"] == "unavailable" and first["error"]
        if mode == "denied":
            failure = json.loads(first["error"])
            assert failure["exit_status"] == 1 and failure["stderr"] == "HTTP 403"
            assert failure["stdout"] == "<absent>" and failure["invocation"][0] == "gh"
    calls.clear()
    assert evidence.fetch_ci_annotations("owner/repo", "")["status"] == "unavailable"
    assert not calls


@pytest.mark.parametrize(
    ("patched", "reason"),
    [(None, "no_patched_version_listed"), ("", "no_patched_version_listed"),
     ("not-a-version", "unparsed_patched_version_range"), ("2.0.2", None)],
)
@pytest.mark.parametrize("interruption", ["none", "before_write", "after_write"])
def test_alert_preflight_distinguishes_missing_patch_and_repeats_after_interruption(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    patched: str | None, reason: str | None, interruption: str,
) -> None:
    queue = load_pr_workflow_module(monkeypatch, "dependency_queue")
    alert = {"repo": "owner/repo", "number": 8, "package": "extract-zip",
             "ecosystem": "npm", "patched_versions": patched, "manifest_path": "package.json"}
    snapshot = {
        "outcome": {"blocked": False, "queue_present": True},
        "summary": {"org": "owner", "open_dependabot_alerts": 1},
        "open_dependabot_prs": [], "open_dependabot_alerts": [alert],
    }
    monkeypatch.setattr(queue, "refresh_snapshot", lambda *_: (
        snapshot, subprocess.CompletedProcess([], 0, "", ""),
    ))
    monkeypatch.setattr(queue, "fetch_pr_batch", lambda *_: ({}, []))
    monkeypatch.setattr(queue, "wait_for_dependabot_rebases", lambda *_: ({}, []))
    monkeypatch.setattr(queue, "queued_repositories", lambda _: [{"repo": "owner/repo"}])
    monkeypatch.setattr(queue, "resolve_checkout", lambda *_, **__: {
        "status": "found", "path": str(tmp_path), "explicit": True,
    })
    monkeypatch.setattr(queue, "exact_ci_evidence", lambda _: {"status": "ok"})
    monkeypatch.setattr(queue, "dependency_tree_evidence", lambda *_: {"status": "ok"})
    monkeypatch.setattr(queue, "fetch_default_branch_head", lambda *_: {
        "status": "ok", "branch": "main", "commit": "a" * 40,
    })
    registry_targets: list[str] = []

    def registry(update: dict[str, Any]) -> dict[str, str]:
        registry_targets.append(update["target_version"])
        return {"status": "ok"}

    monkeypatch.setattr(queue, "registry_evidence", registry)
    monkeypatch.setattr(queue, "emit_result", lambda *_: None)
    original_write = queue.write_json
    writes = 0

    def write(path: pathlib.Path, payload: dict[str, Any]) -> None:
        nonlocal writes
        writes += 1
        if writes == 1 and interruption == "before_write":
            raise OSError("interrupted before output")
        original_write(path, payload)
        if writes == 1 and interruption == "after_write":
            raise OSError("interrupted after output")

    monkeypatch.setattr(queue, "write_json", write)
    output = tmp_path / "preflight.json"
    argv = ["preflight", "--org", "owner", "--snapshot-helper", str(tmp_path / "snapshot.py"),
            "--snapshot", str(tmp_path / "snapshot.json"), "--output", str(output),
            "--workspace-root", str(tmp_path)]
    assert queue.main(argv) == (0 if interruption == "none" else 1)
    if interruption != "none":
        assert output.exists() == (interruption == "after_write")
    for _ in range(2):
        assert queue.main(argv) == 0
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["outcome"]["blocked"] == (reason is not None)
        assert payload["summary"]["open_dependabot_alerts"] == 1
        blockers = payload["blockers"]
        if reason is None:
            assert blockers == []
        else:
            assert len(blockers) == 1 and blockers[0]["alert"] == 8
            assert blockers[0]["message"] == reason
            assert blockers[0]["check"] == (
                "alert_patch_availability" if reason == "no_patched_version_listed"
                else "alert_registry_preflight"
            )
    assert registry_targets == (["2.0.2"] * 3 if reason is None else [])


@pytest.mark.parametrize(
    "fault", ["none", "missing_release", "duplicate_release", "not_list", "non_final",
              "missing_checksum", "wrong_url", "before_response", "during_read", "after_read"],
)
def test_gradle_registry_checks_exact_release_and_repeats_after_interruption(
    monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    evidence = load_pr_workflow_module(monkeypatch, "dependency_evidence")
    release: dict[str, Any] = {
        "version": "9.8.1", "released": True, "final": True, "snapshot": False, "broken": False,
        "downloadUrl": "https://services.gradle.org/distributions/gradle-9.8.1-bin.zip",
        "checksum": "a" * 64, "wrapperChecksum": "b" * 64,
    }
    payload: Any = [release]
    if fault == "missing_release":
        payload = []
    elif fault == "duplicate_release":
        payload = [release, release]
    elif fault == "not_list":
        payload = release
    elif fault == "non_final":
        release["final"] = False
    elif fault == "missing_checksum":
        release.pop("wrapperChecksum")
    elif fault == "wrong_url":
        release["downloadUrl"] = "https://example.invalid/gradle-9.8.1-bin.zip"
    attempts = 0

    class Response:
        def __init__(self, attempt: int) -> None:
            self.attempt = attempt

        def __enter__(self) -> Self:
            return self

        def read(self) -> bytes:
            if self.attempt == 1 and fault == "during_read":
                raise OSError("interrupted while reading")
            return json.dumps(payload).encode("utf-8")

        def __exit__(self, *_: object) -> None:
            if self.attempt == 1 and fault == "after_read":
                raise OSError("interrupted after response")

    def open_metadata(request: Any, *, timeout: int) -> Response:
        nonlocal attempts
        attempts += 1
        assert request.full_url == "https://services.gradle.org/versions/all"
        assert timeout == 20
        if attempts == 1 and fault == "before_response":
            raise OSError("interrupted before response")
        return Response(attempts)

    monkeypatch.setattr(evidence.urllib.request, "urlopen", open_metadata)
    update = evidence.parse_update(
        "Bump gradle-wrapper from 9.8.0 to 9.8.1",
        [{"path": "gradle/wrapper/gradle-wrapper.properties"}], [],
    )
    assert update["ecosystem"] == "gradle" and update["update_type"] == "patch"
    interruptions = {"before_response", "during_read", "after_read"}
    for attempt in range(2):
        result = evidence.registry_evidence(update)
        expected_ok = fault == "none" or (fault in interruptions and attempt == 1)
        assert result["status"] == ("ok" if expected_ok else "blocked")
        if expected_ok:
            assert result["version"] == "9.8.1" and result["wrapper_sha256"] == "b" * 64
    assert attempts == 2


@pytest.mark.parametrize(
    "fault", ["none", "duplicate_url", "missing_checksum", "wrong_url", "missing_jar",
              "checkout_escape", "noncanonical_property"],
)
def test_gradle_wrapper_context_requires_unambiguous_checkout_bound_files(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    evidence = load_pr_workflow_module(monkeypatch, "dependency_evidence")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    relative = "gradle/wrapper/gradle-wrapper.properties"
    if fault == "checkout_escape":
        relative = "../outside/gradle-wrapper.properties"
    properties = checkout / relative
    properties.parent.mkdir(parents=True)
    url = "distributionUrl=https\\://services.gradle.org/distributions/gradle-9.8.0-bin.zip\n"
    text = url + "distributionSha256Sum=" + "a" * 64 + "\n"
    if fault == "duplicate_url":
        text += url
    elif fault == "missing_checksum":
        text = url
    elif fault == "wrong_url":
        text = text.replace("services.gradle.org", "example.invalid")
    elif fault == "noncanonical_property":
        text += "distribution\\u0055rl=https://example.invalid/gradle-9.8.0-bin.zip\n"
    properties.write_text(text, encoding="utf-8")
    jar_bytes = b"checkout wrapper context"
    if fault != "missing_jar":
        (properties.parent / "gradle-wrapper.jar").write_bytes(jar_bytes)
    update = {"package": "gradle-wrapper", "ecosystem": "gradle", "target_version": "9.8.1"}
    for _ in range(2):
        result = evidence.dependency_tree_evidence(checkout, update, [{"path": relative}], [])
        assert result["status"] == ("ok" if fault == "none" else "blocked")
        if fault == "none":
            assert result["sources"][0]["version"] == "9.8.0"
            assert result["sources"][0]["wrapper_sha256"] == hashlib.sha256(jar_bytes).hexdigest()
    unsupported = evidence.parse_update("Bump another-tool from 9.8.0 to 9.8.1", [{"path": relative}], [])
    assert unsupported["ecosystem"] == "unknown"


def _git_output(repo: pathlib.Path, *arguments: str) -> str:
    result = run_git(repo, *arguments)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commit_dependency_files(repo: pathlib.Path, files: dict[str, bytes]) -> str:
    repo.mkdir()
    _git_output(repo, "init", "-b", "main")
    _git_output(repo, "config", "user.name", "Dependency reader tests")
    _git_output(repo, "config", "user.email", "dependency-tests@example.invalid")
    _git_output(repo, "config", "core.autocrlf", "false")
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    _git_output(repo, "add", "--all")
    _git_output(repo, "commit", "-m", "Dependency fixture")
    return _git_output(repo, "rev-parse", "HEAD")


def _checkout_state(repo: pathlib.Path) -> tuple[Any, ...]:
    files = sorted(
        (path.relative_to(repo).as_posix(), path.read_bytes())
        for path in repo.rglob("*")
        if ".git" not in path.relative_to(repo).parts and path.is_file()
    )
    fetch_head = repo / ".git/FETCH_HEAD"
    return (
        _git_output(repo, "rev-parse", "HEAD"),
        _git_output(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        _git_output(repo, "status", "--porcelain", "--untracked-files=all"),
        (repo / ".git/index").read_bytes(),
        fetch_head.read_bytes() if fetch_head.exists() else None,
        files,
    )


@pytest.mark.parametrize("ecosystem", ["npm", "pip", "github-actions", "gradle"])
@pytest.mark.parametrize("local_state", ["changed", "moved", "deleted", "directory"])
def test_dependency_commit_reader_ignores_local_file_layout(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ecosystem: str, local_state: str,
) -> None:
    evidence = load_pr_workflow_module(monkeypatch, "dependency_evidence")
    fixtures = {
        "npm": {
            "package.json": b'{"devDependencies":{"demo":"2.0.0"},"engines":{"node":">=22"}}',
            "package-lock.json": b'{"packages":{"node_modules/demo":{"version":"2.0.0"}}}',
        },
        "pip": {
            "pyproject.toml": b'[project]\nrequires-python = ">=3.14"\ndependencies = ["demo==2.0.0"]\n',
            "uv.lock": b'[[package]]\nname = "demo"\nversion = "2.0.0"\n',
            "poetry.lock": b'[[package]]\nname = "demo"\nversion = "2.0.0"\n',
            "requirements.txt": b'demo==2.0.0\r\n',
        },
        "github-actions": {".github/workflows/ci.yml": b"steps:\r\n  - uses: actions/checkout@v4\r\n"},
        "gradle": {
            "gradle/wrapper/gradle-wrapper.properties": (
                b"distributionUrl=https\\://services.gradle.org/distributions/gradle-9.8.1-bin.zip\r\n"
                b"distributionSha256Sum=" + b"a" * 64 + b"\r\n"
            ),
            "gradle/wrapper/gradle-wrapper.jar": b"\x00committed wrapper\xff",
        },
    }
    files = fixtures[ecosystem]
    checkout = tmp_path / "checkout"
    head = _commit_dependency_files(checkout, files)
    for relative in files:
        path = checkout / relative
        if local_state == "changed":
            path.write_bytes(b"unpublished local content")
        elif local_state == "moved":
            destination = checkout / "scripts/moved" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            path.rename(destination)
        else:
            path.unlink()
            if local_state == "directory":
                path.mkdir()
    before = _checkout_state(checkout)
    package = {"github-actions": "actions/checkout", "gradle": "gradle-wrapper"}.get(ecosystem, "demo")
    update = {"package": package, "ecosystem": ecosystem}
    first = evidence.dependency_tree_evidence(checkout, update, [{"path": name} for name in files], [], head)
    assert first["status"] == "ok", first
    assert first["commit"] == head
    assert evidence.dependency_tree_evidence(checkout, update, [{"path": name} for name in files], [], head) == first
    assert _checkout_state(checkout) == before
    sources = first["sources"]
    if ecosystem == "npm":
        assert sources[0]["matches"][0]["version"] == "2.0.0"
        assert sources[1]["declared"]["constraint"] == "2.0.0"
        assert sources[1]["project_engines"] == {"node": ">=22"}
    elif ecosystem == "pip":
        assert len(sources) == 4
        assert all(source["matches"] for source in sources)
        assert any(source.get("project_requires_python") == ">=3.14" for source in sources)
    elif ecosystem == "github-actions":
        assert sources == [{"file": ".github/workflows/ci.yml", "line": 2, "source": package, "ref": "v4"}]
    else:
        assert sources[0]["version"] == "9.8.1"
        assert sources[0]["wrapper_sha256"] == hashlib.sha256(files["gradle/wrapper/gradle-wrapper.jar"]).hexdigest()


def _cold_dependency_checkout(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    source = tmp_path / "origin"
    _commit_dependency_files(source, {"package.json": b'{"dependencies":{"demo":"1.0.0"}}'})
    _git_output(source, "switch", "-c", "dependabot/demo")
    (source / "package.json").write_bytes(b'{"dependencies":{"demo":"2.0.0"}}')
    _git_output(source, "add", "package.json")
    _git_output(source, "commit", "-m", "Bump demo to 2.0.0")
    head = _git_output(source, "rev-parse", "HEAD")
    checkout = tmp_path / "checkout"
    _git_output(tmp_path, "clone", "--no-local", "--single-branch", "--branch", "main", str(source), str(checkout))
    assert run_git(checkout, "cat-file", "-e", f"{head}^{{commit}}").returncode != 0
    return checkout, head


@pytest.mark.parametrize("interruption", ["none", "before_fetch", "after_fetch"])
def test_dependency_commit_fetch_converges_without_changing_checkout(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, interruption: str,
) -> None:
    evidence = load_pr_workflow_module(monkeypatch, "dependency_evidence")
    checkout, head = _cold_dependency_checkout(tmp_path)
    before = _checkout_state(checkout)
    original_run = subprocess.run
    fetches: list[list[str]] = []
    interrupt = interruption != "none"

    def run(command: list[str], **kwargs: Any):
        if "fetch" in command:
            fetches.append(command)
            assert command[-4:] == ["--no-tags", "--no-write-fetch-head", "origin", head]
            if interrupt and interruption == "before_fetch":
                raise KeyboardInterrupt("before fetch")
            result = original_run(command, **kwargs)
            if interrupt and interruption == "after_fetch":
                raise KeyboardInterrupt("after fetch")
            return result
        return original_run(command, **kwargs)

    monkeypatch.setattr(evidence.subprocess, "run", run)
    update = {"package": "demo", "ecosystem": "npm"}
    if interrupt:
        with pytest.raises(KeyboardInterrupt, match="fetch"):
            evidence.dependency_tree_evidence(checkout, update, [], [], head)
        assert _checkout_state(checkout) == before
        interrupt = False
    result = evidence.dependency_tree_evidence(checkout, update, [], [], head)
    assert result["status"] == "ok", result
    assert result["sources"][0]["declared"]["constraint"] == "2.0.0"
    assert evidence.dependency_tree_evidence(checkout, update, [], [], head) == result
    assert len(fetches) == (2 if interruption == "before_fetch" else 1)
    assert _checkout_state(checkout) == before


@pytest.mark.parametrize("operation", ["rev-parse", "fetch", "ls-tree", "cat-file"])
@pytest.mark.parametrize("fault", ["failed", "timeout", "oserror"])
def test_dependency_commit_read_failures_preserve_cause_without_local_fallback(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, operation: str, fault: str,
) -> None:
    evidence = load_pr_workflow_module(monkeypatch, "dependency_evidence")
    if operation == "fetch":
        checkout, head = _cold_dependency_checkout(tmp_path)
    else:
        checkout = tmp_path / "checkout"
        head = _commit_dependency_files(checkout, {"package.json": b'{"dependencies":{"demo":"2.0.0"}}'})
    before = _checkout_state(checkout)
    original_run = subprocess.run
    failed_commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any):
        if operation not in command:
            return original_run(command, **kwargs)
        failed_commands.append(command)
        if fault == "timeout":
            raise subprocess.TimeoutExpired(command, 30, output=b"partial output", stderr=b"connection timeout")
        if fault == "oserror":
            raise OSError("Git process could not start")
        return subprocess.CompletedProcess(command, 73, b"partial output", b"token=secret-fixture denied")

    monkeypatch.setattr(evidence.subprocess, "run", run)
    first = evidence.dependency_tree_evidence(checkout, {"package": "demo", "ecosystem": "npm"}, [], [], head)
    assert first["status"] in {"unavailable", "blocked"}
    assert first["sources"] == []
    failure = first["errors"][0]
    assert failure["operation"] == "read_dependency_commit_files"
    assert failure["checkout"] == str(checkout) and failure["commit"] == head
    assert pathlib.Path(failure["command"][0]).is_absolute()
    assert operation in failure["command"]
    assert failure["exit_code"] == (73 if fault == "failed" else None)
    assert failure["stdout"] == ("<absent>" if fault == "oserror" else "partial output")
    assert failure["stderr"] == {
        "failed": "token=<redacted> denied", "timeout": "connection timeout", "oserror": "<absent>",
    }[fault]
    assert "secret-fixture" not in json.dumps(first)
    assert evidence.dependency_tree_evidence(checkout, {"package": "demo", "ecosystem": "npm"}, [], [], head) == first
    assert len(failed_commands) == 2
    monkeypatch.setattr(evidence.subprocess, "run", original_run)
    assert _checkout_state(checkout) == before


@pytest.mark.parametrize("head", ["", "main", "a" * 39])
def test_dependency_commit_reader_requires_exact_head(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, head: str,
) -> None:
    evidence = load_pr_workflow_module(monkeypatch, "dependency_evidence")
    (tmp_path / "package.json").write_text('{"dependencies":{"demo":"2.0.0"}}', encoding="utf-8")
    for _ in range(2):
        result = evidence.dependency_tree_evidence(tmp_path, {"package": "demo"}, [], [], head)
        assert result["status"] == "unavailable" and result["sources"] == []
        assert result["errors"][0]["error"] == "Dependency source must be an exact commit ID"


@pytest.mark.parametrize("fault", ["malformed", "escape", "absolute", "symlink", "gitlink"])
def test_dependency_commit_reader_rejects_unsafe_or_malformed_sources(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    evidence = load_pr_workflow_module(monkeypatch, "dependency_evidence")
    checkout = tmp_path / "checkout"
    head = _commit_dependency_files(checkout, {"package.json": b"bad JSON" if fault == "malformed" else b"{}"})
    if fault in {"symlink", "gitlink"}:
        oid = _git_output(checkout, "rev-parse", f"{head}:package.json") if fault == "symlink" else head
        mode = "120000" if fault == "symlink" else "160000"
        _git_output(checkout, "update-index", "--cacheinfo", f"{mode},{oid},package.json")
        _git_output(checkout, "commit", "-m", "Unsupported dependency source type")
        head = _git_output(checkout, "rev-parse", "HEAD")
    (checkout / "package.json").write_bytes(b'{"dependencies":{"demo":"local"}}')
    relative = {"escape": "../package.json", "absolute": "/package.json"}.get(fault, "package.json")
    before = _checkout_state(checkout)
    for _ in range(2):
        result = evidence.dependency_tree_evidence(checkout, {"package": "demo"}, [{"path": relative}], [], head)
        assert result["status"] in {"blocked", "unavailable"} and result["errors"]
        assert result["sources"] == []
        if fault in {"escape", "absolute"}:
            assert result["errors"][0]["error"] == "path_outside_checkout"
        elif fault in {"symlink", "gitlink"}:
            assert result["errors"][0]["error"] == "Committed dependency source is not a regular file"
    assert _checkout_state(checkout) == before


@pytest.mark.parametrize("fault", ["missing_jar", "oversized_jar"])
def test_dependency_commit_wrapper_uses_only_bounded_committed_jar(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    evidence = load_pr_workflow_module(monkeypatch, "dependency_evidence")
    checkout = tmp_path / "checkout"
    relative = "gradle/wrapper/gradle-wrapper.properties"
    files = {relative: b"distributionUrl=https\\://services.gradle.org/distributions/gradle-9.8.1-bin.zip\n"
             b"distributionSha256Sum=" + b"a" * 64 + b"\n"}
    if fault == "oversized_jar":
        files["gradle/wrapper/gradle-wrapper.jar"] = b"x" * (5 * 1024 * 1024 + 1)
    head = _commit_dependency_files(checkout, files)
    (checkout / "gradle/wrapper/gradle-wrapper.jar").write_bytes(b"small local JAR")
    update = {"package": "gradle-wrapper", "ecosystem": "gradle"}
    for _ in range(2):
        result = evidence.dependency_tree_evidence(checkout, update, [{"path": relative}], [], head)
        assert result["status"] == "blocked" and result["sources"] == []
        assert result["errors"][0]["error"] == (
            "file_missing_from_dependency_commit" if fault == "missing_jar" else "wrapper JAR exceeds the evidence size bound"
        )


@pytest.mark.parametrize("fault", ["none", "missing_branch", "denied", "bad_commit", "timeout", "spawn"])
def test_alert_default_branch_query_keeps_exact_commit_and_failures(monkeypatch, fault):
    evidence = load_pr_workflow_module(monkeypatch, "dependency_evidence")
    calls = []

    def query(command, **kwargs):
        calls.append(command)
        assert command == ["gh", "api", "repos/owner/repo/commits/release%2Fstable", "--jq", ".sha"]
        assert kwargs["timeout"] == 30
        if fault == "timeout":
            raise subprocess.TimeoutExpired(command, 30, output="partial", stderr="timed out")
        if fault == "spawn":
            raise OSError("GitHub CLI unavailable")
        return subprocess.CompletedProcess(command, 1 if fault == "denied" else 0,
                                           "not-a-commit" if fault == "bad_commit" else "A" * 40 + "\n",
                                           "HTTP 403" if fault == "denied" else "")

    monkeypatch.setattr(evidence, "run_command", query)
    for _ in range(2):
        result = evidence.fetch_default_branch_head("owner/repo", None if fault == "missing_branch" else "release/stable")
        assert result["status"] == ("ok" if fault == "none" else "unavailable")
        if fault == "none":
            assert result["commit"] == "a" * 40 and result["branch"] == "release/stable"
        elif fault != "missing_branch":
            assert result["command"] == calls[-1]
            assert result["stdout"] and result["stderr"] and result["cause"]
            assert result["exit_code"] == (None if fault in {"timeout", "spawn"} else int(fault == "denied"))
    assert len(calls) == (0 if fault == "missing_branch" else 2)


@pytest.mark.parametrize("source_available", [True, False])
@pytest.mark.parametrize("interruption", ["none", "before_write", "after_write"])
def test_alert_preflight_reads_live_default_tree_without_touching_release_checkout(
    tmp_path, monkeypatch, source_available, interruption,
):
    queue = load_pr_workflow_module(monkeypatch, "dependency_queue")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for arguments in [("init", "-b", "main"), ("config", "user.name", "Test"),
                      ("config", "user.email", "test@example.invalid")]:
        assert run_git(checkout, *arguments).returncode == 0
    lock = checkout / "package-lock.json"
    lock.write_text(json.dumps({"packages": {"node_modules/demo": {"version": "1.0.0"}}}))
    assert run_git(checkout, "add", ".").returncode == 0
    assert run_git(checkout, "commit", "-m", "published dependency").returncode == 0
    published = run_git(checkout, "rev-parse", "HEAD").stdout.strip()
    assert run_git(checkout, "checkout", "-b", "release/local").returncode == 0
    lock.unlink()
    assert run_git(checkout, "add", "-A").returncode == 0
    assert run_git(checkout, "commit", "-m", "local release no longer has old tooling").returncode == 0
    before = _checkout_state(checkout)
    alerts = [{"repo": "owner/repo", "number": number, "package": "demo", "ecosystem": "npm",
               "patched_versions": "1.0.1", "manifest_path": "package-lock.json"} for number in (1, 2)]
    snapshot = {"outcome": {"blocked": False}, "summary": {"org": "owner"},
                "open_dependabot_prs": [], "open_dependabot_alerts": alerts}
    monkeypatch.setattr(queue, "refresh_snapshot", lambda *_: (snapshot, subprocess.CompletedProcess([], 0, "", "")))
    monkeypatch.setattr(queue, "fetch_pr_batch", lambda *_: ({}, []))
    monkeypatch.setattr(queue, "wait_for_dependabot_rebases", lambda *_: ({}, []))
    monkeypatch.setattr(queue, "queued_repositories", lambda _: [{"repo": "owner/repo", "default_branch": "main"}])
    monkeypatch.setattr(queue, "resolve_checkout", lambda *_, **__: {"status": "found", "path": str(checkout)})
    monkeypatch.setattr(queue, "exact_ci_evidence", lambda _: {"status": "ok"})
    monkeypatch.setattr(queue, "registry_evidence", lambda _: {"status": "ok"})
    monkeypatch.setattr(queue, "emit_result", lambda *_: None)
    heads = []

    def source(repo, branch):
        heads.append((repo, branch))
        return {"status": "ok", "commit": published} if source_available else {"status": "unavailable", "reason": "HTTP 403"}

    monkeypatch.setattr(queue, "fetch_default_branch_head", source)
    original_tree = queue.dependency_tree_evidence
    tree_reads = []

    def tree(*arguments):
        tree_reads.append(arguments[-1])
        return original_tree(*arguments)

    monkeypatch.setattr(queue, "dependency_tree_evidence", tree)
    output = tmp_path / "preflight.json"
    argv = ["preflight", "--org", "owner", "--snapshot-helper", str(tmp_path / "snapshot.py"),
            "--snapshot", str(tmp_path / "snapshot.json"), "--output", str(output), "--workspace-root", str(tmp_path)]
    original_write = queue.write_json

    def interrupted_write(path, payload):
        if interruption == "after_write":
            original_write(path, payload)
        raise OSError("interrupted report write")

    if interruption != "none":
        with monkeypatch.context() as patch:
            patch.setattr(queue, "write_json", interrupted_write)
            assert queue.main(argv) == 1
        assert output.exists() == (interruption == "after_write")
    for _ in range(2):
        previous = len(heads)
        previous_trees = len(tree_reads)
        assert queue.main(argv) == 0
        assert len(heads) == previous + 1
        assert len(tree_reads) == previous_trees + int(source_available)
        payload = json.loads(output.read_text())
        assert payload["outcome"]["blocked"] != source_available
        for alert in payload["repositories"][0]["alerts"]:
            alert_tree = alert["preflight"]["dependency_tree"]
            if source_available:
                assert alert_tree["status"] == "ok" and alert_tree["commit"] == published
                assert alert_tree["sources"][0]["matches"][0]["version"] == "1.0.0"
            else:
                assert alert_tree["status"] == "unavailable" and alert_tree["sources"] == []
        assert _checkout_state(checkout) == before and not lock.exists()
