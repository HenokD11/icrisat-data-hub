"""Workflow emails (submitted / published / returned / access decisions).

Env vars (any SMTP relay — Office 365, SendGrid, Mailgun, Postmark...):
    SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM
    HUB_BASE_URL  public URL used in links, e.g. https://hub.example.org

No SMTP_HOST -> messages are only logged (local dev). Sending never blocks
or fails a request: it runs in a background thread and errors are logged.
"""

from __future__ import annotations

import logging
import os
import smtplib
import threading
from email.message import EmailMessage

log = logging.getLogger(__name__)


def link(path: str) -> str:
    return os.environ.get("HUB_BASE_URL", "http://localhost:8010").rstrip("/") + path


def send(to: list[str], subject: str, body: str) -> None:
    to = sorted({t for t in to if t})
    if not to:
        return
    host = os.environ.get("SMTP_HOST")
    if not host:
        log.info("email (not sent, no SMTP_HOST) to=%s subject=%s", to, subject)
        return
    threading.Thread(target=_send, args=(host, to, subject, body), daemon=True).start()


def _send(host: str, to: list[str], subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", "")
    msg["Bcc"] = ", ".join(to)  # recipients don't see each other
    msg["Subject"] = f"[ICRISAT Data Hub] {subject}"
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=30) as s:
            s.starttls()
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)
    except Exception:
        log.exception("email to %s failed", to)
