"""OpenAI Sentinel proof issuance for password authentication."""
import asyncio
import base64
import hashlib
import json
import os
import struct
from typing import Any

SENTINEL_FLOW = "username_password_create"
SENTINEL_REQUEST_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
DEFAULT_SENTINEL_VERSION = "20260219f9f6"
MAX_POW_NONCE = 500_000
SENTINEL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/110.0.0.0 Safari/537.36"
)


class OpenAISentinelError(RuntimeError):
    pass


def _sentinel_version() -> str:
    value = str(os.getenv("OPENAI_SENTINEL_VERSION") or DEFAULT_SENTINEL_VERSION)
    if value and all(char.isalnum() or char in {"-", "_"} for char in value):
        return value
    raise OpenAISentinelError("sentinel_version_invalid")


def _solve_pow(seed: str, difficulty_hex: str) -> str:
    try:
        difficulty = int(difficulty_hex, 16)
    except (TypeError, ValueError) as exc:
        raise OpenAISentinelError("sentinel_pow_invalid") from exc
    prefix_length = (len(difficulty_hex) + 1) // 2
    for nonce in range(MAX_POW_NONCE):
        digest = hashlib.sha3_512(f"{seed}{nonce}".encode()).digest()
        if int.from_bytes(digest[:prefix_length], "big") <= difficulty:
            return base64.b64encode(struct.pack(">Q", nonce)).decode()
    raise OpenAISentinelError("sentinel_pow_unsolved")


def _challenge_payload(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise OpenAISentinelError("sentinel_response_invalid") from exc
    if not isinstance(payload, dict) or not str(payload.get("token") or ""):
        raise OpenAISentinelError("sentinel_response_incomplete")
    return payload


async def issue_sentinel_token(session: Any, device_id: str) -> str:
    response = await session.post(
        SENTINEL_REQUEST_URL,
        data=json.dumps({"p": "", "id": device_id, "flow": SENTINEL_FLOW}),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Accept": "*/*",
            "Origin": "https://sentinel.openai.com",
            "Referer": (
                "https://sentinel.openai.com/backend-api/sentinel/"
                f"frame.html?sv={_sentinel_version()}"
            ),
            "User-Agent": SENTINEL_USER_AGENT,
        },
        allow_redirects=False,
    )
    if response.status_code != 200:
        raise OpenAISentinelError(f"sentinel_http_{response.status_code}")
    payload = _challenge_payload(response)
    proof = payload.get("proofofwork")
    proof = proof if isinstance(proof, dict) else {}
    pow_value = ""
    if proof.get("required"):
        pow_value = await asyncio.to_thread(
            _solve_pow,
            str(proof.get("seed") or ""),
            str(proof.get("difficulty") or ""),
        )
    return json.dumps({
        "p": payload.get("p", ""),
        "t": pow_value,
        "c": payload["token"],
        "id": device_id,
        "flow": SENTINEL_FLOW,
    }, separators=(",", ":"))
