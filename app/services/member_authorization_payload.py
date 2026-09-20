"""Build the sub2api payload for one authorized Team member."""
from datetime import datetime, timezone


def build_member_export_payload(team, email, credentials, claims, identity):
    auth = claims.get("https://api.openai.com/auth") or identity.get(
        "https://api.openai.com/auth"
    ) or {}
    exported_credentials = {
        **credentials,
        "email": email,
        "chatgpt_account_id": team.account_id,
        "plan_type": "team",
        "expires_at": datetime.fromtimestamp(
            claims["exp"], timezone.utc
        ).isoformat().replace("+00:00", "Z"),
    }
    user_id = auth.get("chatgpt_user_id") or auth.get("user_id")
    if user_id:
        exported_credentials["chatgpt_user_id"] = user_id
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "proxies": [],
        "accounts": [{
            "name": f"{email} - {team.team_name or team.account_id}",
            "platform": "openai",
            "type": "oauth",
            "credentials": exported_credentials,
            "concurrency": 10,
            "priority": 1,
            "rate_multiplier": 1,
        }],
    }
