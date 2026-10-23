# Running it every day

"How do I let it run?" has three answers, in increasing order of
hands-off-ness. All of them end at the same place: a digest at
`output/digest_latest.html` every weekday morning, and a health table on it
that tells you when something upstream went quiet.

## 0. By hand (what you have without installing anything)

```bash
python -m src.main            # one run; opens the digest when it finishes
```

Fine for the validation period's first days; the failure mode is you — the
morning you forget is indistinguishable from a quiet market until you notice.

## 1. Scheduled on the Mac (recommended — this is the setup)

One-time, three steps, all from the repo root:

```bash
# 1. Secrets where the scheduler can see them. launchd never sources
#    ~/.zshrc, so the key exported there is invisible at 08:00.
cp .env.example .env
${EDITOR:-nano} .env          # fill OPENROUTER_API_KEY at minimum
chmod 600 .env

# 2. Install the weekday-08:00 launchd agent (idempotent — rerun anytime).
bash scripts/install_launchd.sh

# 3. Prove it end-to-end without waiting for tomorrow:
launchctl kickstart "gui/$(id -u)/com.job-hunter.daily"
tail -f output/logs/daily-$(date +%Y-%m-%d).log
```

What you get over cron: a `StartCalendarInterval` the Mac **slept through
fires once on wake** — a lid shut at 08:00 costs minutes, not the day. A Mac
powered *off* at 08:00 still skips (launchd cannot run on a machine that is
off); the digest's `missed_run` alert is the net under that, and
healthchecks.io (below) is the louder one.

The pieces, all in `scripts/`:

| File | Job |
|---|---|
| `run_daily.sh` | what the scheduler runs: sources `.env`, uses the venv's python, takes a lock so runs never overlap (a lock older than 6 h counts as left by a killed run and is reclaimed — SIGKILL and power loss skip cleanup), logs to `output/logs/daily-YYYY-MM-DD.log` (14-day retention), pings healthchecks |
| `com.job-hunter.daily.plist.template` | the schedule; `install_launchd.sh` fills in the repo path |
| `install_launchd.sh` | substitutes, installs to `~/Library/LaunchAgents`, loads, prints how to test-fire |

Logs: `output/logs/daily-YYYY-MM-DD.log` per run, `launchd.log` for anything
that breaks before the wrapper starts. launchd creates its log *files* but
never their directory — the installer creates `output/logs`, so if you ever
delete `output/`, `mkdir -p output/logs` (or rerun the installer) before the
next 08:00, or the agent fails to spawn with nothing logged anywhere. Undo it
all with `launchctl bootout "gui/$(id -u)/com.job-hunter.daily"`.

Two macOS notes: installing raises a one-time "Background Items Added"
notification, and the agent appears (and can be toggled) under **System
Settings → General → Login Items** — leave it enabled. And LaunchAgents run
inside your login session: a Mac sitting at the login window (say, after a
FileVault reboot) skips the same way a powered-off one does; the heartbeat
below is the net under both.

One more thing that has to be running at 08:00 if you use it: the local
model. The last entry of both model chains in `config.yaml` is
`qwen3.8:27b` through the Ollama app (`http://localhost:11434/v1`) — the
floor under the hosted ids, reached only when every one of them has failed.
Ollama down means that entry costs three refused connections and about two
seconds of backoff per job, and the job reaches the digest unscored, exactly
as before the entry existed; Ollama up means a scoring takes minutes (the
model thinks first, then answers at ~18 tok/s), which is why that entry
carries its own `timeout: 600` and `max_retries: 0` — one attempt of up to
ten minutes, never three.

## 2. Heartbeat (optional, free, two minutes)

Every alert the pipeline can raise shares one blind spot: they run inside the
pipeline. The morning where *nothing* ran raises nothing. An external
dead-man's switch closes it:

