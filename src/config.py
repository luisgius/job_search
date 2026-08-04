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
        "adzuna_app_id": "",
        "adzuna_app_key": "",
    },
    "sources": {
        "greenhouse": True,
        "lever": True,
        "adzuna": False,
        "linkedin_email": False,
    },
    "freshness": {
        "max_age_hours": 24,
        # Postings without a trustworthy date cannot be proven fresh. Dropping
        # them is the honest default; flip to False to keep them.
        "skip_undated": True,
    },
    "filters": {
        "countries": [
            "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE",
            "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT",
            "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "CH", "GB",
        ],
        "allow_remote": True,
        "remote_requires_eu_hint": True,
        "title_include": [],
        "title_exclude": [
            "intern", "internship", "working student", "werkstudent",
            "praktikum", "apprentice", "graduate program", "volunteer",
        ],
        "description_exclude": [
            "security clearance", "must be a us citizen", "ts/sci",
        ],
        "require_keywords_any": [],
        "min_description_chars": 0,
    },
    "scoring": {
        "model": "claude-sonnet-5",
        "threshold": 65,
        "max_jobs": 40,
        "max_tokens": 1500,
        "temperature": 0.0,
        "concurrency": 4,
    },
    "tailoring": {
        "enabled": True,
        "model": "claude-sonnet-5",
        "max_per_run": 10,
        "max_tokens": 4000,
        "temperature": 0.2,
    },
    "apply": {
        "enabled": True,
        "dry_run": True,
        "max_per_run": 5,
        "min_score": 80,
        "headless": True,
        "timeout_seconds": 60,
        "require_pdf": True,
    },
    "output": {
        "dir": "output",
        "open_browser": True,
    },
    "db": {
        "path": "output/tracker.sqlite3",
        # A job already seen within this window is not re-surfaced in the
        # digest. Applications are blocked forever regardless.
        "skip_seen_days": 30,
    },
    "cv": {
        "path": "cv/base_cv.md",
    },
    "logging": {
        "level": "INFO",
    },
}

WATCHLIST_DEFAULTS: dict[str, Any] = {
    "greenhouse": [],
    "lever": [],
    "adzuna": {
        "countries": [],
        "queries": [],
        "max_days_old": 1,
        "results_per_page": 50,
        "distance_km": 0,
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
    ("keys", "adzuna_app_id"): "ADZUNA_APP_ID",
    ("keys", "adzuna_app_key"): "ADZUNA_APP_KEY",
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


def _read_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p} is not valid YAML: {exc}") from exc
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
    def anthropic_key(self) -> str:
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

        if require_llm and not self.anthropic_key:
            problems.append(
                "No Anthropic API key — set keys.anthropic in config.yaml "
                "or export ANTHROPIC_API_KEY"
            )

        if not self.cv_path.exists():
            problems.append(f"CV not found at {self.cv_path} — paste your CV there")

        # `isinstance(True, int)` is True, and YAML turns a bare `yes` into a
        # bool — so bools have to be rejected explicitly or `threshold: yes`
        # silently becomes a threshold of 1.
        threshold = self.get("scoring.threshold", 65)
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
                or not 0 <= threshold <= 100:
            problems.append(f"scoring.threshold must be 0-100, got {threshold!r}")

        max_age = self.get("freshness.max_age_hours", 24)
        if isinstance(max_age, bool) or not isinstance(max_age, (int, float)) \
                or max_age <= 0:
            problems.append(f"freshness.max_age_hours must be > 0, got {max_age!r}")

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

        enabled = [n for n in ("greenhouse", "lever", "adzuna", "linkedin_email")
                   if self.source_enabled(n)]
        if not enabled:
            problems.append("every source is disabled — nothing to fetch")

        if self.source_enabled("greenhouse") and not self.watchlist.get("greenhouse"):
            problems.append("sources.greenhouse is on but watchlist.greenhouse is empty")
        if self.source_enabled("lever") and not self.watchlist.get("lever"):
            problems.append("sources.lever is on but watchlist.lever is empty")

        # Loud about the one setting that can send email on the user's behalf.
        if self.get("apply.enabled") and not self.get("apply.dry_run"):
            if not str(applicant.get("phone") or "").strip():
                problems.append(
                    "apply.dry_run is false but applicant.phone is empty — "
                    "most ATS forms require a phone number"
                )
        return problems
