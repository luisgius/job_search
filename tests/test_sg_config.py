from pathlib import Path

import pytest
import yaml

from src.singapore.config import enabled, load_yaml, public_api_robots, validate_entries
from src.singapore.main import due, mode_for_date
from datetime import datetime, timezone


def test_reachable_endpoint_does_not_authorize_automation():
    assert not enabled({"status": "verified", "automation_allowed": "true"})
    assert not enabled({"status": "candidate", "automation_allowed": True})
    assert enabled({"status": "verified", "automation_allowed": True})


def test_enabled_company_requires_verification_and_policy_evidence():
    row = {"name": "Acme", "ats": "lever", "fs": False, "status": "verified", "automation_allowed": True}
    errors = validate_entries([row], [])
    assert any("robots_url" in item for item in errors)
    assert any("terms_url" in item for item in errors)
    assert any("evidence" in item for item in errors)


def test_yaml_rejects_invalid_topology(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("companies: [plain-string]")
    with pytest.raises(ValueError, match="list of mappings"):
        load_yaml(path, "companies")


@pytest.mark.parametrize("weekday,expected", [(5, "weekly"), (7, "boards"), (9, "boards"), (10, "daily")])
def test_frequency_uses_warsaw_weekday(weekday, expected):
    assert mode_for_date(datetime(2026, 10, weekday, 5, tzinfo=timezone.utc)) == expected


def test_due_does_not_fetch_weekly_sources_on_daily_runs():
    assert not due("weekly", "daily") and not due("mwf", "daily")
    assert due("daily", "daily") and due("mwf", "boards") and due("weekly", "weekly")


def test_schedule_has_warsaw_clock_and_persistent_state():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/singapore-monitor.yml").read_text())
    event = workflow.get("on", workflow.get(True))
    assert event["schedule"] == [{"cron": "0 7 * * *", "timezone": "Europe/Warsaw"}]
    steps = workflow["jobs"]["monitor"]["steps"]
    assert any("cache/restore" in step.get("uses", "") for step in steps)
    assert any("cache/save" in step.get("uses", "") for step in steps)
    assert all("scoring_profile" not in step.get("with", {}).get("path", "") for step in steps if "upload-artifact" in step.get("uses", ""))


def test_public_api_robots_exception_requires_exact_researched_origin():
    policy = {"http_status": 401, "policy": "documented_public_api",
              "path_prefix": "/posting-api/job-board/",
              "documentation_url": "https://developers.ashbyhq.com/docs/public-job-posting-api",
              "reference_url": "https://www.rfc-editor.org/rfc/rfc9309.html#section-2.3.1.3",
              "verified_at": "2026-10-07"}
    row = {"name": "Acme", "ats": "ashby", "status": "verified", "automation_allowed": True,
           "robots_url": "https://api.ashbyhq.com/robots.txt", "robots_unavailable": policy}
    assert public_api_robots([row]) == {"https://api.ashbyhq.com": policy}
    with pytest.raises(ValueError, match="unsupported"):
        public_api_robots([{**row, "robots_url": "https://other.example.com/robots.txt"}])
    assert public_api_robots([{**row, "automation_allowed": False}]) == {}
