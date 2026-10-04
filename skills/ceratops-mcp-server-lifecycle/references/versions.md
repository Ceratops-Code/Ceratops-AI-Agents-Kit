# Versions Action

## Goal

Inspect exact installed and registered versions without executing MCP server code.

## Workflow

1. Call MCP `versions` with optional `mcp_server_name`, or run:

   ```powershell
   %USERPROFILE%\.codex\mcp\ceratops_mcp_server_manager\bin\ceratops_mcp_server_manager.cmd versions [mcp-server-name]
   ```

   Omitting the MCP server name inspects the deployment manager.
2. Interpret `installed_version` as the version selected for the next launch,
   `available_versions` as registered releases, and `running_version` as the
   responding manager process version. Other MCP servers have null running versions;
   the manager does not supervise their processes.
3. Report `reconnection_required` when true. Registration and callable Codex
   availability require direct connection evidence, separate from version data.

## Completion Gate

The response describes only checked state and requests no unneeded mutation.

## Output Contract

Report the requested versions and any required reconnection or corrupt state.
