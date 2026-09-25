"""Parse the quota windows returned by ChatGPT's wham/usage endpoint."""
from __future__ import annotations

import json
import math
from typing import Any

FIVE_HOURS_SECONDS = 5 * 60 * 60
SEVEN_DAYS_SECONDS = 7 * 24 * 60 * 60
PERCENT_LIMIT = 100
WINDOW_SECONDS = {FIVE_HOURS_SECONDS: "5h", SEVEN_DAYS_SECONDS: "1week"}
ALIASES = {
    "5h": ("5h", "300min", "five_hours", "short"),
    "1week": ("7d", "10080min", "seven_days", "weekly", "long", "1week"),
}
NUMERIC_FIELDS = {
    "used": ("used", "num_tokens_used", "tokens_used", "consumed"),
    "limit": ("limit", "num_tokens_limit", "tokens_limit", "max", "cap"),
    "remaining": ("remaining", "num_tokens_remaining", "tokens_remaining", "available"),
}
RESET_FIELDS = ("resets_at", "reset_at", "reset_time", "expires_at")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _field(window: dict[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        value = _number(window.get(name))
        if value is not None:
            return value
    return None


def _reset_at(window: dict[str, Any]) -> str | None:
    return next((str(window[key]) for key in RESET_FIELDS if window.get(key) is not None), None)


def _result(used: float, limit: float, remaining: float | None,
            reset_at: str | None) -> dict[str, Any]:
    return {
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "reset_at": reset_at,
        "state": "unknown" if remaining is None else (
            "exhausted" if remaining <= 0 else "available"
        ),
    }


def _percentage_window(window: dict[str, Any]) -> dict[str, Any] | None:
    percent = _number(window.get("used_percent"))
    if percent is None or not 0 <= percent <= PERCENT_LIMIT:
        return None
    return _result(percent, PERCENT_LIMIT, PERCENT_LIMIT - percent, _reset_at(window))


def _numeric_window(window: dict[str, Any]) -> dict[str, Any] | None:
    values = {name: _field(window, fields) for name, fields in NUMERIC_FIELDS.items()}
    used, limit, remaining = values["used"], values["limit"], values["remaining"]
    if used is None and limit is None and remaining is None:
        return None
    if remaining is None and used is not None and limit is not None:
        remaining = max(0, limit - used)
    if used is None and remaining is not None and limit is not None:
        used = max(0, limit - remaining)
    if limit is None and used is not None and remaining is not None:
        limit = used + remaining
    return _result(used or 0, limit or 0, remaining, _reset_at(window))


def _structured_windows(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rate_limit = body.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return {}
    parsed = {}
    for name in ("primary_window", "secondary_window"):
        window = rate_limit.get(name)
        if not isinstance(window, dict):
            continue
        duration = _number(window.get("limit_window_seconds"))
        key = WINDOW_SECONDS.get(duration)
        quota = _percentage_window(window)
        if key and quota:
            parsed[key] = quota
    return parsed


def _alias_windows(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    containers = [body]
    containers.extend(value for key in ("usage", "rate_limits", "limits", "rate_limits_info")
                      if isinstance((value := body.get(key)), dict))
    parsed = {}
    for key, aliases in ALIASES.items():
        for container in containers:
            window = next((container[name] for name in aliases
                           if isinstance(container.get(name), dict)), None)
            if window is None:
                continue
            quota = _percentage_window(window) if "used_percent" in window else _numeric_window(window)
            if quota is not None:
                parsed[key] = quota
                break
    return parsed


def parse_usage(body: Any) -> dict[str, Any] | None:
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return None
    if not isinstance(body, dict):
        return None
    windows = _alias_windows(body)
    windows.update(_structured_windows(body))
    if not windows:
        return None
    return {key: windows.get(key) for key in ("5h", "1week")}
