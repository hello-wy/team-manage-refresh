"""Helpers for reading non-sensitive OpenAI auth session claims."""
import base64
import json
from typing import Any
from urllib.parse import unquote


def _decode_jwt_segment(segment: str) -> dict[str, Any]:
    try:
        padded = segment + "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:
        return {}


def auth_session_claims(session: Any) -> dict[str, Any]:
    try:
        cookie = unquote(str(session.cookies.get("oai-client-auth-session") or ""))
    except Exception:
        return {}
    segments = cookie.split(".")
    return _decode_jwt_segment(segments[1]) if len(segments) > 1 else {}
