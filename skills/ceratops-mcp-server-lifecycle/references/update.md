# Update Action

## Goal

Select an exact release for an already installed MCP server.

## Workflow

1. For an update from a repository, use the install action to derive its name
   and version from that checkout. Otherwise capture the installed MCP server name
   and explicitly selected registered release. If absent, use install; if the
   release is unregistered, package its reviewed source through create.
2. Call MCP `update` with `mcp_server_name` and `version`, or run:

   ```powershell
   C:\AI-Agents-MCP-Servers\ceratops_mcp_server_manager\bin\ceratops_mcp_server_manager.cmd update <mcp-server-name> <version>
   ```

   Explicitly selecting a previous release uses this same operation.
3. On failure, report the error and leave the active selection intact. On
   success, inspect versions. Existing process files remain available in their
   immutable installation directories; deployment does not terminate processes.
4. For a self-update, complete the current request normally and reconnect for
   the selected manager to run. Report both versions while they differ.

## Completion Gate

The selected installed version matches the request; any running manager
difference and retained process installations are accurately reported.

## Output Contract

Report installed version, running manager difference, and reconnection needs.
