import json
import os
import re
from urllib.request import Request, urlopen
from .limits import (MAX_LIST_ITEMS, MAX_LIST_TEXT, MAX_OPENAI_BYTES, MAX_TEXT,
                     bounded_strings, read_bounded)


CAUSES = (("signal", "signal"), ("police", "police activity"), ("fire", "fire"),
          ("weather", "weather"), ("power", "power"), ("track", "track work"),
          ("maintenance", "maintenance"), ("medical", "medical emergency"))


class LocalExtractor:
    method = "local"
    model = "deterministic-v1"

    def extract(self, alert):
        text = (alert.get("headline", "") + " " + alert.get("description", "")).strip()
        low = text.lower()
        planned = any(x in low for x in ("planned", "scheduled", "maintenance", "construction"))
        cause = next((label for key, label in CAUSES if key in low), "unknown")
        if "elevator" in low or "accessible" in low or "ada" in low:
            accessibility = "Accessibility may be affected; verify elevator status and accessible alternatives."
        else:
            accessibility = "No accessibility impact stated."
        if "maintenance" in low or "construction" in low: event = "maintenance"
        elif "suspend" in low: event = "suspension"
        elif "bypass" in low or "skip" in low: event = "bypass"
        elif "shuttle" in low: event = "shuttle"
        elif "delay" in low: event = "delay"
        elif "elevator" in low: event = "accessibility"
        else: event = "service_change"
        effects = next((phrase for phrase in ("service suspended", "shuttle buses", "trains will bypass", "bypass", "delays", "delay") if phrase in low), "Service change reported")
        if "use " in low:
            action = re.split(r"(?<=[.!?])\s+", text[text.lower().find("use "):])[0]
        elif event in ("suspension", "bypass"):
            action = "Allow extra travel time and use posted alternate service."
        else:
            action = "Allow extra travel time and check CTA updates."
        summary = alert.get("headline") or alert.get("description") or "CTA service alert"
        if len(summary) > 180: summary = summary[:177].rstrip() + "..."
        confidence = 0.85 if event != "service_change" else 0.62
        return {"summary": summary, "planned": planned, "cause": cause,
                "effects": effects.capitalize(), "actions": action,
                "affected_lines": bounded_strings(alert.get("lines", [])),
                "affected_stations": bounded_strings(alert.get("stations", [])),
                "accessibility_impact": accessibility, "event_type": event,
                "confidence": confidence, "method": self.method, "model": self.model}


def _strings(value):
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def validate_extraction(value):
    required = {"summary": str, "planned": bool, "cause": str, "effects": str,
                "actions": str, "affected_lines": list, "affected_stations": list,
                "accessibility_impact": str, "event_type": str, "confidence": (int, float)}
    return (isinstance(value, dict) and all(isinstance(value.get(k), t) for k, t in required.items())
            and _strings(value["affected_lines"]) and _strings(value["affected_stations"])
            and all(len(value[k]) <= MAX_TEXT for k, t in required.items() if t is str)
            and len(value["affected_lines"]) <= MAX_LIST_ITEMS and len(value["affected_stations"]) <= MAX_LIST_ITEMS
            and all(len(x) <= MAX_LIST_TEXT for x in value["affected_lines"] + value["affected_stations"])
            and 0 <= float(value["confidence"]) <= 1)


class OpenAIExtractor:
    method = "openai"
    def __init__(self, fetcher=None, fallback=None, max_response_bytes=MAX_OPENAI_BYTES):
        self.fetcher = fetcher or urlopen
        self.fallback = fallback or LocalExtractor()
        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = os.getenv("OPENAI_MODEL", "gpt-5-mini")
        self.max_response_bytes = max_response_bytes

    def extract(self, alert):
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            return self.fallback.extract(alert)
        schema = {"type":"object","properties":{"summary":{"type":"string"},"planned":{"type":"boolean"},"cause":{"type":"string"},"effects":{"type":"string"},"actions":{"type":"string"},"affected_lines":{"type":"array","items":{"type":"string"}},"affected_stations":{"type":"array","items":{"type":"string"}},"accessibility_impact":{"type":"string"},"event_type":{"type":"string"},"confidence":{"type":"number","minimum":0,"maximum":1}},"required":["summary","planned","cause","effects","actions","affected_lines","affected_stations","accessibility_impact","event_type","confidence"],"additionalProperties":False}
        safe_alert = {str(k)[:64]: (v[:MAX_TEXT] if isinstance(v, str) else
                      bounded_strings(v) if isinstance(v, list) else v)
                      for k, v in alert.items() if isinstance(v, (str, list, bool, int, float))}
        payload = {"model": self.model[:256], "messages":[{"role":"system","content":"Extract CTA disruption facts. Return only supported facts."},{"role":"user","content":json.dumps(safe_alert)}], "response_format":{"type":"json_schema","json_schema":{"name":"cta_alert","strict":True,"schema":schema}}}
        req = Request(self.base_url + "/chat/completions", data=json.dumps(payload).encode(), headers={"Authorization":"Bearer " + key,"Content-Type":"application/json","User-Agent":"cta-rail-pipeline/0.1"})
        try:
            response = self.fetcher(req, timeout=20)
            raw = read_bounded(response, self.max_response_bytes, "OpenAI")
            outer = json.loads(raw)
            content = outer.get("choices", [{}])[0].get("message", {}).get("content")
            candidate = json.loads(content) if isinstance(content, str) else outer
            if not validate_extraction(candidate): raise ValueError("schema validation failed")
            candidate.update(method="openai", model=self.model)
            return candidate
        except Exception:
            result = self.fallback.extract(alert)
            result["method"] = "local-fallback"
            return result
