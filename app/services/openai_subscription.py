"""Normalize personal subscription identifiers returned by authenticated APIs."""
import logging
import re

logger = logging.getLogger(__name__)

PLAN_FIELDS = (
    "personal_plan", "subscription_plan", "subscription_tier", "chatgpt_pro_plan_type",
    "chatgpt_plan_type", "plan_type", "planType", "plan", "rate_limit_tier",
)
PLAN_CONTAINERS = ("https://api.openai.com/auth", "entitlement", "subscription", "account")


def _plan_key(value):
    if not isinstance(value, str):
        return ""
    key = re.sub(r"[\s_$-]+", "", value.strip().lower())
    if key.startswith("chatgpt"):
        key = key[7:]
    if key.endswith("plan"):
        key = key[:-4]
    return key


def normalize_personal_plan(value):
    key = _plan_key(value)
    return {
        "free": "Free", "go": "Go", "plus": "Plus",
        "prolite": "Pro100", "pro": "Pro200", "promax": "Pro500",
        "pro100": "Pro100", "pro200": "Pro200", "pro500": "Pro500",
    }.get(key, "")


def personal_subscription(data):
    """Prefer saved/query metadata, refining a legacy generic Pro when possible."""
    if not isinstance(data, dict):
        return ""
    explicit = normalize_personal_plan(data.get("personal_plan"))
    if explicit:
        return explicit
    plans = [normalize_personal_plan(data.get(field)) for field in PLAN_FIELDS]
    plans.extend(personal_subscription(data[key]) for key in PLAN_CONTAINERS
                 if isinstance(data.get(key), dict))
    known = [plan for plan in plans if plan]
    if not known:
        return ""
    first_field = next((data[field] for field in PLAN_FIELDS
                        if normalize_personal_plan(data.get(field))), "")
    if _plan_key(first_field) == "pro":
        return next((plan for plan in known if plan in {"Pro100", "Pro500"}), "Pro200")
    return known[0]


def workspace_personal_subscription(scan):
    if not isinstance(scan, dict):
        return ""
    for workspace in scan.get("available_workspaces") or []:
        if isinstance(workspace, dict) and workspace.get("is_personal"):
            plan = personal_subscription(workspace)
            if plan:
                return plan
    return personal_subscription(scan)


async def query_personal_subscription(session, access_token, account_id=""):
    """Read the current plan using the same authenticated proxy session."""
    if not access_token:
        return ""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    try:
        response = await session.get(
            "https://chatgpt.com/backend-api/wham/usage", headers=headers, timeout=10,
        )
        if response.status_code == 200:
            return personal_subscription(response.json())
    except Exception:
        logger.info("个人订阅查询未完成，保留登录声明中的订阅类型")
    return ""
