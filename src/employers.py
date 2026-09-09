"""Versioned employer evidence registry. Offline, read-only, independent of Config/DB.

Run ``python -m src.employers validate`` or ``python -m src.employers summary``.
The registry is research evidence, never a source activation or vacancy inventory.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import urlsplit, urlunsplit

import yaml

DEFAULT_REGISTRY = Path(__file__).resolve().parents[1] / "coverage" / "employers.yaml"
SUPPORTED_ADAPTERS = frozenset({
    "greenhouse", "lever", "workable", "ashby", "smartrecruiters", "personio",
    "recruitee", "teamtailor", "allegro",
})
HEALTH = ("not_checked", "unknown", "reachable", "unavailable")
SCOPES = ("careers_index", "job_detail", "collector_api")


class RegistryError(ValueError):
    """Invalid registry content or unreadable input."""


class _Loader(yaml.SafeLoader):
    # Dates stay strings; do not mutate PyYAML's global resolvers.
    yaml_implicit_resolvers = {
        key: [(tag, pattern) for tag, pattern in rules
              if tag != "tag:yaml.org,2002:timestamp"]
        for key, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise RegistryError("YAML aliases are not supported; use explicit evidence")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise RegistryError("mapping keys must be strings")
            if key in result:
                raise RegistryError(f"duplicate YAML key: {key}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _text(value, path):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RegistryError(f"{path}: expected nonblank, trimmed string")
    return value


def _object(value, keys, path):
    if not isinstance(value, dict):
        raise RegistryError(f"{path}: expected mapping")
    expected = set(keys.split())
    if set(value) != expected:
        raise RegistryError(f"{path}: missing fields {sorted(expected - set(value))}; "
                            f"unexpected fields {sorted(set(value) - expected, key=str)}")


def _list(value, path, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise RegistryError(f"{path}: expected {'nonempty ' if nonempty else ''}list")
    return value


def _enum(value, choices, path):
    if not isinstance(value, str) or value not in choices:
        raise RegistryError(f"{path}: expected one of {', '.join(sorted(choices))}")


def _date(value, path):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise RegistryError(f"{path}: expected YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RegistryError(f"{path}: invalid calendar date {value}") from exc


def _url(value, path):
    _text(value, path)
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        port = parts.port
        if (parts.scheme not in {"http", "https"} or not _domain(host)
                or parts.username is not None or parts.password is not None
                or re.search(r"[\s\\\x00-\x1f\x7f]", value)
                or re.search(r"%(?![0-9A-Fa-f]{2})", value)):
            raise ValueError("invalid HTTP(S) URL")
    except ValueError as exc:
        raise RegistryError(f"{path}: invalid HTTP(S) URL") from exc
    netloc = host.lower()
    if port and (parts.scheme, port) not in {("http", 80), ("https", 443)}:
        netloc += f":{port}"
    # Preserve path case and query semantics; strip fragments/trailing slash only.
    return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), parts.query, ""))


def _domain(value):
    return isinstance(value, str) and len(value) <= 253 and bool(re.fullmatch(
        r"(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
        r"[a-zA-Z]{2,63}", value))


def validate_registry(data):
    """Validate v1 strictly, returning the input unchanged or raising RegistryError."""
    _object(data, "schemaVersion updatedOn employers", "registry")
    if type(data["schemaVersion"]) is not int or data["schemaVersion"] != 1:
        raise RegistryError("schemaVersion: only integer version 1 is supported")
    updated = _date(data["updatedOn"], "updatedOn")
    employers = _list(data["employers"], "employers", nonempty=True)
    ids, urls = set(), {}
    for i, employer in enumerate(employers):
        p = f"employers[{i}]"
        _object(employer, "employerId name aliases officialDomain careersUrl locations "
                "evidence surfaces sourceCompatibility endpoints status liveHiringStatus nextAction", p)
        ident = _text(employer["employerId"], f"{p}.employerId")
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", ident):
            raise RegistryError(f"{p}.employerId: expected stable lowercase hyphenated ID")
        if ident in ids:
            raise RegistryError(f"{p}: duplicate employerId {ident}")
        ids.add(ident)
        _text(employer["name"], f"{p}.name")
        aliases = _list(employer["aliases"], f"{p}.aliases")
        names = {employer["name"].casefold()}
        for alias in aliases:
            folded = _text(alias, f"{p}.aliases").casefold()
            if folded in names:
                raise RegistryError(f"{p}.aliases: duplicate name/alias")
            names.add(folded)
        if not _domain(employer["officialDomain"]):
            raise RegistryError(f"{p}.officialDomain: expected domain without scheme/path")
        canonical = _url(employer["careersUrl"], f"{p}.careersUrl")
        if canonical in urls:
            raise RegistryError(f"{p}.careersUrl: duplicate URL owned by {urls[canonical]}")
        urls[canonical] = ident
        _enum(employer["status"], {"lead", "researched", "needs_review"}, f"{p}.status")
        _enum(employer["liveHiringStatus"], {"unknown", "not_checked"}, f"{p}.liveHiringStatus")
        _text(employer["nextAction"], f"{p}.nextAction")
        evidence = {}
        for j, ev in enumerate(_list(employer["evidence"], f"{p}.evidence", nonempty=True)):
            ep = f"{p}.evidence[{j}]"
            _object(ev, "id url observedOn method provenance note", ep)
            key = _text(ev["id"], ep + ".id")
            if key in evidence:
                raise RegistryError(f"{ep}: duplicate evidence ID {key}")
            evidence[key] = ev
            _url(ev["url"], ep + ".url")
            if _date(ev["observedOn"], ep + ".observedOn") > updated:
                raise RegistryError(f"{ep}: evidence date exceeds updatedOn")
            _enum(ev["method"], {"primary_page", "search_index", "prior_report", "direct_http"}, ep + ".method")
            _text(ev["provenance"], ep + ".provenance")
            _text(ev["note"], ep + ".note")

        def refs(values, path, required=False):
            seen = set()
            for ref in _list(values, path, nonempty=required):
                _text(ref, path)
                if ref not in evidence or ref in seen:
                    raise RegistryError(f"{path}: unknown or duplicate evidence reference {ref}")
                seen.add(ref)

        for j, loc in enumerate(_list(employer["locations"], p + ".locations", nonempty=True)):
            lp = f"{p}.locations[{j}]"
            _object(loc, "country city kind evidence", lp)
            if not isinstance(loc["country"], str) or not re.fullmatch(r"[A-Z]{2}", loc["country"]):
                raise RegistryError(f"{lp}.country: expected two uppercase letters")
            if loc["city"] is not None:
                _text(loc["city"], lp + ".city")
            _enum(loc["kind"], {"office", "job_location", "historical_job", "remote_country", "country_presence"}, lp + ".kind")
            refs(loc["evidence"], lp + ".evidence", True)
        _object(employer["surfaces"], "frontend apply ats", p + ".surfaces")
        for layer, surface in employer["surfaces"].items():
            sp = f"{p}.surfaces.{layer}"
            _object(surface, "status label url evidence", sp)
            _enum(surface["status"], {"observed", "unknown", "not_checked"}, sp + ".status")
            observed = surface["status"] == "observed"
            if observed:
                _text(surface["label"], sp + ".label")
                _url(surface["url"], sp + ".url")
            elif surface["label"] is not None or surface["url"] is not None:
                raise RegistryError(f"{sp}: unobserved surface must have null label and URL")
            refs(surface["evidence"], sp + ".evidence", observed)
        compat = employer["sourceCompatibility"]
        _object(compat, "status adapter evidence", p + ".sourceCompatibility")
        _enum(compat["status"], {"supported", "unsupported", "unknown"}, p + ".sourceCompatibility.status")
        if compat["status"] == "supported":
            _enum(compat["adapter"], SUPPORTED_ADAPTERS, p + ".sourceCompatibility.adapter")
        elif compat["adapter"] is not None:
            raise RegistryError(f"{p}.sourceCompatibility: adapter must be null unless supported")
        refs(compat["evidence"], p + ".sourceCompatibility.evidence", compat["status"] != "unknown")
        endpoint_urls = set()
        for j, endpoint in enumerate(_list(employer["endpoints"], p + ".endpoints")):
            hp = f"{p}.endpoints[{j}]"
            _object(endpoint, "url scope health checkedOn evidence", hp)
            url = _url(endpoint["url"], hp + ".url")
            if url in endpoint_urls:
                raise RegistryError(f"{hp}: duplicate endpoint URL")
            if url in urls and urls[url] != ident:
                raise RegistryError(f"{hp}: duplicate URL owned by {urls[url]}")
            urls[url] = ident
            endpoint_urls.add(url)
            _enum(endpoint["scope"], SCOPES, hp + ".scope")
            _enum(endpoint["health"], HEALTH, hp + ".health")
            checked = endpoint["health"] != "not_checked"
            refs(endpoint["evidence"], hp + ".evidence", checked)
            if checked:
                checked_date = _date(endpoint["checkedOn"], hp + ".checkedOn")
                if checked_date > updated:
                    raise RegistryError(f"{hp}: check date exceeds updatedOn")
                if not any(evidence[r]["observedOn"] == endpoint["checkedOn"]
                           for r in endpoint["evidence"]):
                    raise RegistryError(f"{hp}: check date needs matching dated evidence")
                if endpoint["health"] in {"reachable", "unavailable"} and not any(
                    evidence[r]["method"] in {"direct_http", "prior_report"}
                    and _url(evidence[r]["url"], hp) == url
                    and evidence[r]["observedOn"] == endpoint["checkedOn"]
                    for r in endpoint["evidence"]
                ):
                    raise RegistryError(f"{hp}: verified health needs exact-URL HTTP/report evidence")
            elif endpoint["checkedOn"] is not None:
                raise RegistryError(f"{hp}: not_checked requires null checkedOn")
    return data


def load_registry(path=DEFAULT_REGISTRY):
    try:
        data = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_Loader)
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise RegistryError(f"cannot read registry {path}: {exc}") from exc
    return validate_registry(data)


def _city(value):
    return "".join(c for c in unicodedata.normalize("NFKD", value or "")
                   if not unicodedata.combining(c)).casefold()


def summarize(data):
    """Summarize recorded observations, without making any live requests."""
    validate_registry(data)
    employers = data["employers"]
    count = lambda key, choices: {choice: sum(e[key] == choice for e in employers) for choice in choices}
    endpoint_counts = {scope: {health: 0 for health in HEALTH} for scope in SCOPES}
    for employer in employers:
        for endpoint in employer["endpoints"]:
            endpoint_counts[endpoint["scope"]][endpoint["health"]] += 1
    krakow = [e for e in employers if any(l["country"] == "PL" and _city(l["city"]) == "krakow" for l in e["locations"])]
    return {
        "schemaVersion": data["schemaVersion"], "asOf": data["updatedOn"],
        "employers": len(employers),
        "geography": {
            "polandEvidenced": sum(any(l["country"] == "PL" for l in e["locations"]) for e in employers),
            "krakowEvidenced": len(krakow),
            "krakowEmployerIds": [e["employerId"] for e in krakow],
        },
        "sourceCompatibility": {status: sum(e["sourceCompatibility"]["status"] == status for e in employers)
                                for status in ("supported", "unsupported", "unknown")},
        "status": count("status", ("lead", "researched", "needs_review")),
        "liveHiringStatus": count("liveHiringStatus", ("unknown", "not_checked")),
        "endpointObservations": endpoint_counts,
        "employersWithoutVerifiedCollector": sum(not any(
            p["scope"] == "collector_api" and p["health"] == "reachable" for p in e["endpoints"]
        ) for e in employers),
        "evidenceMethods": dict(sorted(Counter(ev["method"] for e in employers for ev in e["evidence"]).items())),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "summary"))
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--json", action="store_true", help="machine-readable output, including validation errors")
    args = parser.parse_args(argv)
    try:
        data = load_registry(args.registry)
        result = summarize(data) if args.command == "summary" else {
            "valid": True, "schemaVersion": data["schemaVersion"], "employers": len(data["employers"]),
        }
    except RegistryError as exc:
        if args.json:
            print(json.dumps({"valid": False, "error": str(exc)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "validate":
        print(f"Valid registry v{result['schemaVersion']}: {result['employers']} employers")
    else:
        print(f"Employer evidence as of {result['asOf']} (offline; not a live vacancy count)")
        print(f"Employers: {result['employers']}; Poland evidenced: {result['geography']['polandEvidenced']}; "
              f"Kraków evidenced, including historical/indexed: {result['geography']['krakowEvidenced']}")
        print("Source compatibility (does not imply activation): " + json.dumps(result["sourceCompatibility"]))
        print("Research status: " + json.dumps(result["status"]))
        print("Live hiring status: " + json.dumps(result["liveHiringStatus"]))
        for scope, counts in result["endpointObservations"].items():
            print(f"Endpoint observations / {scope}: {json.dumps(counts)}")
        print(f"Employers without a verified reachable collector: {result['employersWithoutVerifiedCollector']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
