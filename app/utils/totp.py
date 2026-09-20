"""RFC 6238 TOTP generation for server-side automatic login."""
import base64
import hashlib
import hmac
import struct
import time
from urllib.parse import parse_qs, urlparse

DEFAULT_DIGITS = 6
DEFAULT_PERIOD_SECONDS = 30
BASE32_BLOCK_SIZE = 8


class TotpError(ValueError):
    pass


def normalize_totp_secret(value: str) -> str:
    raw = str(value or "").strip()
    if raw.lower().startswith("otpauth://"):
        raw = (parse_qs(urlparse(raw).query).get("secret") or [""])[0]
    return raw.upper().replace(" ", "").replace("-", "").rstrip("=")


def _decode_secret(secret: str) -> bytes:
    normalized = normalize_totp_secret(secret)
    if not normalized:
        raise TotpError("2FA 密钥为空")
    padding = "=" * (-len(normalized) % BASE32_BLOCK_SIZE)
    try:
        return base64.b32decode(normalized + padding, casefold=True)
    except Exception as exc:
        raise TotpError("2FA 密钥不是有效的 Base32 格式") from exc


def generate_totp(
    secret: str,
    *,
    timestamp: float | None = None,
    period_seconds: int = DEFAULT_PERIOD_SECONDS,
    digits: int = DEFAULT_DIGITS,
) -> str:
    if period_seconds <= 0 or digits <= 0:
        raise TotpError("TOTP 参数无效")
    current_time = time.time() if timestamp is None else timestamp
    counter = int(current_time // period_seconds)
    digest = hmac.new(
        _decode_secret(secret), struct.pack(">Q", counter), hashlib.sha1
    ).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)
