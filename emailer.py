#!/usr/bin/env python3
"""
emailer.py — SMTP slanje preko Gmail App Password (ili bilo kojeg drugog SMTP-a).
"""

import os
import smtplib
import ssl
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger("emailer")


def send_email(subject: str, html_body: str, to_addr: str, attachments: list | None = None):
    """
    Šalje HTML mail preko SMTP-a. Kredencijali iz env varijabli:
      SMTP_HOST      (npr. smtp.gmail.com)
      SMTP_PORT      (465 za SSL, 587 za STARTTLS)
      SMTP_USER      (full Gmail adresa)
      SMTP_PASS      (App Password — NE obična Gmail lozinka)
      SMTP_FROM      (obično ista kao SMTP_USER)
      SMTP_USE_TLS   (0 za SSL/465, 1 za STARTTLS/587; default 0)
    """
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", 465))
    user = os.environ["SMTP_USER"]
    pwd = os.environ["SMTP_PASS"]
    from_addr = os.environ.get("SMTP_FROM", user)
    use_tls = os.environ.get("SMTP_USE_TLS", "0") == "1"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    # Plain-text fallback za klijente bez HTML-a
    text_fallback = (
        "Ova poruka je HTML formatirana. Ako je vidiš kao tekst, otvori je u modernom klijentu."
    )
    msg.attach(MIMEText(text_fallback, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    ctx = ssl.create_default_context()
    if use_tls:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, pwd)
            s.send_message(msg)

    log.info(f"Email poslan: {subject} -> {to_addr}")
