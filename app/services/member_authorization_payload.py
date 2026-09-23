"""Build sub2api payloads for authorized OpenAI accounts."""
from datetime import datetime, timezone


def build_member_export_payload(team, email, credentials, claims, identity):
    return build_openai_export_payload(
        email=email,
        account_id=team.account_id,
        credentials=credentials,
        claims=claims,
        identity=identity,
        plan_type="team",
    )


def build_openai_export_payload(
    *, email, account_id, credentials, claims, identity, plan_type,
):
    auth = claims.get("https://api.openai.com/auth") or identity.get(
        "https://api.openai.com/auth"
    ) or {}
    exported_credentials = {
        **credentials,
        "email": email,
        "plan_type": plan_type,
        "expires_at": datetime.fromtimestamp(
            claims["exp"], timezone.utc
        ).isoformat().replace("+00:00", "Z"),
    }
    if account_id:
        exported_credentials["chatgpt_account_id"] = account_id
    user_id = auth.get("chatgpt_user_id") or auth.get("user_id")
    if user_id:
        exported_credentials["chatgpt_user_id"] = user_id
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "proxies": [],
        "accounts": [{
            "name": email,
            "platform": "openai",
            "type": "oauth",
            "credentials": exported_credentials,
            "concurrency": 10,
            "priority": 1,
            "rate_multiplier": 1,
        }],
    }
