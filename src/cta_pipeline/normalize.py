import html
import json
import re
from .limits import MAX_ID, MAX_LIST_ITEMS, MAX_LIST_TEXT, MAX_TEXT


class PayloadError(ValueError):
    pass


def _list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def clean_text(value, limit=MAX_TEXT):
    if isinstance(value, dict):
        value = value.get("#cdata-section", value.get("text", ""))
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if text.startswith("<![CDATA[") and text.endswith("]]>"):
        text = text[9:-3]
    text = html.unescape(text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:limit]


def _service_values(service):
    lines, stations, station_ids = [], [], []
    for item in _list(service):
        if not isinstance(item, dict):
            continue
        nested = item.get("Service")
        if nested:
            sublines, substations, subids = _service_values(nested)
            lines += sublines; stations += substations; station_ids += subids
        route = item.get("Route") or item.get("ServiceId") or item.get("RouteId")
        name = item.get("StopName") or item.get("ServiceName") or item.get("StationName")
        sid = item.get("StopId") or item.get("StationId")
        if route: lines.append(clean_text(route, MAX_LIST_TEXT))
        if name and not route: stations.append(clean_text(name, MAX_LIST_TEXT))
        elif item.get("StopName"): stations.append(clean_text(item["StopName"], MAX_LIST_TEXT))
        if sid: station_ids.append(str(sid)[:MAX_LIST_TEXT])
    return (sorted(set(filter(None, lines)))[:MAX_LIST_ITEMS],
            sorted(set(filter(None, stations)))[:MAX_LIST_ITEMS],
            sorted(set(filter(None, station_ids)))[:MAX_LIST_ITEMS])


def normalize_payload(document):
    if not isinstance(document, dict):
        raise PayloadError("CTA response root must be an object")
    if "CTARailAlerts" in document:
        root = document["CTARailAlerts"]
    elif "CTAAlerts" in document:
        root = document["CTAAlerts"]
    else:
        raise PayloadError("CTA response missing CTAAlerts container")
    if not isinstance(root, dict):
        raise PayloadError("CTAAlerts container must be an object")
    error_code = root.get("ErrorCode", document.get("ErrorCode", "0"))
    if str(error_code or "0").strip() not in ("", "0", "0.0"):
        raise PayloadError(f"CTA API ErrorCode {str(error_code)[:32]}")
    if "Alert" not in root:
        raise PayloadError("CTAAlerts container missing Alert")
    rows = root["Alert"]
    # CTA documents an empty feed as an empty JSON array. Objects must be alerts.
    if not isinstance(rows, (list, dict)):
        raise PayloadError("CTA Alert must be an object or array")
    if isinstance(rows, dict) and not any(k in rows for k in ("AlertId", "AlertID", "Guid")):
        raise PayloadError("CTA Alert object has unexpected shape")
    result = []
    source_ids = set()
    for row in _list(rows):
        if not isinstance(row, dict):
            raise PayloadError("CTA Alert array members must be objects")
        source_id = next((row.get(key) for key in ("AlertId", "AlertID", "Guid")
                          if row.get(key) is not None), None)
        if isinstance(source_id, bool) or not isinstance(source_id, (str, int, float)):
            raise PayloadError("CTA Alert requires a usable source ID")
        source_id = str(source_id).strip()[:MAX_ID]
        if not source_id:
            raise PayloadError("CTA Alert requires a usable source ID")
        if source_id in source_ids:
            raise PayloadError(f"duplicate CTA alert source ID {source_id!r}")
        source_ids.add(source_id)
        lines, stations, station_ids = _service_values(row.get("Service"))
        result.append({
            "source_id": source_id,
            "headline": clean_text(row.get("Headline")),
            "description": clean_text(row.get("ShortDescription") or row.get("FullDescription")),
            "severity": clean_text(row.get("SeverityScore") or row.get("Severity") or "Unknown"),
            "start_time": clean_text(row.get("EventStart") or row.get("StartTime")),
            "end_time": clean_text(row.get("EventEnd") or row.get("EndTime")),
            "lines": lines,
            "stations": stations,
            "station_ids": station_ids,
            "major": str(row.get("MajorAlert", "")).lower() in ("true", "1", "yes"),
            "alert_url": clean_text(row.get("AlertURL") or row.get("URL")),
        })
    return result


def canonical_bytes(alert):
    return json.dumps(alert, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def bound_normalized_alert(alert):
    if not isinstance(alert, dict) or not alert.get("source_id"):
        raise ValueError("normalized alert requires source_id")
    result = {}
    for key, value in alert.items():
        if isinstance(value, str):
            result[key] = value[:MAX_ID if key == "source_id" else MAX_TEXT]
        elif isinstance(value, list):
            if not all(isinstance(item, str) for item in value):
                raise ValueError(f"normalized alert {key} must contain strings")
            result[key] = [item[:MAX_LIST_TEXT] for item in value[:MAX_LIST_ITEMS]]
        elif isinstance(value, (bool, int, float)) or value is None:
            result[key] = value
        else:
            raise ValueError(f"normalized alert {key} has unsupported type")
    return result
