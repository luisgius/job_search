"""Resumen y puntuacion de cada newsletter con la API de Claude."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from anthropic import Anthropic

from .mailbox import Mail

# Los identificadores de modelo cambian con el tiempo. Este se verifico como
# vigente el 2026-08-12; si algun dia devuelve 404, mira docs.anthropic.com.
MODEL = "claude-sonnet-5"

# Truncado del cuerpo. La mayoria de newsletters ponen lo bueno arriba.
MAX_BODY_CHARS = 14_000

SYSTEM = """Eres un filtro de newsletters para un lector concreto.
Tu trabajo es decidir si merece la pena que dedique su tiempo a leer cada
correo, y resumirlo con honestidad. No infles la puntuacion: la mayoria de
newsletters no merecen una lectura completa, y decirlo es util.

Devuelve UNICAMENTE un objeto JSON valido, sin markdown ni texto alrededor.

Esquema:
{
  "is_newsletter": bool,
  "relevance": int (0-5),
  "actionability": int (0-5),
  "summary": string (2-4 frases, en espanol, concreto, sin relleno),
  "key_takeaway": string (la unica idea que se llevaria si solo lee una linea),
  "action": string ("" si no hay nada accionable; si lo hay, el paso concreto),
  "reason": string (una frase justificando la puntuacion)
}

Criterio de relevance: encaje con los dominios de interes del lector.
Criterio de actionability: 5 = contiene algo reproducible o aplicable esta
semana (codigo, metodo, tecnica, decision concreta). 0 = opinion o noticia
sin nada que hacer. Un ensayo excelente pero no aplicable tiene relevance
alta y actionability baja: eso es correcto y esperado."""


def _profile_block(profile: dict) -> str:
    lines = ["DOMINIOS DE INTERES:"]
    for d in profile.get("domains", []):
        lines.append(f"- {d['name']} (peso {d.get('weight', 1.0)}): {d['detail'].strip()}")
    exclusions = profile.get("exclusions", [])
    if exclusions:
        lines.append("\nNO LE INTERESA:")
        lines.extend(f"- {e}" for e in exclusions)
    return "\n".join(lines)


def _parse_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def score_one(client: Anthropic, mail: Mail, profile: dict) -> dict:
    body = mail.body[:MAX_BODY_CHARS]
    truncated = len(mail.body) > MAX_BODY_CHARS

    prompt = f"""{_profile_block(profile)}

CORREO A EVALUAR
Remitente: {mail.sender_name} <{mail.sender_addr}>
Asunto: {mail.subject}
{"(cuerpo truncado)" if truncated else ""}

---
{body}
---"""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            # En Sonnet 5 el pensamiento adaptativo esta ACTIVO si no se dice
            # nada, y max_tokens limita pensamiento + respuesta juntos: con
            # 1000 tokens el JSON se cortaria a medias. Aqui no hace falta
            # razonar, solo rellenar un esquema fijo.
            thinking={"type": "disabled"},
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        return _parse_json(text)
    except Exception as exc:  # noqa: BLE001
        # Un fallo puntual no debe tumbar el digest entero.
        return {
            "is_newsletter": True,
            "relevance": 0,
            "actionability": 0,
            "summary": f"No se pudo procesar: {exc}",
            "key_takeaway": "",
            "action": "",
            "reason": "error",
            "error": True,
        }


def score_all(mails: list[Mail], profile: dict, max_workers: int = 4) -> list[Mail]:
    client = Anthropic()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(lambda m: score_one(client, m, profile), mails))
    for mail, result in zip(mails, results):
        mail.score = result
    return mails


def bucket(mail: Mail, thresholds: dict) -> str:
    rel = mail.score.get("relevance", 0)
    act = mail.score.get("actionability", 0)
    full = thresholds["read_full"]
    skim = thresholds["skim"]
    if rel >= full["min_relevance"] and act >= full["min_actionability"]:
        return "read_full"
    if rel >= skim["min_relevance"]:
        return "skim"
    return "archive"
