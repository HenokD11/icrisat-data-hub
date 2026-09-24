"""Sign-in: WorkOS AuthKit (production) or a dev email login (local only).

Production env vars:
    WORKOS_API_KEY     sk_... from the WorkOS dashboard
    WORKOS_CLIENT_ID   client_... from the WorkOS dashboard
    HUB_BASE_URL       e.g. https://hub.example.org — AuthKit redirects to
                       {HUB_BASE_URL}/auth/callback (add it in WorkOS -> Redirects)
    HUB_SECRET_KEY     long random string; signs the session cookie

AuthKit hosts the sign-in page and emails the one-time codes itself, so any
email address (ICRISAT staff or external partners) can sign in and no SMTP
is needed for login. Only *verified* emails are accepted.

Local development: set HUB_DEV_LOGIN=1 to get a plain "type your email"
form. It is refused whenever WorkOS is configured, and off by default.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib import parse
from urllib import request as urlrequest

SESSION_COOKIE = "hub_session"
STATE_COOKIE = "hub_login_state"
MAX_AGE = 7 * 24 * 3600
WORKOS_API = "https://api.workos.com/user_management"


class Auth:
    def __init__(self) -> None:
        self.secret = (os.environ.get("HUB_SECRET_KEY") or secrets.token_hex(32)).encode()
        self.api_key = os.environ.get("WORKOS_API_KEY", "")
        self.client_id = os.environ.get("WORKOS_CLIENT_ID", "")
        self.base_url = os.environ.get("HUB_BASE_URL", "").rstrip("/")
        self.workos = bool(self.api_key and self.client_id)
        self.dev = os.environ.get("HUB_DEV_LOGIN") == "1" and not self.workos
        self.secure_cookies = self.base_url.startswith("https://")

    # ------------------------------------------------------------ cookies
    def sign(self, data: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        sig = hmac.new(self.secret, raw.encode(), hashlib.sha256).hexdigest()
        return f"{raw}.{sig}"

    def unsign(self, token: str | None, max_age: int = MAX_AGE) -> dict | None:
        if not token or "." not in token:
            return None
        raw, sig = token.rsplit(".", 1)
        good = hmac.new(self.secret, raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(raw))
        except ValueError:
            return None
        if time.time() - data.get("iat", 0) > max_age:
            return None
        return data

    def user(self, request) -> dict | None:
        data = self.unsign(request.cookies.get(SESSION_COOKIE))
        return data if data and data.get("email") else None

    def login(self, response, email: str, name: str = "") -> None:
        token = self.sign({"email": email.strip().lower(), "name": name.strip(), "iat": time.time()})
        response.set_cookie(SESSION_COOKIE, token, max_age=MAX_AGE, httponly=True,
                            samesite="lax", secure=self.secure_cookies)

    def logout(self, response) -> None:
        response.delete_cookie(SESSION_COOKIE)

    # ------------------------------------------------------------- WorkOS
    def authorize_url(self, request, next_path: str) -> tuple[str, str]:
        """Returns (url, signed state cookie value)."""
        nonce = secrets.token_urlsafe(24)
        query = parse.urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri(request),
            "response_type": "code",
            "provider": "authkit",
            "state": nonce,
        })
        return f"{WORKOS_API}/authorize?{query}", self.sign(
            {"nonce": nonce, "next": next_path, "iat": time.time()})

    def redirect_uri(self, request) -> str:
        return (self.base_url or str(request.base_url).rstrip("/")) + "/auth/callback"

    def exchange(self, code: str) -> dict:
        """Trade the callback code for the WorkOS user (blocking HTTP call)."""
        body = json.dumps({
            "client_id": self.client_id,
            "client_secret": self.api_key,
            "grant_type": "authorization_code",
            "code": code,
        }).encode()
        req = urlrequest.Request(f"{WORKOS_API}/authenticate", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
        with urlrequest.urlopen(req, timeout=20) as r:
            return json.load(r)["user"]


def safe_next(path: str | None) -> str:
    """Only same-site relative paths — no open redirects."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path
