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

from src.config import DEFAULTS, Config, ConfigError, deep_merge
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


def test_shipped_config_and_watchlist_parse():
    """The files a new user actually edits must load without surprises."""
    root = Path(__file__).resolve().parent.parent
    cfg = Config.load(root / "config.yaml", root / "watchlist.yaml", root=root, env={})
    assert cfg.get("apply.dry_run") is True, "shipped config must default to dry run"
    assert cfg.get("scoring.threshold") == 65
    assert isinstance(cfg.watchlist.get("greenhouse"), list)


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
        {"applicant": {"name": "", "email": ""}, "keys": {"anthropic": ""}},
        cv=None,
    )
    problems = cfg.validate()
    joined = " ".join(problems).lower()
    assert len(problems) >= 4
    assert "applicant.name" in joined
    assert "applicant.email" in joined
    assert "anthropic" in joined
    assert "cv" in joined


def test_validate_rejects_a_malformed_email(tmp_path: Path):
    cfg = write_config(tmp_path, {"applicant": {"email": "ada-at-example"}})
    assert any("does not look like an address" in p for p in cfg.validate())


def test_validate_can_skip_the_llm_key_check(tmp_path: Path):
    cfg = write_config(tmp_path, {"keys": {"anthropic": ""}})
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
