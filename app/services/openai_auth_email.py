"""Distinguish an email login option from required post-auth verification."""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from app.services.openai_auth_protocol import AUTH_ORIGIN, inspect_auth_page_session
from app.services.openai_auth_session import auth_session_claims

PASSWORD_URL = f"{AUTH_ORIGIN}/log-in/password"


def is_email_verification(url):
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.netloc == "auth.openai.com"
            and parsed.path.rstrip("/") == "/email-verification")


def is_password_login(url):
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.netloc == "auth.openai.com"
            and parsed.path.rstrip("/") == "/log-in/password")


class _PasswordLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.available = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href and is_password_login(urljoin(AUTH_ORIGIN, href)):
                self.available = True


def email_login_options(session, response):
    html = str(getattr(response, "text", "") or "")
    context = {**auth_session_claims(session), **inspect_auth_page_session(html)}
    raw_mode = context.get("email_verification_mode")
    # Keep logging bounded to known values; never emit cookies or page payloads.
    mode = raw_mode if isinstance(raw_mode, str) and raw_mode in {
        "passwordless_login", "passwordless_signup", "post_auth_email_verification",
        "onboarding", "post_signup", "resurrection_login",
    } else "unknown"
    if mode not in {"passwordless_login", "unknown"}:
        return mode, False
    if context.get("reauth") or context.get("passwordless_otp_from_password_redirect"):
        return mode, False
    methods = context.get("login_methods")
    if isinstance(methods, list):
        return mode, mode == "passwordless_login" and any(
            isinstance(method, dict) and method.get("type") == "password"
            for method in methods
        )
    parser = _PasswordLinkParser()
    parser.feed(html)
    return mode, parser.available
