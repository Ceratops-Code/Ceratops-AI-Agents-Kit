# Ceratops MCP Server Manager

One deterministic engine installs exact local Python MCP server releases, updates
installed MCP servers, and inspects versions. CLI and stdio MCP use that same engine.
Repository installation selects an MCP server and reads its name and version from
`pyproject.toml`, then packages and installs that release through the CLI. It
makes no model/API calls and has no UI.

## Layout and supported runtime

Manager source files live directly in
`mcp-servers/ceratops_mcp_server_manager/`, beside `pyproject.toml`. Hatch
maps these modules into the installed Python package; the standalone launcher
stays outside the wheel.

Editable source belongs in the MCP server's owning repository. Each MCP server
owns a separate directory under `%USERPROFILE%\.codex\mcp`, including its
packages, environments, version selection, registry, cache, and locks. The
manager's directory is
`%USERPROFILE%\.codex\mcp\ceratops_mcp_server_manager`. The skill installer
separately owns skill deployment.

```text
%USERPROFILE%\.codex\mcp\<mcp-server-name>\
  bin\                              stable launcher for this MCP server
  artifacts\<version>\<manifest-sha256>\
  versions\<version>\<instance>\environment\
  current.json                      selected complete installation
  registry.json                     exact version-to-manifest mapping
  staging\                          packaging scratch, removed on return
  cache\                            this MCP server's package/build cache
  locks\                            this MCP server's kernel-released locks
```

Deployment uses existing global Windows x64 CPython 3.14.x and uv 0.12.10 or
newer 0.12.x. Both are validated outside the MCP server store before creating an
installation; the manager never downloads its own Python or uv. Each installed
version has a separate virtual environment for dependencies. Global Python
must remain available and compatible while these environments are used.

`pylock.toml` records exact dependency versions and artifact hashes. Wheels
are installed offline with uv's hash enforcement, followed by dependency and
package readiness checks. A manager update changes its wheel dependencies;
global Python and uv remain independently maintained prerequisites.

## Source installation and use

From an active Ceratops-AI-Agents-Kit source checkout:

```powershell
uv run --locked scripts/deploy-mcp-server-manager.py
%USERPROFILE%\.codex\mcp\ceratops_mcp_server_manager\bin\ceratops_mcp_server_manager.cmd versions
```

The deployment script installs the source checkout's declared manager version,
including when a manager is already installed. It validates global
prerequisites, builds and registers the source release, prepares the launchers,
and calls the same packaging and deployment code used after installation. It
provisions hash-locked Python libraries in temporary storage and removes that
storage on success or failure; no libraries need to be installed globally.
It changes no Codex
configuration. An incompatible or missing prerequisite fails before deployment
writes installation files.

For a one-time move from a previous manager root, name that exact source
explicitly:

```powershell
uv run --locked scripts/deploy-mcp-server-manager.py --import-root <legacy-root>
```

The migration validates and imports only registered immutable artifacts, then
rebuilds each selected environment under the current root. It never copies an
old virtual environment or changes the legacy root, which remains available as
rollback state until it is deliberately removed.

| CLI command | MCP tool | Inputs |
| --- | --- | --- |
| `package --source <directory> [--lock]` | Not exposed | Reviewed MCP server source; optional lock refresh |
| `package --source <mcp-server-directory> --package-wheel <wheel> --package-lock <lockfile>` | Not exposed | Build only MCP server source; register its separate package wheel and locked dependencies |
| `install [--source <directory>] [--mcp-server-name <name>]` | Not exposed | Repository or MCP server source; defaults to the current directory |
| `install --source <mcp-server-directory> --package-wheel <wheel> --package-lock <lockfile>` | Not exposed | Build the MCP server and install it with the declared package wheel |
| Not exposed | `install` | `mcp_server_name`, `version` for an exact registered release |
| `update <mcp-server-name> <version>` | `update` | `mcp_server_name`, `version` |
| `versions [mcp-server-name]` | `versions` | optional `mcp_server_name` |

