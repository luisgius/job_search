#!/usr/bin/env bash
# The daily run, wrapped for a scheduler.
#
# launchd (or cron) calls THIS, never `python -m src.main` directly, because a
# scheduler's environment is not your shell: no profile, no exported keys, no
# working directory. Everything the run needs is re-established here:
#
#   * secrets come from `<repo>/.env` (git-ignored; see .env.example) — the
#     one place OPENROUTER_API_KEY etc. live for scheduled runs;
#   * the venv's python is used by absolute path;
#   * a lock directory stops two runs overlapping (the pipeline is
#     single-run by design; macOS has no flock(1), mkdir is the portable lock);
#   * stdout/stderr land in output/logs/daily-YYYY-MM-DD.log, pruned with the
#     same retention spirit as the tracker backups;
#   * if HEALTHCHECKS_URL is set (a free check at healthchecks.io), the run
#     pings /start, then success or /fail — which turns "the laptop was shut
#     at 08:00 and nothing ran" from a silent nothing into an email. Unset,
#     the pings are skipped without complaint.
#
# Exit code is src.main's own, so `launchctl` and the log agree with the run.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

LOG_DIR="$REPO/output/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/daily-$(date +%Y-%m-%d).log"
# The breadcrumb comes BEFORE anything that can fail: a wrapper that dies
# early must still leave "I ran" in the daily log, or debugging starts from
# a missing file instead of an error message.
echo "$(date -u +%FT%TZ) wrapper invoked" >>"$LOG"

# Secrets and knobs for scheduled runs. `set -a` exports everything the file
# assigns, so the pipeline sees them as environment (env-wins in config.py).
# Sourced inside `if !` on purpose: an unquoted value with spaces in .env
# ("PHONE=+34 600 ...") is a command-not-found under `set -e`, and it must
# cost a loud log line — every good line still applied — never the whole run.
if [ -f "$REPO/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    if ! . "$REPO/.env" 2>>"$LOG"; then
        echo "$(date -u +%FT%TZ) WARNING: a line in .env failed to parse —" \
             "quote values that contain spaces (see .env.example)" >>"$LOG"
    fi
    set +a
fi

ping_hc() {
    # Best-effort by construction: monitoring must never fail the run.
    [ -n "${HEALTHCHECKS_URL:-}" ] || return 0
    curl -fsS -m 10 --retry 3 "${HEALTHCHECKS_URL}${1:-}" >/dev/null 2>&1 || true
}

LOCK="$REPO/output/.daily-run.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    echo "$(date -u +%FT%TZ) another daily run holds $LOCK — skipping" >>"$LOG"
    exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

PY="$REPO/.venv/bin/python"
if [ ! -x "$PY" ]; then
    PY="$(command -v python3)"
    echo "$(date -u +%FT%TZ) no .venv found — falling back to $PY" >>"$LOG"
fi

# Old logs are regenerable noise; the digest and tracker carry the record.
find "$LOG_DIR" -name 'daily-*.log' -mtime +14 -delete 2>/dev/null || true

ping_hc "/start"
echo "$(date -u +%FT%TZ) daily run starting ($PY)" >>"$LOG"
if "$PY" -m src.main --no-browser >>"$LOG" 2>&1; then
    echo "$(date -u +%FT%TZ) daily run finished ok" >>"$LOG"
    ping_hc ""
else
    code=$?
    echo "$(date -u +%FT%TZ) daily run FAILED with exit $code" >>"$LOG"
    ping_hc "/fail"
    exit "$code"
fi
