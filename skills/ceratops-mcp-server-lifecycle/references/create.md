# Create Action

## Goal

Create an MCP server's editable source and a reproducible release in its owning
repository.

## Workflow

1. Establish the requested behavior and source owner. Use the ordinary coding
   environment and maintained packaging dependencies. Keep repository creation
   or Git release work in `ceratops-repo-lifecycle` when required.
2. Declare the MCP server name and exact numeric `major.minor.patch` version in
   `pyproject.toml` with a pinned wheel build backend. Add the schema-2
   `mcp-server.json` readiness-module contract documented in the manager README;
   it must not duplicate the project name or version. The supported runtime is
   global Windows x64 CPython 3.14; other formats require manager development.
3. Implement the module's fixed `--deployment-check` readiness protocol. Its
   JSON must report exact MCP server identity and installed package version with
   `ready: true`; check required dependencies without modifying user data.
4. Add focused behavioral tests and usage documentation in the owning repo.
   Define every public structured MCP tool parameter from the server's
   canonical JSON Schema. The actual MCP `list_tools` result must expose that
   complete nested schema; an unqualified `{"type":"object"}` placeholder is
   invalid. An intentionally open-ended map may remain opaque only when the
   canonical deployment contract names that tool parameter in its opaque-map
   allowlist. Make `--deployment-check` publish the canonical tool input
   schemas and opaque-map allowlist; the manager must compare them with a real
   MCP `list_tools` response before accepting the candidate. Lifecycle tests
   must exercise the production server's `list_tools` result and compare each
   structured parameter with its canonical schema. The manager's `package`
   operation must install the built wheel set in an isolated candidate
   environment and validate the exact deployment contract before committing
   registry state. A failed preflight must leave the registry unchanged.
5. Use the installed manager's public CLI:

   ```powershell
   %USERPROFILE%\.codex\mcp\ceratops_mcp_server_manager\bin\ceratops_mcp_server_manager.cmd package --source <mcp-server-source> --lock
   %USERPROFILE%\.codex\mcp\ceratops_mcp_server_manager\bin\ceratops_mcp_server_manager.cmd package --source <mcp-server-source>
   ```

   Review `pylock.toml` between these commands. The first records locked
   dependencies; the second builds and registers that exact MCP server without
   activating it. For an MCP server declaring a separate SDLC package prerequisite,
   build and validate that package wheel first; register the MCP server with
   `package --source <mcp-server-source> --package-wheel <wheel> --package-lock
   <package-lockfile>`. Keep the package source and canonical lockfile in its package
   directory. A Ceratops-AI-Agents-Kit checkout is not required. If the manager
   is absent, use bootstrap only when first installation is authorized.
6. Hand authorized deployment to this skill's install action. Use a new version
   when artifact contents change; a published identity/version is immutable.

## Completion Gate

Source, manifest, packaging, focused tests, and documentation agree; the
registered artifact is exact. Report an installation separately when requested.

## Output Contract

Report the source owner, release version, deployment outcome, and blockers.
