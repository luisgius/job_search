"""Offline registry contract and CLI regressions; no live employer checks."""
from copy import deepcopy
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from src.employers import (
    DEFAULT_REGISTRY, RegistryError, load_registry, main, summarize, validate_registry,
)


@pytest.fixture
def registry():
    return deepcopy(load_registry())


def test_shipped_cohort_and_honest_summary(registry):
    result = summarize(registry)
    assert result["employers"] == 20
    assert result["geography"]["polandEvidenced"] == 20
    assert result["geography"]["krakowEvidenced"] == 14
    assert len(set(result["geography"]["krakowEmployerIds"])) == 14
    assert result["liveHiringStatus"] == {"unknown": 20, "not_checked": 0}
    assert result["sourceCompatibility"] == {"supported": 3, "unsupported": 4, "unknown": 13}
    assert result["employersWithoutVerifiedCollector"] == 20
    assert result["endpointObservations"]["job_detail"]["unavailable"] == 4
    assert result["endpointObservations"]["job_detail"]["reachable"] == 2
    assert sum(result["endpointObservations"]["collector_api"].values()) == 0


def test_layers_and_closed_jobs_do_not_claim_employer_closure(registry):
    by_id = {e["employerId"]: e for e in registry["employers"]}
    az = by_id["astrazeneca"]["surfaces"]
    assert az["frontend"]["status"] == az["apply"]["status"] == "observed"
    assert az["ats"]["status"] == "unknown"
    for ident in ("here-technologies", "hitachi-energy", "ing-hubs-poland"):
        assert by_id[ident]["liveHiringStatus"] == "unknown"
    assert by_id["tesco-technology"]["sourceCompatibility"]["status"] == "supported"
    assert by_id["tesco-technology"]["endpoints"][0]["health"] == "unknown"
    assert by_id["allegro"]["sourceCompatibility"]["adapter"] == "allegro"
    assert by_id["allegro"]["endpoints"][0]["health"] == "unknown"


def test_registry_adapter_subset_matches_implemented_collectors():
    from src.config import BOARD_SOURCE_NAMES, SOURCE_NAMES
    from src.employers import SUPPORTED_ADAPTERS
    assert SUPPORTED_ADAPTERS == set(BOARD_SOURCE_NAMES) | {"allegro"}
    assert SUPPORTED_ADAPTERS <= set(SOURCE_NAMES)


@pytest.mark.parametrize("path,value,message", [
    (("schemaVersion",), 2, "schemaVersion"),
    (("schemaVersion",), True, "schemaVersion"),
    (("updatedOn",), "2026-02-30", "calendar date"),
    (("updatedOn",), "20260909", "YYYY-MM-DD"),
    (("employers",), {}, "list"),
    (("employers",), [], "nonempty"),
    (("employers", 0, "employerId"), "Allegro", "stable"),
    (("employers", 0, "name"), " ", "nonblank"),
    (("employers", 0, "aliases"), ["Allegro"], "duplicate"),
    (("employers", 0, "officialDomain"), "https://allegro.eu", "domain"),
    (("employers", 0, "status"), "closed", "expected one"),
    (("employers", 0, "liveHiringStatus"), "closed", "expected one"),
    (("employers", 0, "nextAction"), None, "string"),
    (("employers", 0, "locations", 0, "country"), "Poland", "two uppercase"),
    (("employers", 0, "locations", 0, "kind"), [], "expected one"),
    (("employers", 0, "locations", 0, "evidence"), [], "nonempty"),
    (("employers", 0, "evidence", 0, "observedOn"), "2026-09-10", "exceeds"),
    (("employers", 0, "evidence", 0, "method"), "guess", "expected one"),
    (("employers", 0, "surfaces", "ats", "status"), "workday", "expected one"),
    (("employers", 0, "surfaces", "ats", "label"), "Workday", "unobserved"),
    (("employers", 0, "surfaces", "frontend", "evidence"), ["missing"], "reference"),
    (("employers", 0, "surfaces", "frontend", "evidence"), ["career", "career"], "duplicate"),
    (("employers", 0, "sourceCompatibility", "status"), "enabled", "expected one"),
    (("employers", 0, "sourceCompatibility", "adapter"), "workday", "expected one"),
    (("employers", 5, "sourceCompatibility", "adapter"), "workday", "must be null"),
    (("employers", 3, "sourceCompatibility", "adapter"), "workday", "expected one"),
    (("employers", 0, "endpoints", 0, "scope"), "employer", "expected one"),
    (("employers", 0, "endpoints", 0, "health"), "open", "expected one"),
    (("employers", 0, "endpoints", 0, "checkedOn"), "yesterday", "YYYY-MM-DD"),
    (("employers", 0, "endpoints", 0, "checkedOn"), "2026-09-08", "matching dated"),
    (("employers", 0, "endpoints", 0, "checkedOn"), "2026-09-10", "exceeds"),
    (("employers", 0, "endpoints", 0, "health"), "not_checked", "null checkedOn"),
])
def test_schema_rejects_invalid_fields(registry, path, value, message):
    node = registry
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(RegistryError, match=message):
        validate_registry(registry)


