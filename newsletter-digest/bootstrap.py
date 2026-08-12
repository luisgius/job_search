"""Escanea tus ultimos 90 dias y propone config/senders.yaml.

Ejecutar UNA vez, en local. Luego revisa el fichero a mano: ese repaso
de 10 minutos es lo que hace que la precision no dependa del azar.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import yaml

from src import mailbox

DAYS = 90
OUT = Path(__file__).parent / "config" / "senders.yaml"

# Remitentes que escriben mucho pero NO son newsletters. Amplia esta lista.
TRANSACTIONAL_HINTS = (
    "noreply", "no-reply", "notifications", "notification", "alert",
    "security", "billing", "invoice", "receipt", "support", "verify",
    "account", "statement",
)


def main() -> None:
    conn = mailbox.connect()
    try:
        mails = mailbox.fetch_since(conn, date.today() - timedelta(days=DAYS))
    finally:
        mailbox.close(conn)

    counts: Counter[str] = Counter()
    unsub: defaultdict[str, int] = defaultdict(int)
    names: dict[str, str] = {}

    for m in mails:
        counts[m.sender_addr] += 1
        names.setdefault(m.sender_addr, m.sender_name)
        if m.has_list_unsubscribe:
            unsub[m.sender_addr] += 1

    likely_newsletters, likely_transactional, review = [], [], []

    for addr, n in counts.most_common():
        if n < 2:
            continue
        unsub_ratio = unsub[addr] / n
        looks_transactional = any(h in addr for h in TRANSACTIONAL_HINTS)

        if unsub_ratio >= 0.8 and not looks_transactional:
            likely_newsletters.append(addr)
        elif looks_transactional:
            likely_transactional.append(addr)
        else:
            review.append(addr)

    doc = {
        "newsletters": sorted(likely_newsletters),
        "transactional": sorted(likely_transactional),
        "frequency": {a: c for a, c in counts.most_common() if c >= 2},
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Generado por bootstrap.py. REVISALO A MANO antes de usarlo.\n"
        "# newsletters  : allowlist, entran siempre.\n"
        "# transactional: denylist, se descartan siempre (gana a la allowlist).\n"
        "# frequency    : historico para la capa 2. No lo edites.\n"
        f"#\n# Pendientes de revisar ({len(review)}), decide tu en que lista van:\n"
        + "".join(f"#   {a}  ({counts[a]} correos, {names.get(a, '')})\n" for a in review)
    )
    OUT.write_text(header + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False))

    print(f"Escrito {OUT}")
    print(f"  Newsletters detectadas : {len(likely_newsletters)}")
    print(f"  Transaccionales        : {len(likely_transactional)}")
    print(f"  A revisar manualmente  : {len(review)}  <-- mira el header del yaml")


if __name__ == "__main__":
    main()
