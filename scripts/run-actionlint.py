#!/usr/bin/env python3
"""Provision and run the repository's pinned actionlint executable.

The official release archive is selected from a closed platform map and checked
against its recorded SHA-256 digest before extraction. The executable is cached
inside ``scripts/.venv``; uv owns that environment and removes the cache with it.
Only the expected archive member is read, and installation uses an atomic
replacement. The runner disables optional external linters so results do not
depend on undeclared shellcheck or pyflakes installations.
"""

from __future__ import annotations

import hashlib
import io
import os
import pathlib
import platform
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

VERSION = "1.7.12"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
ASSETS = {
    ("darwin", "amd64"): "5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644",
    ("darwin", "arm64"): "aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f",
    ("linux", "386"): "72a44b32c2d032700e6d0c23ca2f540b67519ec68db098ddfcfa96059e61f723",
    ("linux", "amd64"): "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
    ("linux", "arm64"): "325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6",
    ("linux", "armv6"): "ae4a0a5227578e66f5d00ee02788d5c64fdae1fa6484ab88ceaeee9359c28fa4",
    ("windows", "386"): "cdc8643b2c8dc890c76ad16095da97e75f86572805cc3573cc13f31ea0f19127",
    ("windows", "amd64"): "6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9",
    ("windows", "arm64"): "cadcf7ea4efe3a68728893813643cebe1185e5b1d4be5b96245f65c9a4d5ea41",
}
ARCHITECTURES = {
    "aarch64": "arm64",
    "amd64": "amd64",
    "arm64": "arm64",
    "armv6l": "armv6",
    "i386": "386",
    "i686": "386",
    "x86": "386",
    "x86_64": "amd64",
}
WORKFLOW_PATTERNS = (
    ".github/workflows/**/*.yml",
    ".github/workflows/**/*.yaml",
)


def release_asset(system: str, machine: str) -> tuple[str, str, str]:
    """Return the official archive name, digest, and executable for one host."""

    operating_system = system.lower()
    architecture = ARCHITECTURES.get(machine.lower())
    key = (operating_system, architecture or "")
    digest = ASSETS.get(key)
    if digest is None:
        raise RuntimeError(f"actionlint {VERSION} does not support {system}/{machine}")
    executable = "actionlint.exe" if operating_system == "windows" else "actionlint"
    extension = "zip" if operating_system == "windows" else "tar.gz"
    archive = f"actionlint_{VERSION}_{operating_system}_{architecture}.{extension}"
    return archive, digest, executable


def extract_executable(payload: bytes, archive: str, executable: str) -> bytes:
    """Read exactly one regular executable member without extracting paths."""

    if archive.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as package:
            zip_members = [
                member
                for member in package.infolist()
                if not member.is_dir()
                and pathlib.PurePosixPath(member.filename).name == executable
                and (member.external_attr >> 16) & 0o170000 != stat.S_IFLNK
            ]
            if len(zip_members) != 1:
                raise RuntimeError("actionlint archive must contain one regular executable")
            content = package.read(zip_members[0])
    else:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as package:
            tar_members = [
                member
                for member in package.getmembers()
                if member.isfile()
                and pathlib.PurePosixPath(member.name).name == executable
            ]
            if len(tar_members) != 1:
                raise RuntimeError("actionlint archive must contain one regular executable")
            source = package.extractfile(tar_members[0])
            if source is None:
                raise RuntimeError("actionlint executable could not be read")
            content = source.read()
    if not content:
        raise RuntimeError("actionlint executable is empty")
    return content


def download_archive(archive: str, digest: str) -> bytes:
    """Download one bounded official release asset and verify its digest."""

    url = f"https://github.com/rhysd/actionlint/releases/download/v{VERSION}/{archive}"
    request = urllib.request.Request(url, headers={"User-Agent": "ceratops-actionlint-runner"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise RuntimeError("actionlint archive exceeds the size limit")
    if hashlib.sha256(payload).hexdigest() != digest:
        raise RuntimeError("actionlint archive SHA-256 mismatch")
    return payload


def environment_binary(executable: str) -> pathlib.Path:
    """Keep the downloaded tool inside the uv environment running this script."""

    scripts = pathlib.Path(__file__).resolve().parent
    binary_directory = scripts / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    interpreter_directory = pathlib.Path(sys.executable).absolute().parent
    if os.path.normcase(str(interpreter_directory)) != os.path.normcase(
        str(binary_directory.absolute())
    ):
        raise RuntimeError("run actionlint through the locked scripts uv environment")
    if not binary_directory.is_dir():
        raise RuntimeError("scripts uv environment is missing")
    return binary_directory / executable


def expected_version(binary: pathlib.Path) -> bool:
    """Accept only the recorded actionlint version from an executable cache."""

    if binary.is_symlink() or not binary.is_file():
        return False
    result = subprocess.run(
        [str(binary), "-version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0 and output.splitlines()[:1] == [VERSION]


def install_binary(target: pathlib.Path, content: bytes) -> None:
    """Atomically replace only the actionlint cache inside the uv environment."""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".actionlint-", suffix=".tmp", dir=target.parent
    )
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        if os.name != "nt":
            temporary.chmod(0o755)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def provision_actionlint() -> pathlib.Path:
    """Return a verified cached executable, provisioning it when necessary."""

    archive, digest, executable = release_asset(platform.system(), platform.machine())
    target = environment_binary(executable)
    if expected_version(target):
        return target
    payload = download_archive(archive, digest)
    install_binary(target, extract_executable(payload, archive, executable))
    if not expected_version(target):
        target.unlink(missing_ok=True)
        raise RuntimeError("installed actionlint executable has the wrong version")
    return target


def workflow_files(repository: pathlib.Path) -> list[str]:
    """Return each regular direct or nested workflow YAML file exactly once."""

    return sorted(
        {
            path.relative_to(repository).as_posix()
            for pattern in WORKFLOW_PATTERNS
            for path in repository.glob(pattern)
            if path.is_file() and not path.is_symlink()
        }
    )


def main() -> int:
    """Provision actionlint, then check the explicit recursive workflow set."""

    repository = pathlib.Path(__file__).resolve().parents[1]
    workflows = workflow_files(repository)
    if not workflows:
        return 0
    try:
        binary = provision_actionlint()
    except (OSError, RuntimeError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"actionlint setup failed: {exc}", file=sys.stderr)
        return 3
    result = subprocess.run(
        [str(binary), "-shellcheck=", "-pyflakes=", *workflows],
        cwd=repository,
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
