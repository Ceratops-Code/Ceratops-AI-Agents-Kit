"""Check deployment receipts consumed by repository promotion finalization.

This module owns the closed receipt shape and entity checks. It has no deployment,
publication or record-deletion effects; promotion supplies its existing boundary
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
    binding: dict[str, Any] | None,
    error_type: type[RuntimeError],
    read_promotion_bindings: Callable[[dict[str, Any]], list[dict[str, Any]]],
    check_destination_path: Callable[[pathlib.Path], pathlib.Path],
) -> None:
    """Check closed completion evidence without scanning or deploying installed files.

    The producer attests its actual transaction. An external handoff receipt
    must bind the exact saved promotion bytes; inline executed-action evidence
    is already held by the lifecycle envelope. Neither path replays deployment.
    The caller owns its error class, record-binding parser and linked-path guard;
    injecting them preserves those boundaries without a circular import.
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
        "promotion",
    }
    if (
        not isinstance(receipt, dict)
        or not fields.issubset(receipt)
        or set(receipt) - fields - {"promotion_cleanup"}
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
    promotion_matches = receipt["promotion"] is None and binding is None
    if binding is not None:
        promotion_matches = binding in read_promotion_bindings(receipt)
    if receipt["producer"] != outcome.get("handoff") or not promotion_matches:
        raise error_type(
            "Deployment completion evidence does not match this handoff and saved record."
        )
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
    if not isinstance(receipt["transaction_id"], str) or not re.fullmatch(
        r"[0-9a-f]{32}", receipt["transaction_id"]
    ):
        raise error_type(
            "Deployment completion evidence lacks a transaction identity."
        )