Omitting a version-inspection name selects `ceratops_mcp_server_manager`.
Update requires an existing installation. MCP install and CLI/MCP update accept
an explicitly selected previous registered version through the same engine.
There is no separate rollback operation, automatic rollback subsystem,
`create-tool` endpoint, shell/script input, or installation/output path input.
Source directories accepted by CLI install and package are reviewed build
inputs. Install accepts no version override: it packages the selected source
release and activates exactly that name and version. A direct MCP server directory
needs no Git. Otherwise Git enumerates tracked and non-ignored untracked
`mcp-server.json` declarations below the selected directory. Multiple MCP
servers require `--mcp-server-name`; duplicate names, absent matches, and
failed queries stop before building. Ignored environments are excluded.

The manager does not read `sdlc/sdlc.yml`. Repository lifecycle hands the
selected MCP server and any package prerequisite to `ceratops-mcp-server-lifecycle/install`.
That action builds and validates a declared package wheel, then passes its
wheel and canonical lockfile to the manager. The selected MCP server source
still determines its name and version; YAML does not supply a version override.

For example, from a repository root:

```powershell
%USERPROFILE%\.codex\mcp\ceratops_mcp_server_manager\bin\ceratops_mcp_server_manager.cmd install --mcp-server-name example_mcp_server
```

Public CLI and MCP results identify the MCP server with `mcp_server_name`. MCP returns
structured result data and a compact equivalent JSON text block.
CLI writes JSON to stdout and returns exit code 2 with a diagnostic on stderr
for a failed operation. `installed_version` is the selected next-launch
version. `running_version` is the responding manager process version; it is
null for other MCP servers because the manager does not supervise their processes.
Version inspection also returns available releases and the selected manifest
digest. `reconnection_required` reports a manager version difference.

## Development and release contracts

A source project declares `[project].name` and a static `[project].version`
in `pyproject.toml`, plus normal wheel packaging. Its `mcp-server.json`
declares only the readiness module:

```json
{
  "schema": 2,
  "module": "example_mcp_server"
}
```

The project name supplies both the MCP server and distribution names. It starts
with a lowercase letter, with single hyphens or underscores between
alphanumeric segments.
MCP server identities stay exact; distribution matching uses package-name
normalization. Release versions are exact numeric `major.minor.patch`
values. Module names use lowercase Python import components. Windows device
names, separators, traversal, malformed identities, and unknown fields fail
validation. Source metadata must remain stable during the build, and its name
and version must agree with the built wheel. A source-only MCP server keeps its
lock inside its MCP server directory. An MCP server with a separate package
dependency declares its exact version in `pyproject.toml` and supplies the
package's prebuilt wheel and canonical lockfile through the paired CLI flags.
The lockfile may be the package's `uv.lock` beside `pyproject.toml`, or an
existing PEP 751 lockfile. For `uv.lock`, the manager runs a locked export that
omits the package itself and development dependencies, then consumes the
temporary PEP 751 output inside its disposable staging transaction. It never
writes or replaces a package lockfile during installation.

Use a pinned maintained build backend. The module's fixed readiness invocation
is `python -I -B -m <module> --deployment-check`. It must return exactly:

```json
{"mcp_server_id": "example_mcp_server", "version": "1.0.0", "ready": true}
```

Readiness checks dependencies and necessary local prerequisites without
modifying user data. Create and test MCP servers in their owning development
repositories; MCP server creation never runs through this manager.

After the manager's first installation, use its public launcher from any
directory; a Ceratops-AI-Agents-Kit checkout is not required:

```powershell
%USERPROFILE%\.codex\mcp\ceratops_mcp_server_manager\bin\ceratops_mcp_server_manager.cmd package --source <mcp-server-source> --lock
%USERPROFILE%\.codex\mcp\ceratops_mcp_server_manager\bin\ceratops_mcp_server_manager.cmd package --source <mcp-server-source>
```

