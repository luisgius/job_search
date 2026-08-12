"""Lectura de Gmail via IMAP y parseo de mensajes a una estructura limpia."""

from __future__ import annotations

import email
import imaplib
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from email.header import decode_header, make_header
from email.message import Message

from bs4 import BeautifulSoup

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993


@dataclass
class Mail:
    uid: str
    sender_name: str
    sender_addr: str
    subject: str
    date: datetime | None
    body: str
    has_list_unsubscribe: bool
    html_ratio: float
    # Se rellenan mas adelante en el pipeline
    is_newsletter: bool = False
    classified_by: str = ""
    score: dict = field(default_factory=dict)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def _split_sender(raw: str) -> tuple[str, str]:
    name, addr = email.utils.parseaddr(raw)
    return _decode(name) or addr, addr.lower()


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _extract_body(msg: Message) -> tuple[str, float]:
    """Devuelve (texto, ratio_html). Prefiere text/plain, cae a text/html."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_filename():
                continue
            ctype = part.get_content_type()
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/plain":
                plain_parts.append(decoded)
            elif ctype == "text/html":
                html_parts.append(decoded)
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
        except Exception:
            decoded = ""
        if msg.get_content_type() == "text/html":
            html_parts.append(decoded)
        else:
            plain_parts.append(decoded)

    plain = "\n".join(plain_parts).strip()
    html = "\n".join(html_parts).strip()

    total = len(plain) + len(html)
    ratio = (len(html) / total) if total else 0.0

    # text/plain suele ser mas limpio; si es residual, usamos el HTML.
    if plain and len(plain) > 400:
        body = plain
    elif html:
        body = _html_to_text(html)
    else:
        body = plain

    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body, ratio


def connect() -> imaplib.IMAP4_SSL:
    user = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(user, password)
    return conn


def fetch_since(conn: imaplib.IMAP4_SSL, since: date, folder: str = "INBOX") -> list[Mail]:
    """Descarga los mensajes de INBOX desde `since` (inclusive).

    Nota: IMAP SINCE tiene granularidad de dia, no de hora. Filtramos
    despues por timestamp exacto en el pipeline para evitar duplicados.
    """
    conn.select(folder, readonly=True)
    criterion = f'(SINCE "{since.strftime("%d-%b-%Y")}")'
    status, data = conn.search(None, criterion)
    if status != "OK" or not data or not data[0]:
        return []

    uids = data[0].split()
    mails: list[Mail] = []

    for uid in uids:
        status, raw = conn.fetch(uid, "(RFC822)")
        if status != "OK" or not raw or not isinstance(raw[0], tuple):
            continue
        msg = email.message_from_bytes(raw[0][1])

        sender_name, sender_addr = _split_sender(msg.get("From", ""))
        body, ratio = _extract_body(msg)

        try:
            parsed_date = email.utils.parsedate_to_datetime(msg.get("Date", ""))
        except Exception:
            parsed_date = None

        mails.append(
            Mail(
                uid=uid.decode(),
                sender_name=sender_name,
                sender_addr=sender_addr,
                subject=_decode(msg.get("Subject")),
                date=parsed_date,
                body=body,
                # Esta es la senal fuerte: RFC 2369.
                has_list_unsubscribe=bool(
                    msg.get("List-Unsubscribe") or msg.get("List-Id")
                ),
                html_ratio=ratio,
            )
        )

    return mails


def close(conn: imaplib.IMAP4_SSL) -> None:
    try:
        conn.close()
    except Exception:
        pass
    try:
        conn.logout()
    except Exception:
        pass
