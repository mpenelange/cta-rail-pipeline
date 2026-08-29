#!/usr/bin/env python3
"""Print bounded, sanitized structural metadata for one CTA Train Tracker response."""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from cta_pipeline.native_launcher import ConfigError, NoRedirectHandler, build_config
from cta_pipeline.telemetry import TRAIN_TRACKER_ROUTES, TRAIN_TRACKER_URL


REQUEST_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ROUTES_REPORTED = len(TRAIN_TRACKER_ROUTES)
MAX_KEY_NAMES = 32
MAX_MEMBER_TYPES = 5
MAX_TRAIN_MEMBERS_INSPECTED = 256

_TOP_KEYS = frozenset({"ctatt"})
_ROOT_KEYS = frozenset({"errCd", "errNm", "route", "tmst"})
_TRAIN_KEYS = frozenset({
    "rn", "destSt", "destNm", "trDr", "nextStaId", "nextStpId", "nextStaNm",
    "prdt", "arrT", "isApp", "isDly", "flags", "lat", "lon", "heading",
})
_CONTENT_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*\Z")
_DIGITS = re.compile(r"[0-9]{1,8}\Z")
_FAILURE_TYPES = frozenset({
    "configuration", "decode_error", "http_error", "invalid_response",
    "malformed_json", "network_error", "response_too_large",
})


class DiagnosticFailure(Exception):
    def __init__(self, error_type):
        self.error_type = error_type


def _kind(value):
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "other"


def _field_kind(container, key):
    return "missing" if not isinstance(container, dict) or key not in container else _kind(container[key])


def _key_names(value, allowlist):
    if not isinstance(value, dict):
        return []
    names = sorted(key for key in value if isinstance(key, str) and key in allowlist)
    if any(not isinstance(key, str) or key not in allowlist for key in value):
        names.append("<other>")
    return names[:MAX_KEY_NAMES]


def _safe_content_type(value):
    if not isinstance(value, str):
        return "unknown"
    normalized = value.split(";", 1)[0].strip().lower()
    return normalized if len(normalized) <= 127 and _CONTENT_TYPE.fullmatch(normalized) else "unknown"


def _numeric_error_code(value):
    if isinstance(value, bool):
        return None
    text = str(value) if isinstance(value, (int, str)) else ""
    return int(text) if _DIGITS.fullmatch(text) else None


def _train_metadata(route):
    if not isinstance(route, dict) or "train" not in route:
        return {"train_type": "missing", "train_count": None,
                "train_member_types": [], "train_key_names": []}
    trains = route["train"]
    train_type = _kind(trains)
    if isinstance(trains, list):
        members = trains[:MAX_TRAIN_MEMBERS_INSPECTED]
        member_types = sorted({_kind(member) for member in members})[:MAX_MEMBER_TYPES]
        keys = set()
        for member in members:
            if isinstance(member, dict):
                keys.update(_key_names(member, _TRAIN_KEYS))
        return {"train_type": train_type, "train_count": len(trains),
                "train_member_types": member_types,
                "train_key_names": sorted(keys)[:MAX_KEY_NAMES]}
    if isinstance(trains, dict):
        return {"train_type": train_type, "train_count": 1,
                "train_member_types": ["object"],
                "train_key_names": _key_names(trains, _TRAIN_KEYS)}
    return {"train_type": train_type, "train_count": None,
            "train_member_types": [], "train_key_names": []}


def summarize(document, http_status, content_type, byte_count):
    """Return only allowlisted, cardinality-oriented metadata from decoded JSON."""
    top = document if isinstance(document, dict) else None
    root = top.get("ctatt") if isinstance(top, dict) else None
    routes = root.get("route") if isinstance(root, dict) else None
    route_items = routes if isinstance(routes, list) else []
    reported = []
    for index, route in enumerate(route_items[:MAX_ROUTES_REPORTED]):
        route_name = route.get("@name") if isinstance(route, dict) else None
        item = {"index": index,
                "route_name": route_name if route_name in TRAIN_TRACKER_ROUTES else None}
        item.update(_train_metadata(route))
        reported.append(item)
    code = root.get("errCd") if isinstance(root, dict) else None
    return {
        "byte_count": byte_count,
        "content_type": _safe_content_type(content_type),
        "cta_error_code": _numeric_error_code(code),
        "cta_error_name_present": isinstance(root, dict) and "errNm" in root,
        "http_status": http_status if (isinstance(http_status, int) and
                                        not isinstance(http_status, bool) and
                                        100 <= http_status <= 599) else None,
        "root_key_names": _key_names(root, _ROOT_KEYS),
        "route_count": len(routes) if isinstance(routes, list) else None,
        "route_field_type": _field_kind(root, "route"),
        "routes": reported,
        "top_level_key_names": _key_names(top, _TOP_KEYS),
    }


def _default_fetcher(request, timeout):
    return urllib.request.build_opener(NoRedirectHandler()).open(request, timeout=timeout)


def _content_type(response):
    try:
        return response.headers.get("Content-Type", "")
    except (AttributeError, TypeError):
        return ""


def run_diagnostic(environment, fetcher=None):
    key = environment.get("CTA_API_KEY", "")
    if not isinstance(key, str) or not key.strip():
        raise DiagnosticFailure("configuration")
    url = TRAIN_TRACKER_URL + "?" + urlencode({
        "key": key, "rt": ",".join(TRAIN_TRACKER_ROUTES), "outputType": "JSON",
    })
    request = urllib.request.Request(url, headers={"User-Agent": "cta-traintracker-diagnostic/1"})
    fetch = fetcher or _default_fetcher
    try:
        response = fetch(request, REQUEST_TIMEOUT_SECONDS)
        with response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            status = getattr(response, "status", None)
            content_type = _content_type(response)
    except urllib.error.HTTPError:
        raise DiagnosticFailure("http_error") from None
    except (urllib.error.URLError, TimeoutError):
        raise DiagnosticFailure("network_error") from None
    if not isinstance(raw, bytes):
        raise DiagnosticFailure("invalid_response")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DiagnosticFailure("response_too_large")
    try:
        document = json.loads(raw.decode("utf-8"))
    except UnicodeError:
        raise DiagnosticFailure("decode_error") from None
    except (ValueError, TypeError, RecursionError):
        raise DiagnosticFailure("malformed_json") from None
    return summarize(document, status, content_type, len(raw))


def _failure_type(exc):
    if isinstance(exc, DiagnosticFailure):
        return exc.error_type if exc.error_type in _FAILURE_TYPES else "unexpected"
    if isinstance(exc, (ConfigError, ValueError)):
        return "configuration"
    return "unexpected"


def main(*, config=None, fetcher=None, repo_root=None, environ=None):
    try:
        if config is None:
            repo = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]
            config = build_config([], repo, os.environ if environ is None else environ)
        result = run_diagnostic(config.environment, fetcher=fetcher)
    except Exception as exc:
        result = {"status": "diagnostic_failed", "error_type": _failure_type(exc)}
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
