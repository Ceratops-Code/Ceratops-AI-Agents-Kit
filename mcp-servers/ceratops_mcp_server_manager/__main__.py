"""Private transport/readiness switches leave the deployment operation set closed."""

import json
import sys

from . import MCP_SERVER_NAME, __version__

if sys.argv[1:] == ["--deployment-check"]:
    from .server import build_server

    build_server()
    print(json.dumps({"mcp_server_id": MCP_SERVER_NAME, "version": __version__, "ready": True}))
elif sys.argv[1:] == ["--mcp"]:
    from .server import build_server

    build_server().run(transport="stdio")
else:
    from .cli import main

    raise SystemExit(main())
