"""Check deployment receipts consumed by repository lifecycle boundaries.

This module owns the closed receipt shape and entity checks. It has no deployment,
publication or record-deletion effects; each caller supplies its boundary
policies and performs cleanup only after the receipt passes.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Callable
from typing import Any


def check_deployment_receipt(
    receipt: object,
    *,
    repo_root: pathlib.Path,
    commit: str,
    outcome: dict[str, Any],
    error_type: type[RuntimeError],
    check_destination_path: Callable[[pathlib.Path], pathlib.Path],
) -> None:
    """Check closed completion evidence without scanning or deploying installed files.

    Callers supply the actual installer's returned result. The return receiver
    checks the current registered operation before consuming its temporary
    input. The caller owns its error class and destination-path guard.
    """
    fields = {
        "schema",
        "producer",
        "status",
        "repo_root",
        "commit",
        "install_root",
        "deployed",
        "removed",
        "transaction_id",
        "cleanup_debt",
    }
    if (
        not isinstance(receipt, dict)
        or not fields.issubset(receipt)
        or set(receipt) - fields - {"maintenance", "client_refresh"}
        or receipt.get("schema") != "ceratops-deployment-completion.v1"
        or receipt.get("status") != "completed"
        or receipt.get("cleanup_debt") != []
    ):
        raise error_type(
            "Deployment completion evidence is failed, incomplete, or has cleanup debt."
        )
    if receipt["commit"] != commit or receipt["repo_root"] != str(repo_root):
        raise error_type(
            "Deployment completion evidence identifies a different repository or commit."
        )
    if receipt["producer"] != outcome.get("handoff"):
        raise error_type(
            "Deployment completion evidence identifies a different installer."
        )
    if "maintenance" in receipt:
        maintenance = receipt["maintenance"]
        if (not isinstance(maintenance, dict)
                or set(maintenance) != {"removed", "deferred", "errors"}
                or any(not isinstance(items, list) or any(not isinstance(item, str) for item in items)
                       for items in maintenance.values())
                or maintenance["errors"] != receipt["cleanup_debt"]):
            raise error_type("Deployment maintenance metadata is incomplete.")
    destination = receipt["install_root"]
    if not isinstance(destination, str) or not pathlib.Path(destination).is_absolute():
        raise error_type(
            "Deployment completion evidence lacks its installation destination."
        )
    if str(check_destination_path(pathlib.Path(destination))) != destination:
        raise error_type("Deployment completion destination is not canonical.")
    identity_pattern = (
        r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*"
        if receipt["producer"] == "ceratops-mcp-server-lifecycle/install"
        else r"[a-z0-9]+(?:-[a-z0-9]+)*"
    )
    names: list[str] = []
    for field in ("deployed", "removed"):
        values = receipt[field]
        if not isinstance(values, list) or not all(
            isinstance(name, str) and re.fullmatch(identity_pattern, name)
            for name in values
        ):
            raise error_type(
                "Deployment completion evidence has invalid deployment identities."
            )
        names.extend(values)
    if not names or len(set(names)) != len(names):
        raise error_type(
            "Deployment completion evidence has missing or duplicate deployment identities."
        )
    if "client_refresh" in receipt:
        refresh = receipt["client_refresh"]
        if (receipt["producer"] != "ceratops-mcp-server-lifecycle/install"
                or not isinstance(refresh, dict)
                or refresh.get("status") not in {"pending", "completed", "failed"}
                or refresh.get("server_name") not in receipt["deployed"]):
            raise error_type("MCP client availability result does not identify the installed server.")
        # Availability belongs to the initiating client. A pending/failed reload
        # must never undo or repeat the completed installation.
    if not isinstance(receipt["transaction_id"], str) or not re.fullmatch(
        r"[0-9a-f]{32}", receipt["transaction_id"]
    ):
        raise error_type(
            "Deployment completion evidence lacks a transaction identity."
        )
