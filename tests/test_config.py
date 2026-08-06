"""Tests for src/config.py.

Two things here are safety-relevant rather than cosmetic:
  * environment variables must beat config.yaml, so a key exported in the
    shell is never shadowed by a stale committed value;
  * `validate()` must report *every* problem at once — a user who has to run
    the pipeline five times to discover five typos will stop using it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.config import (
    DEFAULT_MAX_AGE_HOURS,
    DEFAULT_REPOST_MIN_GAP_DAYS,
    DEFAULTS,
    Config,
    ConfigError,
    deep_merge,
)
from src.config import WATCHLIST_DEFAULTS as DEFAULTS_WATCHLIST
from tests.conftest import write_config


# ==========================================================================
# deep_merge
# ==========================================================================


def test_deep_merge_recurses_into_dicts():
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    assert deep_merge(base, {"a": {"b": 9}}) == {"a": {"b": 9, "c": 2}, "d": 3}


def test_deep_merge_replaces_lists_wholesale():
    # Merging lists element-wise would make it impossible to *shrink* a list,
    # e.g. to trim filters.countries down to two countries.
    assert deep_merge({"x": [1, 2, 3]}, {"x": [9]}) == {"x": [9]}


def test_deep_merge_ignores_none_overrides():
    # An empty YAML block parses as None; that means "unspecified", not "null".
    assert deep_merge({"a": 1}, {"a": None}) == {"a": 1}


def test_deep_merge_does_not_mutate_the_base():
    base = {"a": {"b": [1]}}
    deep_merge(base, {"a": {"b": [2], "c": 3}})
    assert base == {"a": {"b": [1]}}


def test_deep_merge_tolerates_non_dict_override():
    assert deep_merge({"a": 1}, "nonsense") == {"a": 1}


# ==========================================================================
# loading
# ==========================================================================


def test_missing_files_fall_back_to_defaults(tmp_path: Path):
    cfg = Config.load(tmp_path / "nope.yaml", tmp_path / "nope2.yaml",
                      root=tmp_path, env={})
    assert cfg.get("scoring.threshold") == DEFAULTS["scoring"]["threshold"]
    assert cfg.get("apply.dry_run") is True


def test_partial_config_keeps_untouched_defaults(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"scoring": {"threshold": 90}}), encoding="utf-8"
    )
    cfg = Config.load(tmp_path / "config.yaml", tmp_path / "missing.yaml",
                      root=tmp_path, env={})
    assert cfg.get("scoring.threshold") == 90
    assert cfg.get("scoring.model") == DEFAULTS["scoring"]["model"]


def test_invalid_yaml_raises_config_error(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("applicant:\n  name: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        Config.load(tmp_path / "config.yaml", tmp_path / "w.yaml", root=tmp_path, env={})


def test_non_mapping_yaml_raises_config_error(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        Config.load(tmp_path / "config.yaml", tmp_path / "w.yaml", root=tmp_path, env={})


def test_empty_yaml_file_is_treated_as_absent(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    cfg = Config.load(tmp_path / "config.yaml", tmp_path / "w.yaml", root=tmp_path, env={})
    assert cfg.get("apply.dry_run") is True


# ==========================================================================
# duplicate keys — the watchlist killer
#
# PyYAML keeps the *last* value for a duplicated key and raises nothing. A
# watchlist that already has `greenhouse: [spotify, datadog]` and gains a
# second `greenhouse:` block at the bottom — say, one pasted from a
# `--discover` report — therefore deletes spotify and datadog from every
# future run, silently, with exit code 0. A deleted job is invisible forever,
# and a quiet board is indistinguishable from a quiet market, so the loader
# refuses the file and names the key instead.
# ==========================================================================


def test_a_duplicate_top_level_watchlist_key_is_refused_not_last_wins(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    (tmp_path / "watchlist.yaml").write_text(
        "greenhouse:\n  - spotify\n  - datadog\nlever:\n  - plaid\n"
        "greenhouse:\n  - glovo\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        Config.load(tmp_path / "config.yaml", tmp_path / "watchlist.yaml",
                    root=tmp_path, env={})
    message = str(excinfo.value)
    assert "duplicate key 'greenhouse'" in message
    assert "watchlist.yaml" in message


def test_a_duplicate_key_in_config_yaml_is_refused_too(tmp_path: Path):
    """Same footgun, other file: the second `scoring:` block would silently
    replace the first, and the user would believe both took effect."""
    (tmp_path / "config.yaml").write_text(
        "scoring:\n  threshold: 70\nscoring:\n  max_jobs: 10\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="duplicate key 'scoring'"):
        Config.load(tmp_path / "config.yaml", tmp_path / "w.yaml",
                    root=tmp_path, env={})


def test_a_nested_duplicate_key_is_refused_at_any_depth(tmp_path: Path):
    """Two `path:` keys under `db:` are the same mistake at smaller scale, and
    last-wins there means the tracker quietly lands in the wrong file."""
    (tmp_path / "config.yaml").write_text(
        "db:\n  path: a.sqlite3\n  path: b.sqlite3\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="duplicate key 'path'"):
        Config.load(tmp_path / "config.yaml", tmp_path / "w.yaml",
                    root=tmp_path, env={})


def test_anchors_aliases_and_merge_keys_are_not_duplicates(tmp_path: Path):
    """The refusal must not outlaw YAML's own reuse features: an alias is not
    a duplicated key, and overriding a merged-in key with an explicit one is
    the documented point of `<<:` rather than a paste accident."""
    (tmp_path / "config.yaml").write_text(
        "applicant: &shared\n  name: Ada\n  email: ada@example.com\n"
        "scoring:\n  <<: *shared\n  threshold: 70\n",
        encoding="utf-8",
    )
    cfg = Config.load(tmp_path / "config.yaml", tmp_path / "w.yaml",
                      root=tmp_path, env={})
    assert cfg.get("scoring.threshold") == 70
    assert cfg.get("scoring.name") == "Ada"


ROOT = Path(__file__).resolve().parent.parent


def _flatten(data, prefix=""):
    """Nested mapping -> {"a.b.c": value}. An empty dict is a leaf."""
    out = {}
    for key, value in (data or {}).items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.update(_flatten(value, dotted + "."))
        else:
            out[dotted] = value
    return out


def test_shipped_config_and_watchlist_parse():
    """The files a new user actually edits must load without surprises."""
    cfg = Config.load(ROOT / "config.yaml", ROOT / "watchlist.yaml", root=ROOT, env={})
    assert cfg.get("apply.dry_run") is True, "shipped config must default to dry run"
    assert cfg.get("scoring.threshold") == 65
    assert isinstance(cfg.watchlist.get("greenhouse"), list)


def test_the_shipped_config_never_drifts_away_from_the_defaults():
    """The fifth site, and the one that was never asserted.

    `freshness.max_age_hours` was unified across four *code* sites and each
    got a test. The shipped `config.yaml` is the fifth, it deep-merges **over**
    `DEFAULTS`, and therefore it is the one that wins at runtime — and nothing
    checked it. All three of these mutations survived the whole 1684-test
    suite:

        config.yaml  max_age_hours: 72 -> 24
        config.yaml  repost_min_gap_days: 14 -> 0
        config.yaml  filters.countries: (drop half of them)

    In other words the shipped file could silently revert an entire commit and
    the suite stayed green.

    Asserted as an exact set comparison rather than a handful of spot checks,
    because a spot check only defends the settings somebody thought of. Any
    intentional divergence goes in `DELIBERATE` below with a reason — if the
    file is *meant* to differ from the default, that belongs in the test, not
    in a reader's memory.
    """
    shipped = _flatten(yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")))
    defaults = _flatten(DEFAULTS)

    #: dotted key -> why the shipped file deliberately differs.
    #:
    #: The applicant block is per-user identity: DEFAULTS ships it empty so a
    #: fresh clone fails validation loudly instead of applying as nobody, and
    #: the user's own config fills it in. That is divergence by design — the
    #: one kind this list exists to record.
    _APPLICANT = "per-user identity; DEFAULTS stays empty so a fresh clone fails validation"
    DELIBERATE: dict[str, str] = {
        "applicant.name": _APPLICANT,
        "applicant.email": _APPLICANT,
        "applicant.phone": _APPLICANT,
        "applicant.location": _APPLICANT,
        "applicant.linkedin": _APPLICANT,
        "applicant.github": _APPLICANT,
        "applicant.website": _APPLICANT,
    }

    unknown = sorted(set(shipped) - set(defaults) - set(DELIBERATE))
    assert not unknown, (
        f"config.yaml sets keys DEFAULTS has never heard of: {unknown}. "
        "Either the key is a typo and does nothing, or DEFAULTS is missing it "
        "— both are silent."
    )

    drifted = {
        key: (shipped[key], defaults[key])
        for key in sorted(set(shipped) & set(defaults))
        if key not in DELIBERATE and shipped[key] != defaults[key]
    }
    assert not drifted, (
        "config.yaml disagrees with DEFAULTS, and config.yaml is the one that "
        f"wins at runtime: {drifted}"
    )


def test_a_bare_yaml_keyword_key_never_reaches_the_shipped_config():
    """The bug the exact-set comparison above caught on its first run.

    YAML 1.1 reads a bare `on`, `off`, `yes` and `no` as booleans. `notify:`
    had `on: [no_digest, ...]`, so the whole alert list landed under a key
    named `True`, `notify.on` quietly fell back to `DEFAULTS`, and a user
    editing that list in the file they were told to edit changed nothing at
    all. It was invisible precisely because the two lists happened to agree.

    `filters.countries` already carries a `# MUST stay quoted` comment for
    exactly this on the Norway entry. This test is that comment, enforced.
    """
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    booleans = [
        f"{section}.{key}"
        for section, block in raw.items()
        if isinstance(block, dict)
        for key in block
        if isinstance(key, bool)
    ]
    assert not booleans, (
        f"a bare YAML keyword became a boolean key: {booleans}. Quote it."
    )
    assert raw["notify"]["on"], "notify.on must survive as a string key"


def test_the_shipped_watchlist_names_only_real_sources():
    """A typo'd source name in `watchlist.yaml` is the same silent failure as
    an enabled board with an empty list: it fetches nothing and reads as a
    quiet market. `validate()` cannot see it, because it only looks up the
    sources it knows."""
    watch = yaml.safe_load((ROOT / "watchlist.yaml").read_text(encoding="utf-8"))
    assert set(watch) <= set(DEFAULTS_WATCHLIST), (
        f"watchlist.yaml names sources that do not exist: "
        f"{sorted(set(watch) - set(DEFAULTS_WATCHLIST))}"
    )


# ==========================================================================
# secrets
# ==========================================================================


def test_env_beats_config_file(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"keys": {"anthropic": "from-file"}}), encoding="utf-8"
    )
    cfg = Config.load(tmp_path / "config.yaml", tmp_path / "w.yaml", root=tmp_path,
                      env={"ANTHROPIC_API_KEY": "from-env"})
    assert cfg.anthropic_key == "from-env"


def test_empty_env_var_does_not_clobber_the_file(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"keys": {"anthropic": "from-file"}}), encoding="utf-8"
    )
    cfg = Config.load(tmp_path / "config.yaml", tmp_path / "w.yaml", root=tmp_path,
                      env={"ANTHROPIC_API_KEY": ""})
    assert cfg.anthropic_key == "from-file"


def test_adzuna_keys_come_from_env_too(tmp_path: Path):
    cfg = Config.load(tmp_path / "c.yaml", tmp_path / "w.yaml", root=tmp_path,
                      env={"ADZUNA_APP_ID": "id1", "ADZUNA_APP_KEY": "key1"})
    assert cfg.get("keys.adzuna_app_id") == "id1"
    assert cfg.get("keys.adzuna_app_key") == "key1"


# ==========================================================================
# access
# ==========================================================================


def test_get_dotted_and_default(config):
    assert config.get("applicant.name") == "Ada Lovelace"
    assert config.get("nope.nope", "fallback") == "fallback"
    assert config.get("applicant.name.deeper", "fallback") == "fallback"


def test_path_resolves_relative_to_root(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"db": {"path": "output/t.sqlite3"}}), encoding="utf-8"
    )
    cfg = Config.load(tmp_path / "config.yaml", tmp_path / "w.yaml", root=tmp_path, env={})
    assert cfg.db_path == tmp_path / "output" / "t.sqlite3"


def test_path_keeps_absolute_paths(tmp_path: Path):
    absolute = tmp_path / "elsewhere" / "t.sqlite3"
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"db": {"path": str(absolute)}}), encoding="utf-8"
    )
    cfg = Config.load(tmp_path / "config.yaml", tmp_path / "w.yaml", root=tmp_path, env={})
    assert cfg.db_path == absolute


def test_source_enabled(config):
    assert config.source_enabled("greenhouse") is True
    assert config.source_enabled("adzuna") is False
    assert config.source_enabled("nonexistent") is False


# ==========================================================================
# validate
# ==========================================================================


def test_valid_config_has_no_problems(config):
    assert config.validate() == []


def test_validate_reports_every_problem_at_once(tmp_path: Path):
    cfg = write_config(
        tmp_path,
        {"applicant": {"name": "", "email": ""}, "keys": {"openrouter": ""}},
        cv=None,
    )
    problems = cfg.validate()
    joined = " ".join(problems).lower()
    assert len(problems) >= 4
    assert "applicant.name" in joined
    assert "applicant.email" in joined
    assert "openrouter" in joined
    assert "cv" in joined


def test_validate_rejects_a_malformed_email(tmp_path: Path):
    cfg = write_config(tmp_path, {"applicant": {"email": "ada-at-example"}})
    assert any("does not look like an address" in p for p in cfg.validate())


def test_validate_can_skip_the_llm_key_check(tmp_path: Path):
    cfg = write_config(tmp_path, {"keys": {"openrouter": ""}})
    assert cfg.validate() != []
    assert cfg.validate(require_llm=False) == []


@pytest.mark.parametrize("threshold", [-1, 101, "high", True])
def test_validate_rejects_bad_thresholds(tmp_path: Path, threshold):
    # `True` matters: YAML parses a bare `yes` as a bool, and bool is a
    # subclass of int, so an unguarded range check reads it as a threshold of 1.
    cfg = write_config(tmp_path, {"scoring": {"threshold": threshold}})
    assert any("threshold" in p for p in cfg.validate())


def test_null_threshold_means_unspecified_not_invalid(tmp_path: Path):
    # `scoring:\n  threshold:\n` is an empty block, i.e. "use the default".
    cfg = write_config(tmp_path, {"scoring": {"threshold": None}})
    assert cfg.get("scoring.threshold") == 65
    assert cfg.validate() == []


@pytest.mark.parametrize("hours", [0, -5, "soon", True])
def test_validate_rejects_bad_freshness(tmp_path: Path, hours):
    cfg = write_config(tmp_path, {"freshness": {"max_age_hours": hours}})
    assert any("max_age_hours" in p for p in cfg.validate())


@pytest.mark.parametrize("value", [-1, "soon", True])
def test_validate_rejects_a_bad_repost_gap(tmp_path: Path, value):
    """It only ever colours a card, so a bad value costs a wrong flag rather
    than a lost posting — but a setting that is silently ignored is worse than
    one that is rejected, because the user believes it took effect."""
    cfg = write_config(tmp_path, {"freshness": {"repost_min_gap_days": value}})
    assert any("repost_min_gap_days" in p for p in cfg.validate())


def test_zero_is_a_legal_repost_gap_and_means_off(tmp_path: Path):
    """The neighbouring case. 0 stays *legal* — but it now means "off", which
    is what 0 means everywhere else in this config (`scoring.max_jobs: 0`
    scores nothing; `should_surface(within_days=0)` applies no window). Read
    the old way — "flag everything you can" — it turned the flag up to
    maximum, and the only knob for quietening a noisy accusation was the one
    that made it loudest. `test_digest` pins the behaviour; this pins that
    validate still accepts it."""
    cfg = write_config(tmp_path, {"freshness": {"repost_min_gap_days": 0}})
    assert cfg.validate() == []


# ==========================================================================
# the freshness window has exactly one definition
# ==========================================================================


def test_the_freshness_window_is_defined_once_and_read_everywhere(tmp_path: Path):
    """The regression this constant exists to prevent.

    `freshness.max_age_hours` used to be written out as a literal `24` in four
    places: `DEFAULTS`, `Config.validate`, `filters.apply_filters` and
    `digest._config_summary`. Four copies of one number is a latent bug —
    change one and the others silently disagree, so the digest cheerfully
    reports a window the filter is not using and the funnel stops adding up.

    Each assertion below is one of those four sites, driven through its own
    public entry point rather than by reading the source.
    """
    from src import digest, filters
    from tests.conftest import NOW, make_job

    # 1. the shipped default
    assert DEFAULTS["freshness"]["max_age_hours"] == DEFAULT_MAX_AGE_HOURS
    # 2. a loaded config with no freshness block of its own
    assert write_config(tmp_path).get("freshness.max_age_hours") == DEFAULT_MAX_AGE_HOURS
    # 3. the filter's own fallback, on a config that names no window at all
    inside = make_job(hours_old=DEFAULT_MAX_AGE_HOURS - 1, ats_job_id="in")
    outside = make_job(hours_old=DEFAULT_MAX_AGE_HOURS + 1, ats_job_id="out")
    kept = filters.apply_filters([inside, outside], {}, now=NOW).kept
    assert [job.ats_job_id for job in kept] == ["in"]
    # 4. what the digest tells the reader the window was
    assert digest.build_context([], None, None, now=NOW)["config_summary"][
        "max_age_hours"] == DEFAULT_MAX_AGE_HOURS


def test_the_window_is_seventy_two_hours_not_twenty_four():
    """Pinned with its argument, because the obvious "tidy-up" is to put it
    back to 24 and nobody would notice what that costs.

    A narrower window does not make the digest smaller: `db.skip_seen_days`
    already guarantees each posting is shown exactly once, so widening 24h to
    72h does not triple anything. What it does is stop losing things — a
    Friday posting when the next run is Monday, a board that publishes in
    batches, an aggregator whose timestamp is its own ingest time — and every
    one of those is a real job that vanishes with no trace that it existed.

    The premise behind 24 was "apply within a day or lose". That is contested:
    one tracked sample of 347 applications saw 12% response on day one against
    61% on day four, because day one is when the other 200 applicants arrive.
    """
    assert DEFAULT_MAX_AGE_HOURS == 72


def test_the_ghost_job_threshold_has_a_default_worth_defending():
    """14 days: comfortably longer than any same-board ingest lag, so an
    ordinary duplicate is never mistaken for a re-listing, and comfortably
    shorter than a hiring cycle.

    There is deliberately no age threshold beside it any more. One shipped —
    `stale_after_days: 30`, nineteen lines from `max_age_hours: 72` — and the
    two were mutually exclusive: a posting old enough to be flagged had been
    deleted by the freshness filter 27 days earlier. The knob validated, the
    tests passed, and it could not fire.
    """
    assert DEFAULT_REPOST_MIN_GAP_DAYS == 14
    assert DEFAULTS["freshness"]["repost_min_gap_days"] == DEFAULT_REPOST_MIN_GAP_DAYS
    assert set(DEFAULTS["freshness"]) == {
        "max_age_hours", "skip_undated", "repost_min_gap_days",
    }


def test_validate_flags_adzuna_without_keys(tmp_path: Path):
    cfg = write_config(tmp_path, {"sources": {"adzuna": True}})
    assert any("adzuna" in p for p in cfg.validate())


def test_validate_flags_all_sources_disabled(tmp_path: Path):
    cfg = write_config(
        tmp_path,
        {"sources": {"greenhouse": False, "lever": False,
                     "adzuna": False, "linkedin_email": False}},
    )
    assert any("every source is disabled" in p for p in cfg.validate())


def test_validate_flags_enabled_board_with_empty_watchlist(tmp_path: Path):
    cfg = write_config(tmp_path, {"sources": {"greenhouse": True}},
                       watchlist={"greenhouse": []})
    assert any("watchlist.greenhouse is empty" in p for p in cfg.validate())


@pytest.mark.parametrize(
    "board", ["greenhouse", "lever", "workable", "ashby", "smartrecruiters", "personio"]
)
def test_validate_flags_every_board_source_with_an_empty_watchlist(
    tmp_path: Path, board
):
    """An enabled board with nothing to fetch is the silent failure this check
    exists for: it produces zero jobs, run after run, and looks exactly like a
    quiet market. Parametrised over every vendor so adding a seventh board
    without wiring it in fails here rather than in six months of empty
    digests."""
    cfg = write_config(tmp_path, {"sources": {board: True}}, watchlist={board: []})
    assert any(f"watchlist.{board} is empty" in p for p in cfg.validate())


@pytest.mark.parametrize(
    "board", ["workable", "ashby", "smartrecruiters", "personio"]
)
def test_a_populated_watchlist_silences_the_warning(tmp_path: Path, board):
    cfg = write_config(tmp_path, {"sources": {board: True}},
                       watchlist={board: ["acme"]})
    assert not any(f"watchlist.{board}" in p for p in cfg.validate())


def test_the_european_boards_ship_off_by_default():
    """They are useless until the watchlist names companies, and shipping them
    on would mean every fresh checkout starts by printing four validation
    problems the user cannot act on yet."""
    from src.config import DEFAULTS

    for board in ("workable", "ashby", "smartrecruiters", "personio"):
        assert DEFAULTS["sources"][board] is False
        assert DEFAULTS_WATCHLIST[board] == []


def test_every_board_source_has_a_watchlist_default():
    """A source in `SOURCE_NAMES` with no `WATCHLIST_DEFAULTS` entry reads its
    slugs from `None` and fetches nothing, forever, with no warning."""
    from src.config import BOARD_SOURCE_NAMES, DEFAULTS

    for name in BOARD_SOURCE_NAMES:
        assert name in DEFAULTS["sources"], f"{name} missing from sources defaults"
        assert name in DEFAULTS_WATCHLIST, f"{name} missing from WATCHLIST_DEFAULTS"


def test_validate_flags_missing_phone_only_when_live_applying(tmp_path: Path):
    dry = write_config(tmp_path, {"applicant": {"phone": ""},
                                  "apply": {"dry_run": True}})
    assert not any("phone" in p for p in dry.validate())

    live = write_config(tmp_path, {"applicant": {"phone": ""},
                                   "apply": {"dry_run": False}})
    assert any("phone" in p for p in live.validate())


def test_validate_flags_linkedin_without_credentials(tmp_path: Path):
    cfg = write_config(tmp_path, {"sources": {"linkedin_email": True}})
    assert any("gmail_credentials.json" in p for p in cfg.validate())
