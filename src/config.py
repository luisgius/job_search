"""Configuration loading + validation.

`config.yaml` holds settings, `watchlist.yaml` holds *what to search*. Both
are merged onto the defaults below, so a partially-filled config still runs.

Secrets resolve in this order: environment variable > config.yaml > empty.
Environment always wins, so a committed config can never shadow a key the
user exported in their shell.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# --------------------------------------------------------------------------
# defaults
# --------------------------------------------------------------------------

#: The freshness window, in hours — **the** definition of it.
#:
#: This number used to be written out four times (here, `Config.validate`,
#: `filters.apply_filters` and `digest._config_summary`). Four copies of one
#: number is a latent bug: change one and the others silently disagree, so the
#: page reports a window the filter is not using.
#:
#: 72 rather than 24, and the reasoning is in `config.yaml` next to the
#: setting: `db.skip_seen_days` already guarantees each job is shown once, so a
#: wider window does not triple the digest — it only recovers what a 24-hour
#: window loses in silence (a weekend, a board that publishes late, a source
#: whose timestamp is its own ingest time).
DEFAULT_MAX_AGE_HOURS = 72

#: How long a role must already have been on the market before this listing
#: for the card to say so.
#:
#: This is the *only* ghost-job signal, and it is deliberately not posting age.
#: Age cannot be one: every posting the digest shows came through
#: `filters.is_fresh`, so it is younger than `max_age_hours` by construction —
#: at 72 hours, a "flag postings older than N days" knob can only fire for
#: N < 3, which flags everything. It was shipped anyway once (see git history),
#: and it was dead code the day it landed.
#:
#: The gap survives that argument because a re-listed role gets a *new* date
#: each time and sails through the freshness filter, so the tracker's memory —
#: not the posting's date — is what measures how long the role has been
#: circulating. That is also the quantity worth reading: "this role has been
#: advertised for eight months" is a ghost-job signal, "this posting is three
#: days old" is not.
#:
#: Two weeks is comfortably longer than any cross-source ingest lag and
#: comfortably shorter than a hiring cycle. **0 turns the flag off**, matching
#: `scoring.max_jobs: 0` ("your cost ceiling, so 0 means zero") and
#: `db.should_surface(within_days<=0)` — in this repo 0 disables a mechanism,
#: it never maximises one.
DEFAULT_REPOST_MIN_GAP_DAYS = 14

#: Phrases the opt-in cover-letter style lint (`tailoring.style`) reports.
#: The first six are adapted from ai-job-search
#: (https://github.com/ai-job-search-dev/ai-job-search),
#: .claude/skills/job-application-assistant/03-writing-style.md, MIT License,
#: © 2026 Mads Lorentzen. The rest are what `tailor.build_cover_prompt`
#: already forbids, so the lint can tell when the model ignored it.
DEFAULT_BANNED_PHRASES: tuple[str, ...] = (
    "I am passionate about",
    "I believe I would be a great fit",
    "leverage my skills",
    "hit the ground running",
    "drive results",
    "synergies",
    "I am writing to apply",
    "I'm writing to apply",
    "I am writing to express",
    "innovative culture",
)

#: CV sections whose bullets are facts to copy, never to re-word: a tailored
#: CV may drop or reorder them, but every bullet it keeps must be found in the
#: base CV (`tailoring.entity_check`). Matched case-insensitively against the
#: `## ` heading text.
DEFAULT_VERBATIM_SECTIONS: tuple[str, ...] = ("Certifications", "Languages")

DEFAULTS: dict[str, Any] = {
    "applicant": {
        "name": "",
        "email": "",
        "phone": "",
        "location": "",
        "linkedin": "",
        "github": "",
        "website": "",
    },
    "keys": {
        "anthropic": "",
        "openrouter": "",
        "adzuna_app_id": "",
        "adzuna_app_key": "",
    },
    "llm": {
        # "anthropic" talks to the SDK; "openrouter" speaks the OpenAI
        # chat-completions dialect over plain HTTP, which also covers any
        # other OpenAI-compatible gateway via `base_url`.
        "provider": "openrouter",
        "base_url": "",
        # How the scorer's JSON is obtained. "auto": grammar-constrained
        # decoding where it is a first-party feature (the Anthropic SDK) and
        # the prompt + extract_json path everywhere else — many :free ids
        # reject the OpenAI dialect's response_format. "native"/"prompt"
        # force one side. Grammar guarantees shape, not provenance: the
        # verbatim-plant validator stays on either way.
        "structured_output": "auto",
    },
    "sources": {
        "greenhouse": True,
        "lever": True,
        # The four European boards ship OFF, like adzuna and linkedin_email
        # and unlike greenhouse/lever, because they are useless until the
        # watchlist names companies: there is no sensible default company list
        # for "mid-size employers in Valencia". Populate `watchlist.yaml`,
        # prove the slugs with `--check-all`, then flip these.
        "workable": False,
        "ashby": False,
        "smartrecruiters": False,
        "personio": False,
        "recruitee": False,
        "teamtailor": False,
        "adzuna": False,
        # The two global feeds need no watchlist and no keys, but they ship
        # OFF like everything else that is not greenhouse/lever: a default
        # that quietly fetches hundreds of third-party postings on the first
        # run is a surprise, and turning a source on should always be the
        # user reading one comment and flipping one switch.
        "arbeitnow": False,
        "landing_jobs": False,
        # Tier 2: no official API — these speak the internal JSON the site's
        # own frontend uses, and may break without notice. Their contract is
        # that breakage degrades the source (a warning, an errors entry, the
        # health baseline alert) and never the run.
        "justjoin_it": False,
        "nofluffjobs": False,
        "allegro": False,
        # An aggregator of ATS boards (Workday, Oracle, SuccessFactors, …
        # that no adapter here reaches). Off like every global feed; its
        # copies lose dedupe to the direct record and never auto-apply.
        "freehire": False,
        "linkedin_email": False,
        # LinkedIn's anonymous jobs-guest search. Against LinkedIn's Terms of
        # Service (no account or cookie is sent; the risk is an IP block), so
        # it ships OFF and is turned on only by someone who accepts that.
        "linkedin_search": False,
    },
    "freshness": {
        "max_age_hours": DEFAULT_MAX_AGE_HOURS,
        # Postings without a trustworthy date cannot be proven fresh. Dropping
        # them is the honest default; flip to False to keep them.
        "skip_undated": True,
        # A FLAG ON THE CARD, never a filter. Nothing reads it to drop, hide
        # or reject a posting — it annotates a card the digest was going to
        # render anyway. See `digest._ghost_flags`.
        "repost_min_gap_days": DEFAULT_REPOST_MIN_GAP_DAYS,
    },
    "filters": {
        "countries": [
            "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE",
            "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT",
            "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "CH", "GB",
        ],
        "allow_remote": True,
        "remote_requires_eu_hint": True,
        "allow_remote_worldwide": False,
        # One posting listed once per city (No Fluff Jobs rotates the city
        # list per copy) is scored once: the profile-city copy is kept, the
        # rest are recorded as `same_posting_other_city`. See
        # `filters.collapse_variants`; it only ever merges eligible copies.
        "collapse_city_variants": True,
        "title_include": [],
        # Whole-word, accent-folded and case-insensitive (`filters._matches`),
        # so "intern" cannot reject "International Sales" — which is why this
        # list can afford to be explicit rather than clever.
        #
        # It has to carry every spelling a board actually uses, because there
        # is no stemming: "intern" does not catch "internships", "apprentice"
        # does not catch "apprenticeship", and "graduate program" does not
        # catch the British "graduate programme". And a search across Berlin,
        # Paris, Madrid, Amsterdam and Warsaw is not an English-language
        # search: every entry-level term below turns up in one week of it, and
        # each one that gets through costs a paid LLM call and a slot in the
        # digest.
        "title_exclude": [
            # English
            "intern", "interns", "internship", "internships",
            "apprentice", "apprentices", "apprenticeship", "apprenticeships",
            "graduate program", "graduate programs",
            "graduate programme", "graduate programmes",
            "graduate scheme", "graduate schemes",
            "working student", "volunteer",
            # German
            "werkstudent", "praktikum", "praktikant", "praktikantin",
            "ausbildung", "auszubildende", "azubi", "duales studium",
            # French
            "stage", "stagiaire", "alternance", "alternant", "apprentissage",
            # Spanish / Portuguese
            "becario", "becaria", "practicas", "estagio", "estagiario",
            # Dutch ("stagiaire" is already listed under French)
            "stagiair", "afstudeerstage",
            # Polish
            "praktyki", "praktykant", "staz", "stazysta",
            # Italian
            "tirocinio", "stagista",
        ],
        "description_exclude": [
            "security clearance", "must be a us citizen", "ts/sci",
        ],
        # Employment types a source states as structured data (Lever's
        # `categories.commitment`, Adzuna's `contract_type`). Matched only
        # against those fields, never against the title, and a posting that
        # states nothing is never rejected here.
        #
        # Trade-off, stated because the default is opinionated: this drops
        # genuine contract/freelance work for anyone who wants it, and that is
        # one config line to undo, visible in the digest's funnel counts with
        # an explicit reason. The other default silently sends a
        # permanent-role hunter to pay for scoring internships every morning.
        # Only the types that are already excluded by title, so this stage
        # catches the ones whose title is neutral. `contract`/`freelance` are
        # deliberately NOT here: plenty of real EU tech work is contract, and
        # a default that deletes jobs is the wrong kind of opinionated — a
        # dropped posting is invisible forever, while a wrong card costs a
        # glance. Add them yourself if you only want permanent roles.
        "employment_type_exclude": [
            "internship", "intern", "apprenticeship", "apprentice",
            "trainee", "temporary", "temp", "seasonal", "volunteer",
            "work experience",
        ],
        "require_keywords_any": [],
        "min_description_chars": 0,
        # Metadata stamp, never a gate: an included title that also carries
        # one of these markers gets `raw["level"] = "junior"`, so scoring and
        # the digest can say what the ad itself declares. A plain title is
        # NOT junior — mid is the target, junior is acceptable, not default.
        "title_junior_markers": [
            "junior", "associate", "graduate", "early career", "entry level",
        ],
        # Legacy ad-language settings are retained for config compatibility only.
        # Hard language checks use explicit job requirements and known CV levels.
        "languages": [],
        "language_min_chars": 150,
        "language_levels": {},
        "title_adjacent": [],
        # Countries reachable only with the employer's help: allowed when —
        # and only when — the posting explicitly offers visa sponsorship.
        "countries_if_sponsorship": [],
    },
    "scoring": {
        "model": "anthropic/claude-sonnet-5",
        # Tried in order when `model` cannot answer (spent daily quota,
        # rotated :free id, outage). A string inherits llm.provider/base_url;
        # a mapping may override them, plus `timeout` (HTTP seconds — the
        # patience a laptop-sized model needs) and `max_retries` (after the
        # first attempt; 0 for a local model, since a slow answer does not
        # get faster on retry) — which is how a local gateway joins:
        #   fallback_models:
        #     - google/gemma-4-31b-it:free
        #     - {model: "qwen3.8:27b", base_url: "http://localhost:11434/v1",
        #        timeout: 600, max_retries: 0}
        "fallback_models": [],
        "threshold": 65,
        "max_jobs": 40,
        "backlog_max_jobs": 1000,
        "backlog_max_age_days": 7,
        "retry_max_attempts": 3,
        "retry_base_hours": 1,
        "max_tokens": 1500,
        "temperature": 0.0,
        "concurrency": 4,
        # Prompt-only personalisation, empty by default — see config.yaml for
        # the shipped example. None of these move threshold or max_jobs.
        "candidate_context": "",
        "positive_signals": [],
        "minor_gaps": [],
        "score_caps": [],
        # Off: caps stay prompt-only, exactly as above. On: each cap is named
        # by its `id` (default `cap-<n>`) and a score is held, in code, to
        # the smallest cap the model lists in `caps_fired`.
        "enforce_caps": False,
    },
    "tailoring": {
        "enabled": True,
        "model": "anthropic/claude-sonnet-5",
        # Same shape and semantics as scoring.fallback_models.
        "fallback_models": [],
        "max_per_run": 10,
        "max_tokens": 4000,
        "temperature": 0.2,
        # Replace everything above the first `## ` heading with the base CV's
        # header plus the `applicant` contact fields it lacks. The header is
        # identity, never tailoring, and models mangle it ("Name: …" lines).
        "pin_header": True,
        # Reject a CV whose `###` headings (employer, title, degree) match no
        # base-CV heading, or whose `cv.verbatim_sections` bullets are new.
        "entity_check": True,
        # "Mar 2026 – Present" -> "Mar 2026 - Present"; some ATS parsers only
        # split ranges on the ASCII hyphen. Quantity ranges are left alone.
        "ascii_date_ranges": False,
        "style": {
            # off | flag | repair. `flag` adds cover-letter flags (which block
            # live apply); `repair` spends one corrective call first.
            "mode": "off",
            "banned_phrases": list(DEFAULT_BANNED_PHRASES),
            "max_em_dashes": 1,
        },
    },
    "apply": {
        "enabled": True,
        "dry_run": True,
        "max_per_run": 5,
        "min_score": 80,
        "headless": True,
        "timeout_seconds": 60,
        "require_pdf": True,
        # A posting the posting guard flagged (instruction-shaped text, hidden
        # tag characters) is never submitted live; dry runs still fill it.
        "block_on_posting_guard": True,
    },
    "pdf": {
        # Read each rendered PDF back as text: missing glyphs (■), name or
        # email absent, too many pages, a heading stranded at a page foot.
        # off | warn (run errors + job status) | block (also withhold the
        # PDF, so apply.require_pdf sends the job to the digest).
        "verify": "off",
        # pdftotext binary; empty = PATH, then /opt/homebrew/bin, /usr/local/bin.
        "pdftotext": "",
        "cv_max_pages": 2,
        "cover_max_pages": 1,
    },
    "output": {
        "dir": "output",
        "open_browser": True,
    },
    "db": {
        "path": "output/tracker.sqlite3",
        # Dated daily copies of the tracker in output/backups/ — the one file
        # in output/ that cannot be regenerated. 0 turns backups off.
        "backups_keep": 14,
        # A job already seen within this window is not re-surfaced in the
        # digest. Applications are blocked forever regardless.
        "skip_seen_days": 30,
    },
    "tracking": {
        # The digest's "Follow-ups due" section, fed by what you record with
        # `python -m src.track`. Read-only: the page suggests a command, it
        # never records anything. 0 turns a check off; max_followups: 0
        # means never suggest a follow-up.
        "followup_days": 10,    # quiet this long since the last activity -> due
        "stale_days": 60,       # quiet this long -> suggest no_response instead
        "max_followups": 2,     # nudges per stage before the page stops asking
    },
    "cv": {
        "path": "cv/base_cv.md",
        # Optional per-role presentations of the SAME facts, picked per job at
        # tailoring time by whole-word title match (first hit wins; no hit
        # falls back to `path`). Scoring always reads `path`: the variants
        # carry identical facts, so scoring each job against its variant would
        # buy nothing and cost a second copy of the truth to keep in sync.
        # Each entry: {path: cv/base_cv_ml.md, title_terms: [ml engineer, …]}.
        "variants": [],
        "verbatim_sections": list(DEFAULT_VERBATIM_SECTIONS),
    },
    "notify": {
        # The failure this exists for is the quiet one: a morning where every
        # board 404'd looks exactly like a genuinely quiet Tuesday.
        "enabled": True,
        # Alert kinds to deliver; see health.ALERT_KINDS. `errors` is omitted
        # by default because a single flaky board is noise, not news.
        "on": ["no_digest", "missed_run", "no_jobs", "source_zero",
               "all_sources_failed"],
        "channels": {
            "console": True,
            "file": True,
            "command": "",   # e.g. "notify-send" / "terminal-notifier -message"
            "email": {},     # {to, from, smtp_host, smtp_port, username, starttls}
        },
        # Exit non-zero when a run raises an alert. Off by default because it
        # changes the documented exit codes; turn it on if your scheduler
        # notices failures for you.
        "exit_nonzero": False,
    },
    "logging": {
        "level": "INFO",
    },
}

#: Sources that are just a list of board slugs in the watchlist, in fetch
#: order. `ats_boards.BOARDS` serves all of them in a single `fetch()` call,
#: and `validate()` warns about each one that is enabled with nothing to fetch.
BOARD_SOURCE_NAMES: tuple[str, ...] = (
    "greenhouse", "lever", "workable", "ashby", "smartrecruiters", "personio",
    "recruitee", "teamtailor",
)

#: Every source the config knows about, in fetch order.
SOURCE_NAMES: tuple[str, ...] = BOARD_SOURCE_NAMES + (
    "adzuna", "arbeitnow", "landing_jobs", "justjoin_it", "nofluffjobs",
    "allegro", "freehire", "linkedin_email", "linkedin_search",
)

#: Search parameters `sources/freehire.py` sets itself. A query may not set
#: them: paging and format are the adapter's, and `posted_within_days`
#: filters on freehire's re-dated `posted_at`, not on when a posting appeared.
FREEHIRE_RESERVED_PARAMS: frozenset[str] = frozenset({
    "limit", "offset", "description_format", "posted_within_days",
})

WATCHLIST_DEFAULTS: dict[str, Any] = {
    "greenhouse": [],
    "lever": [],
    "workable": [],
    "ashby": [],
    "smartrecruiters": [],
    "personio": [],
    "recruitee": [],
    "teamtailor": [],
    "allegro": {
        "max_pages": 2,
        "max_details": 8,
        "max_requests": 12,
        "timeout_seconds": 20,
        "detail_urls": [],
    },
    "freehire": {
        # Search facets, sent as given (a list becomes a repeated param):
        # Poland, and remote roles open to the EU or worldwide, both cut to
        # freehire's data/AI categories.
        "queries": [
            {"countries": ["pl"],
             "category": ["data_engineering", "ai_engineering", "data_analytics",
                          "data_science", "ml_ai"]},
            {"work_mode": "remote", "regions": ["eu", "global"],
             "category": ["data_engineering", "ai_engineering", "data_analytics",
                          "data_science", "ml_ai"]},
        ],
        "limit": 50,
        "max_requests": 6,
        # Upstreams that are themselves aggregators: reposts, not employers.
        "exclude_upstream": ["whatjobs-pl", "energyjobline"],
    },
    "adzuna": {
        "countries": [],
        "queries": [],
        "max_days_old": 1,
        "results_per_page": 50,
        "distance_km": 0,
    },
    "linkedin_search": {
        # keywords + location as typed into LinkedIn's search box; work_type
        # is "remote", "hybrid" or "onsite" (LinkedIn's f_WT).
        "queries": [
            {"keywords": "data scientist", "location": "Poland"},
            {"keywords": "machine learning engineer", "location": "Poland"},
        ],
        "past_hours": 24,       # LinkedIn's f_TPR window
        "max_requests": 12,     # search pages per run, 10 cards each, shared
        "max_descriptions": 30, # detail pages, only for cards past the filters
        "delay_seconds": 2.0,   # between requests, jittered up to +50%
    },
    "linkedin_email": {
        "gmail_query": "from:jobalerts-noreply@linkedin.com newer_than:2d",
        "max_messages": 25,
        "credentials_file": "gmail_credentials.json",
        "token_file": "gmail_token.json",
        "fetch_descriptions": True,
    },
}

# config path -> environment variable that overrides it
ENV_OVERRIDES: dict[tuple[str, ...], str] = {
    ("keys", "anthropic"): "ANTHROPIC_API_KEY",
    ("keys", "openrouter"): "OPENROUTER_API_KEY",
    ("keys", "adzuna_app_id"): "ADZUNA_APP_ID",
    ("keys", "adzuna_app_key"): "ADZUNA_APP_KEY",
    # Not a key, but the same rule: PII the tracked file must never carry.
    # Live auto-apply needs a phone; the phone stays out of git history.
    ("applicant", "phone"): "JOB_HUNTER_PHONE",
}


class ConfigError(RuntimeError):
    """Raised when the config is present but unusable."""


def deep_merge(base: dict[str, Any], override: Any) -> dict[str, Any]:
    """Recursively merge `override` onto a copy of `base`.

    Lists and scalars replace wholesale; only dicts merge key-by-key. A
    `None` override (an empty YAML block) is treated as "not specified".
    """
    result = copy.deepcopy(base)
    if not isinstance(override, dict):
        return result
    for key, value in override.items():
        if value is None and key in result:
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class _NoDuplicateKeysLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` plus one refusal: a mapping with a duplicated key.

    PyYAML keeps the *last* value for a duplicated key and raises nothing, so a
    watchlist that ends in a second `greenhouse:` block — say, one pasted from
    `--discover` — silently deletes every company under the first one. From the
    next morning on those boards are simply never fetched, which reads as a
    quiet market rather than as a mistake: the exact silent job loss
    `src/health.py` exists to catch, delivered by the config loader with no
    error attached. Refusing to load the file is the only honest answer.

    Checked at every nesting level, not just the top: two `path:` keys under
    `db:` are the same mistake at smaller scale. YAML merge keys (`<<:`) are
    exempt — overriding a merged key with an explicit one is the documented
    point of the feature, not a paste accident.
    """

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        if isinstance(node, yaml.MappingNode):
            seen: set[Any] = set()
            for key_node, _value_node in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=True)
                try:
                    duplicated = key in seen
                except TypeError:  # unhashable key: SafeLoader has its own error
                    continue
                if duplicated:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping", node.start_mark,
                        f"found duplicate key {key!r} — YAML keeps only the "
                        "last one, silently discarding everything under the "
                        "first", key_node.start_mark,
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _read_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        loaded = yaml.load(p.read_text(encoding="utf-8"), Loader=_NoDuplicateKeysLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p} is not usable YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{p} must contain a YAML mapping at the top level")
    return loaded


