"""Punto de entrada. Se ejecuta a diario pero solo actua cada N dias."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from src import mailbox
from src.classify import SenderRules, classify
from src.digest import deliver_email, render_html, render_text
from src.score import bucket, score_all

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "state.json"
CADENCE_DAYS = 3
LOOKBACK_CAP_DAYS = 14  # red de seguridad si el estado se pierde

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("digest")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def main(force: bool = False) -> int:
    profile = yaml.safe_load((ROOT / "config" / "profile.yaml").read_text())
    rules = SenderRules(ROOT / "config" / "senders.yaml")
    state = load_state()
    now = datetime.now(timezone.utc)

    last_run_raw = state.get("last_run")
    if last_run_raw:
        last_run = datetime.fromisoformat(last_run_raw)
        elapsed = (now - last_run).days
        if elapsed < CADENCE_DAYS and not force:
            log.info("Solo han pasado %s dias de %s. Nada que hacer.", elapsed, CADENCE_DAYS)
            return 0
    else:
        last_run = now - timedelta(days=CADENCE_DAYS)
        log.info("Sin estado previo. Arrancando con ventana de %s dias.", CADENCE_DAYS)

    # Cap de seguridad: si el job estuvo caido semanas, no procesamos 300 correos.
    floor = now - timedelta(days=LOOKBACK_CAP_DAYS)
    window_start = max(last_run, floor)

    conn = mailbox.connect()
    try:
        mails = mailbox.fetch_since(conn, window_start.date())
    finally:
        mailbox.close(conn)

    # IMAP filtra por dia; afinamos por timestamp para no repetir.
    mails = [m for m in mails if m.date and m.date > window_start]
    log.info("Correos en ventana: %d", len(mails))

    newsletters, uncertain = classify(mails, rules)
    log.info("Newsletters: %d | Dudosos: %d", len(newsletters), len(uncertain))

    # Capa 3: los dudosos pasan por el modelo, que ademas decide si lo son.
    candidates = newsletters + uncertain
    if not candidates:
        log.info("Nada que resumir.")
        state["last_run"] = now.isoformat()
        save_state(state)
        return 0

    scored = score_all(candidates, profile)
    scored = [m for m in scored if m.score.get("is_newsletter", True)]

    thresholds = profile["scoring"]
    buckets: dict[str, list] = {"read_full": [], "skim": [], "archive": []}
    for m in scored:
        buckets[bucket(m, thresholds)].append(m)

    log.info(
        "Leer entero: %d | Resumen: %d | Archivar: %d",
        len(buckets["read_full"]),
        len(buckets["skim"]),
        len(buckets["archive"]),
    )

    worth_sending = buckets["read_full"] or buckets["skim"]
    if not worth_sending and thresholds.get("skip_send_if_empty", True):
        log.info("Nada supera el umbral. No se envia email.")
        state["last_run"] = now.isoformat()
        save_state(state)
        return 0

    period = f"{window_start.strftime('%d %b')} - {now.strftime('%d %b %Y')}"
    subject = (
        f"Digest: {len(buckets['read_full'])} para leer, "
        f"{len(buckets['skim'])} de repaso"
    )
    deliver_email(subject, render_html(buckets, period), render_text(buckets, period))
    log.info("Digest enviado.")

    state["last_run"] = now.isoformat()
    state["last_counts"] = {k: len(v) for k, v in buckets.items()}
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main(force="--force" in sys.argv))