The first command writes a standard `pylock.toml` for review and commit. The
second builds the wheel, fetches compatible hash-locked PyPI dependency wheels,
installs the exact wheel set in a disposable isolated environment, and requires
the exact readiness response before registering one immutable local artifact
record. A failed preflight leaves the registry unchanged and its disposable
environment is removed. Packaging does not activate or retain that candidate.
Both commands are implemented inside the installed manager.
Packaging executes reviewed build code and downloads dependencies; it is an
explicit CLI capability, not an MCP operation or public-repository upload.
The source installer uses this same implementation for first installation and
manager updates, including upgrades from an older CLI. A package-backed MCP
server uses the paired package flags and still builds its own small MCP server
wheel from source. Other repository MCP servers use CLI install. Reconnect
after selecting a new manager version.

Persistent records and the fixed readiness response retain the schema-1
`mcp_server_id` field required by existing launchers and installed MCP
servers. Its value is the project name; it is not a separate identifier or
source declaration. The release manifest is a closed JSON object containing
`schema`, `mcp_server_id`, `version`, `distribution`, `module`, and
`wheels`. Each wheel has exactly a `filename` and `sha256`. The engine
validates every field, digest, wheel archive, and the MCP server's distribution
metadata before execution. All artifact paths are derived from validated
identities. Each MCP server's registry has only `schema`, its matching
`mcp_server_id`, and `versions`; its mapping is
`versions[version] = manifest_sha256`.
An existing identity/version cannot be reassigned to different artifact bytes.
Use a new version for changed releases.

## Activation and self-update

The engine creates a unique candidate directory at its final immutable path,
installs its environment, checks dependencies, and runs readiness. Virtual
environments are never moved after creation. Only then does an atomic JSON
replacement of that MCP server's `current.json` select the candidate. This file
records the exact version and installation folder to launch. A failed candidate
is removed and the prior selection stays
intact. Per-MCP server operating-system locks serialize writes
and are released when the owning process exits, including a crash.

Self-update uses this same sequence. The current process completes its request
from its existing directory; its files are never overwritten. The stable
launcher reads its own `current.json` at the next launch, so a new CLI
process or
MCP reconnection uses the selected version. Already running versions continue
to work because their leased environments are retained.
Every successful activation installs or refreshes that MCP server's stable launcher.
The launcher selects `current.json` and holds an instance lease for the child
process lifetime. At deployment startup and after a completed package or
activation, the manager retains the selected environment and two newest
complete predecessors. Older live environments are deferred. Incomplete
environments and unselected or orphaned packages receive a 24-hour recovery
window; after that they and any registry entries are removed. Reparse points and
unclassified paths are never cleanup targets.

## Codex registration and Forms boundary

The repository's `.codex/config.toml` registers only this service for trusted
development checkouts. It uses global `python`, the manager folder's installed
launcher, and `--mcp`, with the exact three-tool allowlist. It does not modify
the user global configuration or another project's configuration. Registration
alone does not prove the current Codex task has loaded the connection. Verify
callable MCP servers in that task separately; this setup does not restart the
desktop app.

Keep the restricted Forms agent in its separate restricted configuration,
without this service or shell/development capabilities. MCP stdio inherits
the launching host's authority and has no independent agent-role identity.
Do not launch Forms inside a development checkout that grants deployment.
A shared/global registration requires a verified Forms exclusion first.
The registry and reviewed package code are trusted development inputs;
readiness execution is not a sandbox for untrusted wheels.

## Validation

`tests/mcp_server_manager` covers the shared engine, CLI, actual SDK dispatch,
source selection, installation and packaging boundaries, checkout-independent
commands, failures, locks, path rejection, and self-update state. The
repository test runner selects it through `tests/test-impact.json`.
Development dependencies are declared in `scripts/pyproject.toml` and
resolved in `scripts/uv.lock`.

Unit tests use temporary wheel inputs and simulated deployment commands; they
do not install test versions of the manager. Real self-update and reconnection
are verified only during an explicitly requested manager update. Repository
validation does not change the installed manager or switch versions for tests.

Packaging uses [uv](https://docs.astral.sh/uv/pip/compile/) and the
[official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk).
Codex's [MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
defines project configuration and MCP tool allowlists.
