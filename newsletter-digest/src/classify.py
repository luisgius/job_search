"""Cascada de clasificacion: que correo es newsletter y cual no.

Capa 1 (dura, gratis)  : List-Unsubscribe + allow/deny list de remitentes.
Capa 2 (frecuencia)    : remitente recurrente + alto ratio de HTML.
Capa 3 (LLM, marginal) : solo para los dudosos que sobreviven.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .mailbox import Mail

MIN_BODY_CHARS = 300
FREQ_THRESHOLD = 3
HTML_RATIO_THRESHOLD = 0.6


class SenderRules:
    def __init__(self, path: Path):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self.allow = {s.lower() for s in data.get("newsletters", [])}
        self.deny = {s.lower() for s in data.get("transactional", [])}
        self.frequency = {
            k.lower(): v for k, v in (data.get("frequency") or {}).items()
        }

    def is_allowed(self, addr: str) -> bool:
        return addr in self.allow or self._domain(addr) in self.allow

    def is_denied(self, addr: str) -> bool:
        return addr in self.deny or self._domain(addr) in self.deny

    def count_for(self, addr: str) -> int:
        return self.frequency.get(addr, 0)

    @staticmethod
    def _domain(addr: str) -> str:
        return addr.split("@")[-1] if "@" in addr else addr


def classify(mails: list[Mail], rules: SenderRules) -> tuple[list[Mail], list[Mail]]:
    """Devuelve (newsletters, dudosos_para_llm)."""
    newsletters: list[Mail] = []
    uncertain: list[Mail] = []

    for m in mails:
        if len(m.body) < MIN_BODY_CHARS:
            continue

        # Capa 1: la denylist manda siempre. Evita que el banco o GitHub
        # entren solo por tener cabecera de baja.
        if rules.is_denied(m.sender_addr):
            continue

        if rules.is_allowed(m.sender_addr):
            m.is_newsletter = True
            m.classified_by = "allowlist"
            newsletters.append(m)
            continue

        if m.has_list_unsubscribe:
            m.is_newsletter = True
            m.classified_by = "list-unsubscribe"
            newsletters.append(m)
            continue

        # Capa 2: remitente recurrente con cuerpo muy maquetado.
        if (
            rules.count_for(m.sender_addr) >= FREQ_THRESHOLD
            and m.html_ratio >= HTML_RATIO_THRESHOLD
        ):
            m.is_newsletter = True
            m.classified_by = "frequency+html"
            newsletters.append(m)
            continue

        # Capa 3: dudoso. Se resuelve con LLM en el pipeline principal.
        if rules.count_for(m.sender_addr) >= 2 or m.html_ratio >= HTML_RATIO_THRESHOLD:
            uncertain.append(m)

    return newsletters, uncertain
