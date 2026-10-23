"""Tests for src/track.py — recording outcomes by hand.

Every write here goes into the tracker that carries the never-apply-twice
promise, under a key Luis typed from a digest. So the CLI is tested for the
ways a hand-typed command goes wrong: a prefix that matches two jobs, a URL
that matches none, a date in the future, a stage on a closed application.
Each must write nothing and exit non-zero. And the export is a CSV of
untrusted text, so it is tested the way a spreadsheet would open it.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest

from src import digest, track
from src.db import Tracker
from src.models import ApplyStatus
from tests.conftest import NOW, make_job, write_config

TODAY = date(2026, 8, 4)


@pytest.fixture
def setup(tmp_path: Path):
    """A config rooted in tmp_path and a tracker holding two seen jobs."""
    config = write_config(tmp_path)
    acme = make_job(company="Acme", ats_job_id="1")
    beta = make_job(company="Beta", ats_job_id="2")
    with Tracker(config.db_path) as tracker:
        for job in (acme, beta):
            tracker.record_job(job, now=NOW)
            tracker.record_status(job.key, ApplyStatus.DIGEST, now=NOW)
    return tmp_path, config, acme, beta


def run(tmp_path: Path, *argv: str, today: date = TODAY) -> int:
    args = [*argv, "--config", str(tmp_path / "config.yaml"),
            "--watchlist", str(tmp_path / "watchlist.yaml")]
    try:
        return track.main(args, today=today, now=NOW)
    except SystemExit as exc:  # argparse usage errors
        return int(exc.code)


def events(config, key):
    with Tracker(config.db_path) as tracker:
        return [(e["stage"], e["occurred_on"], e["note"]) for e in tracker._events(key)]


# ==========================================================================
# handles
# ==========================================================================


def test_a_unique_prefix_records_and_echoes_the_job_first(setup, capsys):
    tmp_path, config, acme, _ = setup
    assert run(tmp_path, "mark", acme.key[:8], "interview", "--on", "2026-08-02",
               "--note", "call with Ana") == 0
    out = capsys.readouterr().out
    assert out.index("Acme - Backend Engineer") < out.index("recorded interview on 2026-08-02")
    assert "marked applied" in out
    assert events(config, acme.key) == [("applied", "2026-08-02", "implied by interview"),
                                        ("interview", "2026-08-02", "call with Ana")]


def test_an_ambiguous_prefix_lists_the_candidates_and_writes_nothing(setup, capsys):
    tmp_path, config, _, _ = setup
    with Tracker(config.db_path) as tracker:
        for key, company in (("abcdef0000000001", "First"), ("abcdef0000000002", "Second")):
            tracker.conn.execute(
                "INSERT INTO jobs (key, company, title, url, first_seen_at, last_seen_at) "
                "VALUES (?, ?, 'Engineer', '', 'x', 'x')", (key, company))
        tracker.conn.commit()
    assert run(tmp_path, "mark", "abcdef", "applied") == 2
    err = capsys.readouterr().err
    assert "abcdef0000000001" in err and "abcdef0000000002" in err
    assert "nothing was written" in err
    assert events(config, "abcdef0000000001") == events(config, "abcdef0000000002") == []


@pytest.mark.parametrize("handle", ["ffffffff", "abc", "https://example.com/nope"])
def test_a_missing_handle_exits_2(setup, capsys, handle):
    tmp_path, *_ = setup
    assert run(tmp_path, "mark", handle, "applied") == 2
    err = capsys.readouterr().err
    assert "no job matches" in err
    if handle == "abc":
        assert "at least 6 characters" in err


def test_the_exact_posting_url_is_a_handle(setup):
    tmp_path, config, _, beta = setup
    assert run(tmp_path, "mark", beta.url, "rejected", "--on", "2026-08-03") == 0
    assert events(config, beta.key)[-1] == ("rejected", "2026-08-03", "")


def test_a_url_shared_by_two_jobs_is_ambiguous(setup, capsys):
    tmp_path, config, acme, _ = setup
    twin = make_job(company="Acme", ats_job_id="99", url=acme.url, source="lever", ats="lever")
    with Tracker(config.db_path) as tracker:
        tracker.record_job(twin, now=NOW)
    assert run(tmp_path, "mark", acme.url, "applied") == 2
    assert "matches 2 jobs" in capsys.readouterr().err


def test_hostile_job_text_cannot_reach_the_terminal_as_control_codes(setup, capsys):
    tmp_path, config, *_ = setup
    evil = make_job(company="Evil\x1b[2J\x1b]0;pwned\x07", ats_job_id="7")
    with Tracker(config.db_path) as tracker:
        tracker.record_job(evil, now=NOW)
    assert run(tmp_path, "mark", evil.key, "dismissed") == 0
    assert run(tmp_path, "list") == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "Evil" in out


# ==========================================================================
# add
# ==========================================================================


def test_add_records_a_manual_application_that_blocks_a_repost(setup, capsys):
    tmp_path, config, *_ = setup
    assert run(tmp_path, "add", "--company", "Gamma", "--title", "Data Scientist",
               "--location", "Kraków, Poland", "--url", "https://gamma.example/jobs/1",
               "--on", "2026-08-01") == 0
    assert "Gamma - Data Scientist" in capsys.readouterr().out
    board_copy = make_job(company="Gamma", title="Data Scientist", location="Kraków",
                          ats_job_id="555")
    with Tracker(config.db_path) as tracker:
        manual = tracker.jobs_matching("https://gamma.example/jobs/1")[0]
        assert manual["source"] == "manual"
        assert tracker.has_applied(manual["key"])
        assert tracker.get_application(manual["key"])["method"] == "manual"
        assert tracker.has_applied_similar(board_copy.dedupe_key)
    assert events(config, manual["key"]) == [("applied", "2026-08-01", "")]


def test_add_on_a_url_the_pipeline_already_saw_uses_that_job(setup, capsys):
    tmp_path, config, acme, _ = setup
    assert run(tmp_path, "add", "--company", "ACME Corp", "--title", "Backend",
               "--location", "Berlin", "--url", acme.url) == 0
    assert "already tracked" in capsys.readouterr().out
    with Tracker(config.db_path) as tracker:
        assert tracker.has_applied(acme.key)
        assert tracker.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2


BOARD_URL = "https://job-boards.greenhouse.io/acmeanalytics/jobs/4455667"


@pytest.mark.parametrize("company, location, url", [
    ("Acme Analytics", "Warsaw, Poland", BOARD_URL),
    ("Acme Analytics", "Warszawa", BOARD_URL),                  # Polish spelling
    ("ACME", "Warsaw, Poland", BOARD_URL),                      # brand, not the slug
    ("Acme Analytics", "Warsaw, Poland", BOARD_URL + "?gh_src=abc123"),
])
def test_add_then_the_board_copy_is_blocked_however_it_was_typed(setup, capsys,
                                                                   company, location, url):
    """`add` used to key the job on what was typed, so the Greenhouse copy —
    company from the board slug, city as the board spells it — passed every
    gate and could be auto-applied to, while the CLI promised it would not."""
    tmp_path, config, *_ = setup
    assert run(tmp_path, "add", "--company", company, "--title", "Senior Data Engineer",
               "--location", location, "--url", url, "--on", "2026-08-01") == 0
    out = capsys.readouterr().out
    assert "marked applied as greenhouse job 4455667" in out
    assert "repost of it" not in out
    board = make_job(company="Acmeanalytics", title="Senior Data Engineer",
                     location="Warsaw, Poland", ats_job_id="4455667", url=BOARD_URL)
    with Tracker(config.db_path) as tracker:
        assert tracker.has_applied(board.key)
        assert tracker.should_surface(board.key, now=NOW) is False
        assert tracker.has_applied_url(board.url)
        assert tracker.get_job(board.key)["source"] == "manual"


@pytest.mark.parametrize("url, expected", [
    (BOARD_URL, ("greenhouse", "4455667")),
    ("https://boards.greenhouse.io/acme/jobs/123?gh_jid=123", ("greenhouse", "123")),
    ("https://boards.eu.greenhouse.io/acme/jobs/9/", ("greenhouse", "9")),
    ("https://www.acme.com/careers/open-roles?gh_jid=4012345", ("greenhouse", "4012345")),
    ("https://boards.greenhouse.io/embed/job_app?for=acme&token=55", ("greenhouse", "55")),
    ("https://jobs.lever.co/globex/9F2B1C4E-1111-2222-3333-444455556666/apply",
     ("lever", "9f2b1c4e-1111-2222-3333-444455556666")),
    ("https://jobs.eu.lever.co/globex/9f2b1c4e-1111-2222-3333-444455556666",
     ("lever", "9f2b1c4e-1111-2222-3333-444455556666")),
    ("https://jobs.ashbyhq.com/initech/0d4e5f6a-1111-2222-3333-444455556666",
     ("ashby", "0d4e5f6a-1111-2222-3333-444455556666")),
    ("https://apply.workable.com/hooli/j/A1B2C3D4E5/", ("workable", "A1B2C3D4E5")),
    ("https://jobs.smartrecruiters.com/Contoso/743999912345678-data-engineer",
     ("smartrecruiters", "743999912345678")),
    ("https://acme.jobs.personio.de/job/1234567?language=en", ("personio", "1234567")),
    ("https://acme.teamtailor.com/jobs/4242-data-engineer", ("teamtailor", "4242")),
    ("https://gamma.example/jobs/1", (None, None)),
    ("https://www.greenhouse.io/customers", (None, None)),
    ("https://jobs.lever.co/globex", (None, None)),
])
def test_ats_identity_is_the_id_each_board_adapter_keys_by(url, expected):
    assert track.ats_identity(url) == expected


def test_add_without_an_ats_id_promises_only_what_the_url_and_names_enforce(setup, capsys):
    tmp_path, *_ = setup
    assert run(tmp_path, "add", "--company", "Gamma", "--title", "Data Scientist",
               "--location", "Kraków, Poland", "--url", "https://gamma.example/jobs/1") == 0
    out = capsys.readouterr().out
    assert "will not auto-apply to anything at this URL" in out
    assert "only when its company, title and city read as typed" in out
    assert "resurface" not in out


def test_a_url_with_tracking_noise_is_still_a_handle(setup):
    tmp_path, config, _, beta = setup
    assert run(tmp_path, "mark", beta.url + "?utm_source=linkedin", "rejected") == 0
    assert events(config, beta.key)[-1][0] == "rejected"


def test_an_answer_on_an_unconfirmed_submission_says_it_confirmed_it(setup, capsys):
    tmp_path, config, acme, _ = setup
    with Tracker(config.db_path) as tracker:
        tracker.record_status(acme.key, ApplyStatus.SUBMITTED_UNCONFIRMED, now=NOW)
    assert run(tmp_path, "mark", acme.key, "interview") == 0
    out = capsys.readouterr().out
    assert "recorded interview on 2026-08-04" in out
    assert "receipt confirmed: status is now applied" in out
    with Tracker(config.db_path) as tracker:
        assert tracker.get_status(acme.key) == ApplyStatus.APPLIED.value


@pytest.mark.parametrize("argv", [
    ("add", "--company", "Gamma", "--title", "DS", "--url", "https://g.example/1"),
    ("add", "--company", "Gamma", "--title", "DS", "--location", "Kraków",
     "--url", "javascript:alert(1)"),
])
def test_add_refuses_an_incomplete_or_unsafe_record(setup, argv):
    tmp_path, config, *_ = setup
    assert run(tmp_path, *argv) == 2
    with Tracker(config.db_path) as tracker:
        assert tracker.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2


# ==========================================================================
# dates and exit codes
# ==========================================================================


@pytest.mark.parametrize("bad", ["2026-02-30", "yesterday", "04/08/2026", "2026-8-4"])
def test_a_date_that_is_not_one_exits_2(setup, bad):
    tmp_path, config, acme, _ = setup
    assert run(tmp_path, "mark", acme.key, "interview", "--on", bad) == 2
    assert events(config, acme.key) == []


def test_a_future_date_exits_2_and_writes_nothing(setup, capsys):
    tmp_path, config, acme, _ = setup
    assert run(tmp_path, "mark", acme.key, "interview", "--on", "2026-08-05") == 2
    assert "in the future" in capsys.readouterr().err
    assert events(config, acme.key) == []
    # Today itself is fine, and it is the default.
    assert run(tmp_path, "mark", acme.key, "interview") == 0
    assert events(config, acme.key)[-1][:2] == ("interview", "2026-08-04")


def test_exit_codes(setup, tmp_path):
    tmp_path, config, acme, _ = setup
    assert run(tmp_path, "mark", acme.key, "phone screen") == 2      # unknown stage
    assert run(tmp_path, "mark", acme.key) == 2                     # no stage
    assert run(tmp_path, "explode") == 2                            # no such command
    assert run(tmp_path, "mark", acme.key, "rejected") == 0
    assert run(tmp_path, "mark", acme.key, "interview") == 1        # closed
    assert run(tmp_path, "mark", acme.key, "interview", "--force") == 0
    assert run(tmp_path, "mark", acme.key, "label_stretch") == 0    # labels never refused


def test_a_missing_tracker_is_refused_rather_than_created(tmp_path, capsys):
    config = write_config(tmp_path)
    assert run(tmp_path, "list") == 1
    assert "no tracker" in capsys.readouterr().err
    assert not config.db_path.exists()


def test_list_and_report(setup, capsys):
    tmp_path, config, acme, beta = setup
    run(tmp_path, "mark", acme.key, "applied", "--on", "2026-07-01")
    run(tmp_path, "mark", beta.key, "interview", "--on", "2026-07-10")
    run(tmp_path, "mark", beta.key, "rejected", "--on", "2026-07-20")
    capsys.readouterr()

    assert run(tmp_path, "list", "--open") == 0
    out = capsys.readouterr().out
    assert acme.key[:8] in out and beta.key[:8] not in out

    assert run(tmp_path, "report") == 0
    out = capsys.readouterr().out
    with Tracker(config.db_path) as tracker:
        expected = track.funnel_lines(digest.application_funnel(tracker.outcome_events()))
    assert out.splitlines() == expected
    assert "applied 2 -> reached interview 1 -> reached offer 0 -> hired 0" in out
    assert "Rejected or no response: 1/1 final outcomes" in out


# ==========================================================================
# export
# ==========================================================================


def read_csv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.reader(stream))


def test_export_round_trips_commas_quotes_and_newlines(setup):
    tmp_path, config, acme, _ = setup
    note = 'Spoke to Ana, "the lead"\nsecond call next week'
    assert run(tmp_path, "mark", acme.key, "interview", "--note", note) == 0
    assert run(tmp_path, "export") == 0
    rows = read_csv(config.output_dir / "applications.csv")
    assert rows[0] == list(track.EXPORT_COLUMNS)
    by_stage = {row[7]: dict(zip(rows[0], row)) for row in rows[1:]}
    assert by_stage["interview"]["note"] == note
    assert by_stage["interview"]["key"] == acme.key
    assert by_stage["interview"]["company"] == "Acme"


def test_export_defuses_spreadsheet_formulas(setup, tmp_path):
    tmp_path, config, *_ = setup
    hostile = make_job(company='=HYPERLINK("http://evil","x")', title="+SUM(A1)",
                       location="-2+3", ats_job_id="66")
    with Tracker(config.db_path) as tracker:
        tracker.record_job(hostile, now=NOW)
    for note in ("@cmd", "\tlead", "\rx"):
        assert run(tmp_path, "mark", hostile.key, "followed_up", "--note", note) == 0
    out = tmp_path / "custom" / "export.csv"
    assert run(tmp_path, "export", "--out", str(out)) == 0
    rows = [dict(zip(read_csv(out)[0], row)) for row in read_csv(out)[1:]]
    assert rows and all(row["company"] == "'" + hostile.company for row in rows)
    assert all(row["title"] == "'+SUM(A1)" and row["location"] == "'-2+3" for row in rows)
    assert sorted(row["note"] for row in rows if row["stage"] == "followed_up") == sorted(
        ["'\rx", "'\tlead", "'@cmd"])
    for row in rows:
        for value in row.values():
            assert not value.startswith(("=", "+", "-", "@", "\t", "\r")), value
