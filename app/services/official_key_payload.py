"""Build API Key accounts from the live Sub2API template, without OAuth overrides."""
from copy import deepcopy
from datetime import datetime

from app.services.sub2api import Sub2apiError

TEMPLATE_NAME = "官key"
GROUP_ID = 56
ACCOUNT_FIELDS = (
    "notes", "proxy_id", "concurrency", "priority", "rate_multiplier",
    "load_factor", "auto_pause_on_expired",
)
# These values describe the template's usage/probes rather than its configuration.
RUNTIME_EXTRA_FIELDS = frozenset({
    "upstream_billing_probe", "upstream_model_metadata", "openai_responses_probe",
    "duplicate_source_account_id", "duplicate_operation_id", "quota_used", "quota_daily_used",
    "quota_weekly_used", "quota_daily_start", "quota_weekly_start", "quota_daily_reset_at",
    "quota_weekly_reset_at", "openai_responses_supported", "openai_compact_supported",
    "openai_compact_checked_at", "openai_compact_last_status", "openai_compact_last_error",
})


def only_match(items, *, name, kind):
    if not all(isinstance(item, dict) for item in items):
        raise Sub2apiError("Sub2API 配置列表格式错误")
    matches = [item for item in items if item.get("name") == name]
    if len(matches) != 1:
        raise Sub2apiError(f"Sub2API 中需要唯一的{kind}「{name}」，当前匹配 {len(matches)} 个")
    return matches[0]


def build_payload(template, *, entry, group_id):
    if not isinstance(template, dict):
        raise Sub2apiError("Sub2API「官key」模板配置格式错误")
    if template.get("name") != TEMPLATE_NAME:
        raise Sub2apiError("Sub2API「官key」模板名称已变更，请重新核对")
    if template.get("platform") != "openai" or template.get("type") != "apikey":
        raise Sub2apiError("Sub2API「官key」模板必须是 OpenAI API Key 账号")
    credentials = template.get("credentials")
    extra = template.get("extra") or {}
    if not isinstance(credentials, dict) or not isinstance(extra, dict):
        raise Sub2apiError("Sub2API「官key」模板配置格式错误")
    if not credentials.get("base_url"):
        raise Sub2apiError("Sub2API「官key」模板缺少接口地址")
    payload = {key: deepcopy(template[key]) for key in ACCOUNT_FIELDS if key in template}
    payload.update({
        "name": f"官key {entry['account']}", "platform": "openai", "type": "apikey",
        "credentials": {**deepcopy(credentials), "api_key": entry["api_key"]},
        "extra": {key: deepcopy(value) for key, value in extra.items()
                  if key not in RUNTIME_EXTRA_FIELDS},
        "group_ids": [group_id],
    })
    expires_at = template.get("expires_at")
    payload["expires_at"] = int(datetime.fromisoformat(
        expires_at.replace("Z", "+00:00")
    ).timestamp()) if expires_at else None
    if "upstream_billing_probe_enabled" in extra:
        payload["upstream_billing_probe_enabled"] = extra["upstream_billing_probe_enabled"]
    return payload
