"""Low-level helpers for OpenAI OAuth responses and auth pages."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from app.services.openai_workspace import inspect_workspace_claims

AUTH_ORIGIN = "https://auth.openai.com"
CALLBACK_HOST = "localhost:1455"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ENQUEUE_PATTERN = re.compile(
    r'streamController\.enqueue\(("(?:\\.|[^"\\])*")\)',
    re.DOTALL,
)


def json_body(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def next_url(response: Any) -> str:
    payload = json_body(response)
    value = payload.get("continue_url") or payload.get("url")
    value = value or response.headers.get("location") or response.headers.get("Location")
    return urljoin(AUTH_ORIGIN, str(value)) if value else ""


def is_callback(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    query = parse_qs(parsed.query)
    return parsed.netloc == CALLBACK_HOST and bool(query.get("code"))


def auth_headers(
    device_id: str,
    referer: str,
    *,
    json_request: bool = False,
    sentinel_token: str = "",
) -> dict[str, str]:
    headers = {
        "Accept": "application/json" if json_request else "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": AUTH_ORIGIN,
        "Referer": referer or AUTH_ORIGIN,
        "oai-device-id": device_id,
    }
    if json_request:
        headers["Content-Type"] = "application/json"
    if sentinel_token:
        headers["openai-sentinel-token"] = sentinel_token
    return headers


def mfa_factor_id(payload: dict[str, Any]) -> str:
    session = payload.get("oai-client-auth-session")
    if not isinstance(session, dict):
        session = {}
    factors: list[dict[str, Any]] = []
    for key in ("mfa_challenge_factors", "mfa_factors"):
        values = session.get(key)
        if isinstance(values, list):
            factors.extend(item for item in values if isinstance(item, dict))
    for factor in factors:
        if str(factor.get("factor_type") or "").lower() == "totp":
            return str(factor.get("id") or "").strip()
    return ""


def is_mfa_challenge(payload: dict[str, Any], target_url: str) -> bool:
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    return (
        str(page.get("type") or "").lower() == "mfa_challenge"
        or "/mfa-challenge/" in target_url.lower()
    )


def inspect_auth_page_workspace(html: str) -> dict[str, Any]:
    empty = inspect_workspace_claims({})
    for serialized in _serialized_documents(html):
        document = _decode_document(serialized)
        scan = inspect_workspace_claims(document)
        if scan.get("available_workspaces"):
            return scan
        empty = scan
    return empty


def _serialized_documents(html: str) -> list[str]:
    documents = []
    for match in _ENQUEUE_PATTERN.finditer(str(html or "")):
        try:
            documents.append(json.loads(match.group(1)))
        except (TypeError, ValueError):
            continue
    return documents


def _decode_document(serialized: str) -> Any:
    try:
        values = json.loads(serialized)
    except (TypeError, ValueError):
        return {}
    if not isinstance(values, list) or not values:
        return values if isinstance(values, dict) else {}
    return _decode_reference(values, 0, {})


def _decode_reference(values: list[Any], index: Any, memo: dict[int, Any]) -> Any:
    if not isinstance(index, int) or index < 0 or index >= len(values):
        return None
    if index in memo:
        return memo[index]
    node = values[index]
    if isinstance(node, list):
        output: list[Any] = []
        memo[index] = output
        output.extend(_decode_reference(values, item, memo) for item in node)
        return output
    if not isinstance(node, dict):
        return node
    output_dict: dict[str, Any] = {}
    memo[index] = output_dict
    for key_reference, value_reference in node.items():
        key = _decode_key(values, key_reference)
        if key:
            output_dict[key] = _decode_reference(values, value_reference, memo)
    return output_dict


def _decode_key(values: list[Any], key_reference: Any) -> str:
    text = str(key_reference or "")
    if text.startswith("_") and text[1:].isdigit():
        index = int(text[1:])
        if 0 <= index < len(values):
            return str(values[index] or "")
    return text
