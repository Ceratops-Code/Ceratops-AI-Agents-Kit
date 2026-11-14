# Install Action

## Goal

Install the selected repository's declared MCP server version after candidate
validation.

## Script Bundle

- Bind `<skill-root>` to this action's installed parent skill directory;
  require `<skill-root>/scripts/install-mcp-server.py` before invocation.
- The installed helper and manager use their installed lifecycle code and
  supporting modules. Source arguments select the code or packages to install,
  not replacement helper implementations.
- (D) Repository installation runs `python
  <skill-root>/scripts/install-mcp-server.py --repo-root <repo-root>
  [--source <mcp-server-source>] [--mcp-server-name <name>]
  [--package-wheel <wheel> --package-lock <lock>]
  [--evidence-output <caller-selected-file>]`. The optional output contains this
  installation's result, not a promotion-report binding.
- (D) A completed installation whose manager result was retained may replace
  installation inputs with `--manager-result <file>`. The helper checks the active
  installation and its receipt before returning completion; it never reinstalls.
  To report a client refresh, also pass `--client-refresh-result <file>`. Use the
  actual reload/status responses from the initiating Codex client, as below.

## SDLC execution

`sdlc/sdlc.yml` routes MCP server installation to this action; the manager
itself reads MCP server `pyproject.toml` and `mcp-server.json`, not SDLC.
Earlier SDLC bindings use `--source` to build an MCP server directly. SDLC
returns the selected MCP server and its package prerequisite records to this
action. For a package-backed MCP server, run the package's declared build
action and require its wheel validation to pass. Select exactly one wheel
matching its artifact directory and filename pattern. The MCP server source
must declare that package at the wheel's exact version. Pass the wheel and the
package's canonical lockfile to the manager; do not pass package source as MCP
server source. An MCP server without a package prerequisite keeps the
source-build path.

## Codex registration and client refresh

The installer reads the intended profile's `$CODEX_HOME/config.toml` before
installing. A missing file or server entry stops installation and returns the
exact registration to add. Existing entries are corrected to enable the selected
server's stable launcher in MCP mode while retaining unrelated settings. A failed
read or save stops before installation. Correct entries are not rewritten.
The manager's `versions <server-name>` response distinguishes a first install
(`installed_version: null`) from an update; both require that registration.

After installation, the returned `client_refresh.requests` identify the calls
for the initiating Codex client's app-server: `config/mcpServer/reload`, then
`mcpServerStatus/list` with the returned thread and server names. Use that live
client's API; starting another app-server cannot refresh the initiating chat.
If the refresh applies on the next turn, keep it pending until that turn.
If the client API is unavailable, report completed installation and pending chat
availability separately. Do not restart Codex or reinstall to finish this check.

For the completion call, save the returned `client_refresh.manager_result`
as the `--manager-result` input; this lets the same installer finish reporting
without installing again. The caller-owned refresh JSON file contains `thread_id`
from `CODEX_THREAD_ID`, `reload_completed` from the actual reload outcome,
`server_status` from the status response, and `expected_tools` from the
installed server's tool definitions. Use `deferred: true` only when the client
reported a next-turn refresh; preserve an available `error`. The helper requires
that same thread, initialized server capabilities and every expected tool.
Client failure does not turn successful installation into failed installation.
The caller removes the refresh input after consuming its result.

## Workflow

1. For repository installation, use the selected checkout's `pyproject.toml`
   name and version. If it declares several MCP servers, select by MCP server
   name. Run the installed action helper with that checkout as its working
   directory and supply `--source` with its repository or MCP server directory.
   For a package-backed MCP server,
   supply its already validated wheel and package lock as well:

   ```powershell
   python <skill-root>/scripts/install-mcp-server.py --repo-root <repo-root> --source <mcp-server-directory> [--mcp-server-name <name>] --package-wheel <package-wheel.whl> --package-lock <package-lockfile>
   ```

   Omit both package flags for a source-built MCP server without a package
   prerequisite. The manager builds only the selected MCP server source,
   registers the MCP server wheel with the supplied package wheel and locked
   third-party wheels, then installs that exact set. The helper checks the
   selected runtime metadata against the manager result and emits one
   `ceratops-deployment-completion.v1` receipt. It rejects ambiguous names
   before building and accepts no version override or artifact URL.
2. In an active Ceratops-AI-Agents-Kit checkout, install its manager source with
   `uv run --project scripts --locked python
   scripts/deploy-mcp-server-manager.py`; this supports both
   first installation and an existing manager. Other MCP servers use the
   installed CLI. If the manager is absent, follow bootstrap within the
   authorized scope.
   For an explicitly selected registered release, including a previous one,
   call MCP `install` with `mcp_server_name` and `version`. MCP accepts no build
   source, command, script, artifact URL, or output path. Refresh missing or
   outdated source locks only through the create action's explicit packaging
   step; repository installation never rewrites the lock.
3. Treat a failed candidate as an installation failure; report its error and
   preserve the active installation. Fix the owning source or release inputs
   before another attempt when the cause is deterministic.
4. Inspect versions after success and return the actual installation result,
   including manager maintenance metadata. Repository lifecycle consumes its
   requested installation's returned result and owns its own report cleanup;
   this installer neither receives a promotion report nor calls repo cleanup.
   The caller removes optional evidence output after use; the helper keeps only
   that current file. For the manager itself, finish the current request and
   reconnect to activate the selected version on the next launch.

## Completion Gate

The installed MCP server name and version match the selected source metadata or
the explicitly requested registered release. Report the initiating chat's
refreshed, pending or failed tool availability separately.

## Output Contract

Report installed MCP server name and version, pending or failed client refresh,
or the exact installation failure.
