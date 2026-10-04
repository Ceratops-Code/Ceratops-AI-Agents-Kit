# Install Action

## Goal

Install the selected repository's declared MCP server version after candidate
validation.

## Script Bundle

- Bind `<skill-root>` to the directory containing this action's parent
  `SKILL.md`; require `<skill-root>/scripts/install-mcp-server.py` before invocation.
- (D) Repository installation runs `python
  <skill-root>/scripts/install-mcp-server.py --repo-root <repo-root>
  [--source <mcp-server-source>] [--mcp-server-name <name>]
  [--package-wheel <wheel> --package-lock <lock>]`. For a saved promotion
  handoff, also pass `--promotion-result <record> --operation <location>` and
  optionally `--evidence-output <task-temp-file>`.
- (D) A completed installation whose manager result was retained may replace
  installation inputs with `--manager-result <file>` and the same promotion
  arguments. The helper must match the active selection and immutable receipt
  before emitting completion evidence; it never reinstalls for this recovery.

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

## Workflow

1. For repository installation, use the selected checkout's `pyproject.toml`
   name and version. If it declares several MCP servers, select by MCP server
   name. Run the action helper from that checkout and supply `--source` with
   its repository or MCP server directory. For a package-backed MCP server,
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
   `uv run --locked scripts/deploy-mcp-server-manager.py`; this supports both
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
4. Inspect versions after success. For a saved promotion, retain the helper's
   bound receipt and pass all completed handoff receipts together to the
   repository promotion finalizer. The caller removes the optional evidence
   output after finalization; the helper atomically keeps only that current
   file and no predecessors. For the manager itself, finish the current
   request and reconnect to activate the selected version on the next launch.

## Completion Gate

The installed MCP server name and version match the selected source metadata or
the explicitly requested registered release. Report reconnection separately.

## Output Contract

Report installed MCP server name and version, required reconnection, or the exact
failure.
