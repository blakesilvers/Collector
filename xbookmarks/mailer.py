"""Email the report to yourself (Gmail works with an App Password)."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

PASSWORD_ENV = "XBOOKMARKS_SMTP_PASSWORD"


def send(cfg: dict, subject: str, text: str, html_body: str | None = None,
         attachments: list[Path] | None = None) -> None:
    password = os.environ.get(PASSWORD_ENV) or cfg.get("password", "")
    if not password:
        raise RuntimeError(f"No SMTP password: set the {PASSWORD_ENV} environment variable "
                           "(for Gmail, an App Password from myaccount.google.com/apppasswords).")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("from") or cfg["username"]
    msg["To"] = cfg.get("to") or cfg["username"]
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    for path in attachments or []:
        msg.add_attachment(path.read_bytes(), maintype="application", subtype="json", filename=path.name)
    host, port = cfg.get("smtp_host", "smtp.gmail.com"), int(cfg.get("smtp_port", 465))
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=60) as s:
            s.login(cfg["username"], password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls()
            s.login(cfg["username"], password)
            s.send_message(msg)
