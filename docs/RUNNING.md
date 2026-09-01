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
| `run_daily.sh` | what the scheduler runs: sources `.env`, uses the venv's python, takes a lock so runs never overlap, logs to `output/logs/daily-YYYY-MM-DD.log` (14-day retention), pings healthchecks |
| `com.job-hunter.daily.plist.template` | the schedule; `install_launchd.sh` fills in the repo path |
| `install_launchd.sh` | substitutes, installs to `~/Library/LaunchAgents`, loads, prints how to test-fire |

Logs: `output/logs/daily-YYYY-MM-DD.log` per run, `launchd.log` for anything
that breaks before the wrapper starts. Undo it all with
`launchctl bootout "gui/$(id -u)/com.job-hunter.daily"`.

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
