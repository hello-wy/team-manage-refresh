"""Extract non-sensitive workspace state from OpenAI auth sessions and tokens."""
from __future__ import annotations

import base64
import json
from typing import Any

MAX_SCAN_DEPTH = 6
WORKSPACE_COLLECTION_KEYS = frozenset({"accounts", "workspaces", "organizations"})
CURRENT_ID_KEYS = (
    "account_id",
    "accountId",
    "chatgpt_account_id",
    "chatgptAccountId",
    "workspace_id",
    "workspaceId",
    "organization_id",
    "organizationId",
)
DEFAULT_MARKERS = (
    "is_default",
    "isDefault",
    "default",
    "is_current",
    "isCurrent",
    "current",
)


def inspect_workspace_claims(claims: Any) -> dict[str, Any]:
    data = claims if isinstance(claims, dict) else {}
    workspaces = _workspace_candidates(data)
    current_id = _explicit_workspace_id(data) or _default_workspace_id(workspaces)
    if not current_id and len(workspaces) == 1:
        current_id = workspaces[0]["id"]
    current = next((item for item in workspaces if item["id"] == current_id), {})
    status = _workspace_status(current_id, workspaces)
    return {
        "status": status,
        "workspace_id": current_id,
        "workspace_name": str(current.get("name") or ""),
        "available_workspaces": workspaces,
    }


def token_workspace_id(token: str) -> str:
    claims = _jwt_claims(token)
    auth = claims.get("https://api.openai.com/auth")
    if not isinstance(auth, dict):
        auth = {}
    return str(auth.get("chatgpt_account_id") or claims.get("chatgpt_account_id") or "").strip()


def token_claims(token: str) -> dict[str, Any]:
    return _jwt_claims(token)


def resolve_workspace_id(scan: dict[str, Any], requested_id: str = "") -> str:
    requested = str(requested_id or "").strip()
    if requested:
        return requested
    return str((scan or {}).get("workspace_id") or "").strip()


def _workspace_status(current_id: str, workspaces: list[dict[str, Any]]) -> str:
    if current_id:
        return "workspace_ok"
    if workspaces:
        return "workspace_ambiguous"
    return "no_workspace"


def _explicit_workspace_id(data: dict[str, Any]) -> str:
    for node in _identity_nodes(data):
        for key in CURRENT_ID_KEYS:
            value = str(node.get(key) or "").strip()
            if value:
                return value
    return ""


def _identity_nodes(data: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [data]
    auth = data.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        nodes.append(auth)
    for key in ("account", "session", "auth_session"):
        node = data.get(key)
        if isinstance(node, dict):
            nodes.append(node)
    return nodes


def _default_workspace_id(workspaces: list[dict[str, Any]]) -> str:
    defaults = [item["id"] for item in workspaces if item.get("is_default")]
    return defaults[0] if len(defaults) == 1 else ""


def _workspace_candidates(data: dict[str, Any]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    _visit_workspace_nodes(data, found, "", 0)
    return list(found.values())


def _visit_workspace_nodes(
    node: Any,
    found: dict[str, dict[str, Any]],
    context: str,
    depth: int,
) -> None:
    if depth > MAX_SCAN_DEPTH:
        return
    if isinstance(node, list):
        for child in node:
            _visit_workspace_nodes(child, found, context, depth + 1)
        return
    if not isinstance(node, dict):
        return
    _record_workspace(node, found, context)
    for key, child in node.items():
        next_context = key.lower() if key.lower() in WORKSPACE_COLLECTION_KEYS else context
        if key.lower() in {"access_token", "refresh_token", "id_token", "cookie"}:
            continue
        if next_context in WORKSPACE_COLLECTION_KEYS and isinstance(child, dict):
            _record_keyed_workspaces(child, found, next_context)
        _visit_workspace_nodes(child, found, next_context, depth + 1)


def _record_keyed_workspaces(
    collection: dict[str, Any],
    found: dict[str, dict[str, Any]],
    context: str,
) -> None:
    for workspace_id, value in collection.items():
        if not isinstance(value, dict):
            continue
        candidate = {**value, "id": value.get("id") or workspace_id}
        _record_workspace(candidate, found, context)


def _record_workspace(
    node: dict[str, Any],
    found: dict[str, dict[str, Any]],
    context: str,
) -> None:
    workspace_id = _node_workspace_id(node)
    if not workspace_id or not _has_workspace_shape(node, context):
        return
    current = found.get(workspace_id, {})
    found[workspace_id] = {
        "id": workspace_id,
        "name": str(node.get("name") or node.get("title") or current.get("name") or ""),
        "is_personal": bool(
            node.get("is_personal") or node.get("isPersonal") or node.get("personal")
        ),
        "is_default": bool(any(node.get(marker) for marker in DEFAULT_MARKERS)),
        "role": str(node.get("role") or node.get("account_user_role") or ""),
    }


def _node_workspace_id(node: dict[str, Any]) -> str:
    for key in ("id", *CURRENT_ID_KEYS):
        value = str(node.get(key) or "").strip()
        if value:
            return value
    return ""


def _has_workspace_shape(node: dict[str, Any], context: str) -> bool:
    if context in WORKSPACE_COLLECTION_KEYS:
        return True
    return any(
        key in node
        for key in (
            "workspace_id",
            "workspaceId",
            "account_id",
            "accountId",
            "plan_type",
            "planType",
            "account_user_role",
            "is_personal",
            "isPersonal",
        )
    )


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
        value = json.loads(decoded)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}
