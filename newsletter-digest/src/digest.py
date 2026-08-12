"""Render del digest y entrega.

El sink esta desacoplado a proposito: para migrar a Notion, Slack o un
fichero, basta con escribir otra funcion `deliver_*` y cambiarla en main.
"""

from __future__ import annotations

import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from html import escape

from .mailbox import Mail

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

LABELS = {
    "read_full": ("Leer entero", "#1a7f37"),
    "skim": ("Resumen basta", "#9a6700"),
    "archive": ("Archivar", "#6e7781"),
}


def _sort_key(mail: Mail) -> tuple:
    s = mail.score
    # "Ambas por igual": ordenamos por la suma de los dos ejes.
    return (-(s.get("relevance", 0) + s.get("actionability", 0)), mail.sender_name)


def render_html(buckets: dict[str, list[Mail]], period: str) -> str:
    parts = [
        "<html><body style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "line-height:1.5;color:#1f2328;max-width:680px;margin:0 auto;padding:16px\">",
        f"<h2 style=\"margin:0 0 4px\">Digest de newsletters</h2>",
        f"<p style=\"color:#6e7781;margin:0 0 24px;font-size:14px\">{escape(period)}</p>",
    ]

    for key in ("read_full", "skim"):
        items = sorted(buckets.get(key, []), key=_sort_key)
        if not items:
            continue
        label, color = LABELS[key]
        parts.append(
            f"<h3 style=\"color:{color};border-bottom:2px solid {color};"
            f"padding-bottom:4px;margin-top:28px\">{label} ({len(items)})</h3>"
        )
        for m in items:
            s = m.score
            parts.append("<div style=\"margin:0 0 22px\">")
            parts.append(
                f"<div style=\"font-weight:600;font-size:15px\">{escape(m.subject)}</div>"
            )
            parts.append(
                f"<div style=\"color:#6e7781;font-size:13px;margin-bottom:6px\">"
                f"{escape(m.sender_name)} &middot; relevancia {s.get('relevance', 0)}/5 "
                f"&middot; accionable {s.get('actionability', 0)}/5</div>"
            )
            parts.append(
                f"<div style=\"font-size:14px\">{escape(s.get('summary', ''))}</div>"
            )
            if s.get("key_takeaway"):
                parts.append(
                    "<div style=\"font-size:14px;margin-top:6px;padding-left:10px;"
                    f"border-left:3px solid #d0d7de\"><b>Clave:</b> "
                    f"{escape(s['key_takeaway'])}</div>"
                )
            if s.get("action"):
                parts.append(
                    "<div style=\"font-size:14px;margin-top:6px;background:#f6f8fa;"
                    f"padding:8px;border-radius:6px\"><b>Accion:</b> "
                    f"{escape(s['action'])}</div>"
                )
            parts.append("</div>")

    archived = buckets.get("archive", [])
    if archived:
        parts.append(
            "<h3 style=\"color:#6e7781;margin-top:28px;font-size:15px\">"
            f"Descartadas ({len(archived)})</h3><ul style=\"color:#6e7781;font-size:13px\">"
        )
        for m in sorted(archived, key=_sort_key):
            parts.append(
                f"<li>{escape(m.sender_name)}: {escape(m.subject)} "
                f"&mdash; {escape(m.score.get('reason', ''))}</li>"
            )
        parts.append("</ul>")

    parts.append("</body></html>")
    return "".join(parts)


def render_text(buckets: dict[str, list[Mail]], period: str) -> str:
    lines = [f"DIGEST DE NEWSLETTERS - {period}", ""]
    for key in ("read_full", "skim", "archive"):
        items = sorted(buckets.get(key, []), key=_sort_key)
        if not items:
            continue
        lines.append(f"== {LABELS[key][0].upper()} ({len(items)}) ==")
        for m in items:
            s = m.score
            lines.append(f"- [{m.sender_name}] {m.subject}")
            lines.append(f"  R{s.get('relevance', 0)}/A{s.get('actionability', 0)} - {s.get('summary', '')}")
            if s.get("action"):
                lines.append(f"  ACCION: {s['action']}")
        lines.append("")
    return "\n".join(lines)


def deliver_email(subject: str, html: str, text: str) -> None:
    user = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("DIGEST_TO", user)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(user, password)
        server.send_message(msg)