@dataclass
class Config:
    """Parsed configuration. `data` is the merged config.yaml mapping."""

    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    watchlist: dict[str, Any] = field(
        default_factory=lambda: copy.deepcopy(WATCHLIST_DEFAULTS)
    )
    root: Path = field(default_factory=Path.cwd)

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(
        cls,
        config_path: str | Path = "config.yaml",
        watchlist_path: str | Path = "watchlist.yaml",
        root: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> "Config":
        env = os.environ if env is None else env
        cfg = deep_merge(DEFAULTS, _read_yaml(config_path))
        watch = deep_merge(WATCHLIST_DEFAULTS, _read_yaml(watchlist_path))

        for path, var in ENV_OVERRIDES.items():
            value = env.get(var)
            if value:
                node = cfg
                for part in path[:-1]:
                    node = node.setdefault(part, {})
                node[path[-1]] = value

        base = Path(root) if root is not None else Path(config_path).resolve().parent
        return cls(data=cfg, watchlist=watch, root=base)

    # -- access -----------------------------------------------------------

    def get(self, dotted: str, default: Any = None) -> Any:
        """`cfg.get("apply.dry_run")` -> bool. Missing paths yield `default`."""
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self.data.get(name)
        return value if isinstance(value, dict) else {}

    def path(self, dotted: str, default: Any = None) -> Path:
        """Resolve a configured path relative to the project root."""
        value = self.get(dotted, default)
        p = Path(str(value))
        return p if p.is_absolute() else (self.root / p)

    # -- frequently used shortcuts ---------------------------------------

    @property
    def applicant(self) -> dict[str, Any]:
        return self.section("applicant")

    @property
    def provider(self) -> str:
        return str(self.get("llm.provider", "openrouter") or "openrouter").strip().lower()

    @property
    def llm_key(self) -> str:
        """The key belonging to the *configured* provider.

        Always ask via the provider, never by hard-coding `keys.anthropic`:
        reading the wrong one is how a switched provider silently keeps using
        the old credential.
        """
        return str(self.get(f"keys.{self.provider}") or "")

    @property
    def anthropic_key(self) -> str:
        # Kept for callers that genuinely mean Anthropic specifically.
        return str(self.get("keys.anthropic") or "")

    @property
    def output_dir(self) -> Path:
        return self.path("output.dir", "output")

    @property
    def db_path(self) -> Path:
        return self.path("db.path", "output/tracker.sqlite3")

    @property
    def cv_path(self) -> Path:
        return self.path("cv.path", "cv/base_cv.md")

    def source_enabled(self, name: str) -> bool:
        return bool(self.get(f"sources.{name}", False))

    # -- validation -------------------------------------------------------

    def validate(self, *, require_llm: bool = True) -> list[str]:
        """Return a list of human-readable problems. Empty == good to go.

        Deliberately returns rather than raises: `main` prints every problem
        at once instead of making the user fix them one run at a time.
        """
        problems: list[str] = []
        applicant = self.applicant

        if not str(applicant.get("name") or "").strip():
            problems.append("applicant.name is empty — set it in config.yaml")
        email = str(applicant.get("email") or "").strip()
        if not email:
            problems.append("applicant.email is empty — set it in config.yaml")
        elif "@" not in email or "." not in email.split("@")[-1]:
            problems.append(f"applicant.email does not look like an address: {email!r}")

        provider = self.provider
        if provider not in ("anthropic", "openrouter"):
            problems.append(
                f"llm.provider must be 'anthropic' or 'openrouter', got {provider!r}"
            )
        elif require_llm and not self.llm_key:
            env = {"anthropic": "ANTHROPIC_API_KEY",
                   "openrouter": "OPENROUTER_API_KEY"}[provider]
            problems.append(
                f"No {provider} API key — set keys.{provider} in config.yaml "
                f"or export {env}"
            )

        # OpenRouter model ids are vendor-qualified. A bare "claude-sonnet-5"
        # is a 404 from OpenRouter, and the run would burn a stage discovering
        # that once per job.
        if provider == "openrouter":
            for path in ("scoring.model", "tailoring.model"):
                model = str(self.get(path) or "")
                if model and "/" not in model:
                    problems.append(
                        f"{path} is {model!r}, but OpenRouter model ids are "
                        f"vendor-qualified — try 'anthropic/{model}'"
                    )

        # The CV only feeds scoring and tailoring, so it is required on the
        # same terms as the API key: `--no-llm` needs neither.
        if require_llm and not self.cv_path.exists():
            problems.append(f"CV not found at {self.cv_path} — paste your CV there")

        # Variants are optional, but a *configured* variant that cannot work
        # is a config problem on the same terms as the CV itself: the run
        # would silently tailor every ML job from the wrong presentation.
        variants = self.get("cv.variants", []) or []
        if not isinstance(variants, list):
            problems.append("cv.variants must be a list of {path, title_terms} entries")
            variants = []
        for index, entry in enumerate(variants):
            where = f"cv.variants[{index}]"
            if not isinstance(entry, dict):
                problems.append(f"{where} must be a mapping with path and title_terms")
                continue
            raw_path = str(entry.get("path") or "").strip()
            if not raw_path:
                problems.append(f"{where} has no path")
            else:
                resolved = Path(raw_path)
                if not resolved.is_absolute():
                    resolved = self.root / resolved
                if require_llm and not resolved.exists():
                    problems.append(f"{where}: CV variant not found at {raw_path}")
            terms = entry.get("title_terms")
            if not isinstance(terms, list) or not any(str(t).strip() for t in terms):
                problems.append(
                    f"{where} has no title_terms — a variant nothing can ever "
                    "match is dead config"
                )

        # `isinstance(True, int)` is True, and YAML turns a bare `yes` into a
        # bool — so bools have to be rejected explicitly or `threshold: yes`
        # silently becomes a threshold of 1.
        threshold = self.get("scoring.threshold", 65)
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
                or not 0 <= threshold <= 100:
            problems.append(f"scoring.threshold must be 0-100, got {threshold!r}")

        # A quoted "true" would read as on to a human and as off to scoring,
        # which only honours a real boolean.
        enforce_caps = self.get("scoring.enforce_caps", False)
        if enforce_caps is not None and not isinstance(enforce_caps, bool):
            problems.append(
                f"scoring.enforce_caps must be true or false, got {enforce_caps!r}"
            )
        # A cap's id is what the model names in `caps_fired`; two caps
        # answering to one id would make the enforced ceiling ambiguous.
        caps = self.get("scoring.score_caps", [])
        cap_ids: list[str] = []
        for index, entry in enumerate(caps if isinstance(caps, list) else [], 1):
            if not isinstance(entry, dict):
                continue
            cap_id = entry.get("id")
            if cap_id is not None and (not isinstance(cap_id, str) or not cap_id.strip()):
                problems.append(
                    f"scoring.score_caps[{index}].id must be a non-empty string, got {cap_id!r}"
                )
                continue
            cap_ids.append(str(cap_id or "").strip() or f"cap-{index}")
        for duplicate in sorted({i for i in cap_ids if cap_ids.count(i) > 1}):
            problems.append(f"scoring.score_caps has more than one cap with id {duplicate!r}")

        max_age = self.get("freshness.max_age_hours", DEFAULT_MAX_AGE_HOURS)
        for name, minimum, maximum in (
            ("backlog_max_jobs", 0, 10000),
            ("backlog_max_age_days", 1, 30),
            ("retry_max_attempts", 1, 10),
            ("retry_base_hours", 1, 168),
        ):
            value = self.get(f"scoring.{name}", DEFAULTS["scoring"][name])
            if isinstance(value, bool) or not isinstance(value, int) \
                    or not minimum <= value <= maximum:
                problems.append(
                    f"scoring.{name} must be an integer {minimum}-{maximum}, got {value!r}"
                )
        if isinstance(max_age, bool) or not isinstance(max_age, (int, float)) \
                or max_age <= 0:
            problems.append(f"freshness.max_age_hours must be > 0, got {max_age!r}")

        # The ghost-job threshold only ever colours a card, so a bad value
        # costs a wrong flag rather than a lost posting. Still checked: a
        # setting that is silently ignored is worse than one that is rejected,
        # because the user believes it took effect. Zero is legal and means
        # "turn the flag off" — the same thing 0 means for `scoring.max_jobs`
        # and for `db.should_surface`'s window.
        gap = self.get("freshness.repost_min_gap_days", DEFAULT_REPOST_MIN_GAP_DAYS)
        if isinstance(gap, bool) or not isinstance(gap, (int, float)) or gap < 0:
            problems.append(
                f"freshness.repost_min_gap_days must be >= 0 (0 = off), got {gap!r}"
            )

        collapse = self.get("filters.collapse_city_variants", True)
        if not isinstance(collapse, bool):
            problems.append(
                f"filters.collapse_city_variants must be true or false, got {collapse!r}"
            )
        # Advisory like the repost flag: a wrong value costs a wrong nudge.
        for name, default in DEFAULTS["tracking"].items():
            value = self.get(f"tracking.{name}", default)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                problems.append(
                    f"tracking.{name} must be a whole number >= 0 (0 = off), got {value!r}"
                )

        if self.source_enabled("adzuna") and not (
            self.get("keys.adzuna_app_id") and self.get("keys.adzuna_app_key")
        ):
            problems.append(
                "sources.adzuna is on but keys.adzuna_app_id / keys.adzuna_app_key "
                "are missing"
            )

        if self.source_enabled("linkedin_email"):
            creds = self.watchlist.get("linkedin_email", {}).get(
                "credentials_file", "gmail_credentials.json"
            )
            if not (self.root / creds).exists():
                problems.append(
                    f"sources.linkedin_email is on but {creds} is missing "
                    "(download OAuth desktop credentials from Google Cloud Console)"
                )

        if self.source_enabled("linkedin_search"):
            settings = self.watchlist.get("linkedin_search")
            settings = settings if isinstance(settings, dict) else {}
            queries = settings.get("queries")
            if not isinstance(queries, list) or not queries:
                problems.append(
                    "sources.linkedin_search is on but "
                    "watchlist.linkedin_search.queries is empty"
                )
                queries = []
            for index, query in enumerate(queries):
                keywords = query.get("keywords") if isinstance(query, dict) else None
                if not isinstance(keywords, str) or not keywords.strip():
                    problems.append(
                        f"watchlist.linkedin_search.queries[{index}] needs keywords"
                    )
                elif str(query.get("work_type") or "onsite").lower() not in (
                    "remote", "hybrid", "onsite", "on-site"
                ):
                    problems.append(
                        f"watchlist.linkedin_search.queries[{index}].work_type "
                        "must be remote, hybrid or onsite"
                    )
            for name, maximum in (("max_requests", 40), ("max_descriptions", 80),
                                  ("past_hours", 720)):
                value = settings.get(name, WATCHLIST_DEFAULTS["linkedin_search"][name])
                if isinstance(value, bool) or not isinstance(value, int) \
                        or not 1 <= value <= maximum:
                    problems.append(
                        f"watchlist.linkedin_search.{name} must be an integer "
                        f"1-{maximum}, got {value!r}"
                    )

        # freehire's queries go to the API as written: a malformed one fetches
        # nothing, or the wrong market, every morning without a word.
        if self.source_enabled("freehire"):
            settings = self.watchlist.get("freehire")
            settings = settings if isinstance(settings, dict) else {}
            queries = settings.get("queries")
            if not isinstance(queries, list) or not queries:
                problems.append(
                    "sources.freehire is on but watchlist.freehire.queries is empty"
                )
                queries = []
            for index, query in enumerate(queries):
                if not isinstance(query, dict) or not query:
                    problems.append(
                        f"watchlist.freehire.queries[{index}] must be a mapping "
                        "of search facets"
                    )
                    continue
                owned = sorted({str(k) for k in query} & FREEHIRE_RESERVED_PARAMS)
                if owned:
                    problems.append(
                        f"watchlist.freehire.queries[{index}] sets "
                        f"{', '.join(owned)} — paging and format are the "
                        "adapter's, and posted_within_days filters on "
                        "freehire's re-dated posted_at"
                    )
            for name, maximum in (("limit", 100), ("max_requests", 20)):
                value = settings.get(name, WATCHLIST_DEFAULTS["freehire"][name])
                if isinstance(value, bool) or not isinstance(value, int) \
                        or not 1 <= value <= maximum:
                    problems.append(
                        f"watchlist.freehire.{name} must be an integer "
                        f"1-{maximum}, got {value!r}"
                    )
            excluded = settings.get("exclude_upstream", [])
            if not isinstance(excluded, list) or not all(
                isinstance(name, str) for name in excluded
            ):
                problems.append(
                    "watchlist.freehire.exclude_upstream must be a list of "
                    "upstream source names"
                )

        enabled = [n for n in SOURCE_NAMES if self.source_enabled(n)]
        if not enabled:
            problems.append("every source is disabled — nothing to fetch")

        # An enabled board with an empty watchlist is the silent failure this
        # whole check exists for: it produces zero jobs and looks exactly like
        # a quiet day, run after run, with nothing in the log to act on.
        for name in BOARD_SOURCE_NAMES:
            if self.source_enabled(name) and not self.watchlist.get(name):
                problems.append(
                    f"sources.{name} is on but watchlist.{name} is empty"
                )

        mode = str(self.get("llm.structured_output", "auto") or "auto")
        if mode.strip().lower() not in ("auto", "native", "prompt"):
            problems.append(
                f"llm.structured_output must be auto, native or prompt — got {mode!r}"
            )

        # YAML reads a bare `off` as False; a bare `on` (True) is ambiguous
        # between warn and block, so it is refused rather than guessed.
        verify = self.get("pdf.verify", "off")
        if verify is not False and str(verify).strip().lower() not in (
            "off", "warn", "block"
        ):
            problems.append(f"pdf.verify must be off, warn or block — got {verify!r}")
        for name in ("cv_max_pages", "cover_max_pages"):
            pages = self.get(f"pdf.{name}", DEFAULTS["pdf"][name])
            if isinstance(pages, bool) or not isinstance(pages, int) or pages < 1:
                problems.append(
                    f"pdf.{name} must be a whole number, 1 or more — got {pages!r}"
                )

        for role in ("scoring", "tailoring"):
            chain = self.get(f"{role}.fallback_models", [])
            if chain in (None, ""):
                continue
            if not isinstance(chain, list):
                problems.append(f"{role}.fallback_models must be a list")
                continue
            for index, entry in enumerate(chain):
                if isinstance(entry, str) and entry.strip():
                    continue
                if isinstance(entry, dict) and str(entry.get("model") or "").strip():
                    # A number that fails to parse would silently keep the
                    # defaults (120 s, two retries) — and the local entry
                    # that exists for when the network is gone would die as
                    # "transient", or wait half an hour per job.
                    timeout = entry.get("timeout")
                    if timeout is not None and (
                        isinstance(timeout, bool)
                        or not isinstance(timeout, (int, float))
                        or timeout < 1
                    ):
                        problems.append(
                            f"{role}.fallback_models[{index}].timeout must be a "
                            f"number of seconds, at least 1 — got {timeout!r}"
                        )
                    effort = entry.get("reasoning_effort")
                    if effort is not None and effort not in ("none", "low", "medium", "high"):
                        problems.append(f"{role}.fallback_models[{index}].reasoning_effort must be none, low, medium or high")
                    tokens = entry.get("max_tokens")
                    if tokens is not None and (type(tokens) is not int or not 1 <= tokens <= 32768):
                        problems.append(f"{role}.fallback_models[{index}].max_tokens must be an integer from 1 to 32768")
                    retries = entry.get("max_retries")
                    if retries is not None and (
                        isinstance(retries, bool)
                        or not isinstance(retries, int)
                        or retries < 0
                    ):
                        problems.append(
                            f"{role}.fallback_models[{index}].max_retries must "
                            f"be a whole number, 0 or more — got {retries!r}"
                        )
                    continue
                problems.append(
                    f"{role}.fallback_models[{index}] must be a model id or a "
                    "mapping with `model` (and optional provider/base_url/"
                    "timeout/max_retries)"
                )

        # A quoted "false" is a truthy string: the gate would stay on while the
        # user believes they turned it off. Say so instead.
        block = self.get("apply.block_on_posting_guard", True)
        if not isinstance(block, bool):
            problems.append(
                f"apply.block_on_posting_guard must be true or false, got {block!r}"
            )
        # A quoted "false" is a truthy string, so a check the user believes
        # is off would silently stay on (or the reverse).
        for name in ("pin_header", "entity_check", "ascii_date_ranges"):
            value = self.get(f"tailoring.{name}", DEFAULTS["tailoring"][name])
            if not isinstance(value, bool):
                problems.append(f"tailoring.{name} must be true or false, got {value!r}")
        # YAML reads a bare `off` as False, which is what the user meant.
        style_mode = self.get("tailoring.style.mode", "off")
        if style_mode is not False and str(style_mode).strip().lower() not in (
            "off", "flag", "repair"
        ):
            problems.append(
                f"tailoring.style.mode must be off, flag or repair — got {style_mode!r}"
            )
        dashes = self.get("tailoring.style.max_em_dashes", 1)
        if isinstance(dashes, bool) or not isinstance(dashes, int) or dashes < 0:
            problems.append(
                f"tailoring.style.max_em_dashes must be a whole number, 0 or more — "
                f"got {dashes!r}"
            )
        for path in ("tailoring.style.banned_phrases", "cv.verbatim_sections"):
            value = self.get(path, [])
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                problems.append(f"{path} must be a list of non-empty strings")

        # Loud about the one setting that can send email on the user's behalf.
        if self.get("apply.enabled") and not self.get("apply.dry_run"):
            if not str(applicant.get("phone") or "").strip():
                problems.append(
                    "apply.dry_run is false but applicant.phone is empty — "
                    "most ATS forms require a phone number"
                )
        return problems
