# Bootstrap Action

## Goal

Install the first manager using the same engine as its CLI and MCP interfaces.

## Workflow

1. Require first-install authorization and an active Ceratops-AI-Agents-Kit source
   checkout. From that checkout run
   `uv run --locked scripts/deploy-mcp-server-manager.py`.
   This validates existing global CPython 3.14 and uv 0.12.10 or newer 0.12.x,
   provisions locked libraries in disposable storage, packages the first release
   using the manager's own code, and validates its isolated installation inside
   `C:\AI-Agents-MCP-Servers\ceratops_mcp_server_manager`. It installs no
   Python or uv copy and changes no global Python libraries.
2. Inspect the manager with its CLI:

   ```powershell
   C:\AI-Agents-MCP-Servers\ceratops_mcp_server_manager\bin\ceratops_mcp_server_manager.cmd versions
   ```

3. When registration is requested, register only this stdio service in the
   intended coding agent's Codex configuration. Use global `python` with
   `-I -B`, the manager folder's `bin/ceratops_mcp_server_manager.py`
   launcher, and `--mcp`; set the tool allowlist to `install`, `update`,
   and `versions`.
   Verify the restricted Forms agent cannot inherit this service before
   enabling any shared registration. Do not restart the desktop app.
4. Verify tools through a real local MCP connection. Report whether the active
   Codex task has reconnected and exposes them; a CLI test alone is
   insufficient.

## Completion Gate

The first installation passed readiness. Any requested registration is scoped
and verified, or is reported blocked with the installed CLI still available.

## Output Contract

Report installed version, callable MCP availability, and required reconnection.