1. Create a free check at [healthchecks.io](https://healthchecks.io) —
   schedule "every weekday", grace period an hour.
2. Put its ping URL in `.env` as `HEALTHCHECKS_URL=https://hc-ping.com/...`.

`run_daily.sh` then pings `/start` when the run begins, the bare URL on
success, `/fail` on failure — and healthchecks emails you when a weekday
passes with no ping at all. That email is the difference between "the Mac was
off and I knew by 9" and discovering a week of missed mornings.

## Linux instead?

The cron line in the README still works verbatim; point it at
`scripts/run_daily.sh` to keep the lock, the `.env` loading and the
heartbeat:

```cron
0 8 * * 1-5 /bin/bash /path/to/job_search/scripts/run_daily.sh
```

## Offsite copy of the tracker (later, when it holds real history)

`output/tracker.sqlite3` is the one file that cannot be regenerated, and it
already gets a dated local copy every run (`output/backups/`, 14 kept). When
it holds months of application history, add an offsite replica:
[Litestream](https://litestream.io) streaming to a free Backblaze B2 bucket
is the €0 recipe — `brew install litestream`, a 6-line config pointing at
the tracker, `litestream replicate` as a second LaunchAgent. Not wired here
on purpose: it needs your B2 account, and until the tracker holds real
history the local dated copies are proportionate.

## What to look at each morning

The digest, top to bottom: the health table first (a red row = a source
died, a 404'd slug, a Tier 2 endpoint gone dark), then "Needs your click".
The LLM line under the health table is your running bill. Everything else is
optional reading.

## Recording outcomes

The tracker knows what the pipeline submitted. Everything after that, and
anything you applied to yourself, it learns from you — one command each,
from the repo root:

```bash
python -m src.track mark 3f9c2a1b applied        # applied by hand; the digest card prints this line
python -m src.track mark 3f9c2a1b interview --on 2026-10-20 --note "call with Ana"
python -m src.track mark 3f9c2a1b followed_up
python -m src.track mark 3f9c2a1b rejected       # or no_response (ghosted), offer, hired,
                                                 # offer_declined (declined), withdrawn, dismissed
python -m src.track mark 3f9c2a1b label_pursue --note "why"   # also label_stretch, label_dismiss
python -m src.track add --company "Acme" --title "Data Scientist" \
    --location "Kraków, Poland" --url https://acme.example/jobs/1   # applied outside the pipeline
python -m src.track list --open                  # still waiting on an answer
python -m src.track report                       # applied -> interview -> offer -> hired
python -m src.track export                       # output/applications.csv, one row per event
```

The handle is a full key, a unique key prefix of six or more characters
(the digest prints eight) or the posting URL (tracking parameters such as
`?gh_src=` and `?utm_source=` are ignored). Before writing, it echoes
the company and title. A handle that matches several jobs, or none, exits 2,
lists the candidates and writes nothing. `--on` defaults to today and refuses
future dates.

Every stage except `dismissed` and the labels means an application went out
(`withdrawn` included: only a sent application can be withdrawn), so it also
marks the job applied, and confirms one the pipeline recorded as submitted
but unconfirmed. The pipeline then never resurfaces it, never re-scores it and
never auto-applies to it or to a repost of the same company, title and city.
That makes `apply.dry_run: true` no longer the only guard against a second
application to a role you applied to yourself.

`add` is for applications made outside the pipeline. A Greenhouse, Lever,
Ashby, Workable, SmartRecruiters, Personio or Teamtailor URL is recorded under
the id the board's own copy carries, so that copy is blocked however you typed
the company or city. Any other URL blocks auto-apply to a posting at that URL;
a copy elsewhere is recognised only when its company, title and city read as
you typed them. The command prints which of the two it did.

`hired`, `rejected`, `no_response`, `offer_declined`, `withdrawn` and
`dismissed` close an application. Recording anything later needs `--force`
(exit 1 without it). Events are append-only, so a correction is a new event,
not an edit. A job wrongly marked applied stays blocked, which is why the
echo line is there.

On the digest, **Follow-ups due** appears below "Needs your click" only when
an open application has gone quiet for `tracking.followup_days` (10) with
fewer than `tracking.max_followups` (2) follow-ups at its current stage. At
`tracking.stale_days` (60) it suggests `no_response` instead. 0 turns a check
off. The page only prints the command to run; it never records anything.
**Applications to date**, in the run stats, counts each application at the
furthest stage it reached and shows rejections as a fraction of final
outcomes (`2/5`), never a bare percentage.