@pytest.mark.parametrize("url", [
    "ftp://example.com/jobs", "https://", "https://example.com:bad/jobs",
    "https://user:password@example.com/jobs", "https://bad host.com/jobs",
    "https://example.com/has space", "https://example.com\\evil/jobs",
    "https://example.com/%ZZ", "https://[broken/jobs", "https://-bad.example.com/jobs",
])
def test_invalid_urls(registry, url):
    registry["employers"][0]["careersUrl"] = url
    with pytest.raises(RegistryError, match="URL"):
        validate_registry(registry)


@pytest.mark.parametrize("kind", ["id", "career_url", "endpoint", "cross_employer_endpoint", "evidence_id"])
def test_duplicates(registry, kind):
    a, b = registry["employers"][:2]
    if kind == "id":
        b["employerId"] = a["employerId"]
    elif kind == "career_url":
        b["careersUrl"] = a["careersUrl"].replace("careers.allegro.eu", "CAREERS.ALLEGRO.EU:443") + "#other"
    elif kind == "endpoint":
        a["endpoints"].append(deepcopy(a["endpoints"][0]))
    elif kind == "cross_employer_endpoint":
        b["endpoints"].append(deepcopy(a["endpoints"][0]))
    else:
        a["evidence"].append(deepcopy(a["evidence"][0]))
    with pytest.raises(RegistryError, match="duplicate"):
        validate_registry(registry)


def test_schema_rejects_missing_and_unknown_keys(registry):
    registry["employers"][0]["watchlistSlug"] = "guess"
    with pytest.raises(RegistryError, match="unexpected fields.*watchlistSlug"):
        validate_registry(registry)
    del registry["employers"][0]["watchlistSlug"]
    del registry["employers"][0]["nextAction"]
    with pytest.raises(RegistryError, match="missing fields.*nextAction"):
        validate_registry(registry)


def test_search_or_rendered_content_cannot_verify_endpoint_health(registry):
    employer = registry["employers"][1]
    employer["evidence"][0]["method"] = "primary_page"
    with pytest.raises(RegistryError, match="exact-URL HTTP/report"):
        validate_registry(registry)
    employer["evidence"][0]["method"] = "direct_http"
    employer["evidence"][0]["url"] = "https://example.com/other"
    with pytest.raises(RegistryError, match="exact-URL HTTP/report"):
        validate_registry(registry)


def test_collector_health_counts_only_explicit_verified_collector(registry):
    employer = registry["employers"][1]
    employer["endpoints"][0]["scope"] = "collector_api"
    result = summarize(registry)
    assert result["employersWithoutVerifiedCollector"] == 19
    assert result["endpointObservations"]["collector_api"]["reachable"] == 1
    assert result["endpointObservations"]["job_detail"]["reachable"] == 1


@pytest.mark.parametrize("content", [
    "schemaVersion: 1\nschemaVersion: 1", "[not, a, registry]", "schemaVersion: [",
    "1: value", "x: &cycle\n  self: *cycle", "x: !!python/object:foo {}",
])
def test_bad_yaml_is_a_clean_registry_error(tmp_path, content):
    path = tmp_path / "bad.yaml"
    path.write_text(content)
    with pytest.raises(RegistryError):
        load_registry(path)


def test_safe_loader_global_date_behavior_unchanged():
    import yaml
    from datetime import date
    assert yaml.safe_load("day: 2026-09-09")["day"] == date(2026, 9, 9)


def test_cli_is_offline_read_only_and_json_is_parseable(monkeypatch, capsys):
    before = DEFAULT_REGISTRY.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError("offline registry attempted network access")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    assert main(["validate", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert main(["summary", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["employersWithoutVerifiedCollector"] == 20
    assert DEFAULT_REGISTRY.read_bytes() == before
    assert main(["summary"]) == 0
    text = capsys.readouterr().out
    assert "not a live vacancy count" in text
    assert "does not imply activation" in text
    assert "including historical/indexed" in text


def test_cli_errors_and_exit_codes(tmp_path, capsys):
    missing = str(tmp_path / "missing.yaml")
    assert main(["validate", "--registry", missing]) == 1
    assert "error:" in capsys.readouterr().err
    assert main(["summary", "--registry", missing, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False
    with pytest.raises(SystemExit) as exc:
        main(["activate"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["summary", "--write"])
    assert exc.value.code == 2


def test_cli_default_path_independent_of_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["validate"]) == 0
    assert "20 employers" in capsys.readouterr().out


def test_module_entrypoint():
    result = subprocess.run([sys.executable, "-m", "src.employers", "validate", "--json"],
                            cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["employers"] == 20
