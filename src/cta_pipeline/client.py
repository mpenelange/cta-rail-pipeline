import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from .limits import MAX_CTA_BYTES, read_bounded

CTA_URL = "https://lapi.transitchicago.com/api/1.0/alerts.aspx?outputType=JSON&routeid=Red,Blue,Brn,G,Org,P,Pexp,Pink,Y"


class SourceError(RuntimeError):
    pass


class CTAAlertsClient:
    def __init__(self, fetcher=None, timeout=15.0, url=CTA_URL, max_response_bytes=MAX_CTA_BYTES):
        self.fetcher = fetcher or urlopen
        self.timeout = timeout
        self.url = url
        self.max_response_bytes = max_response_bytes

    def fetch(self):
        request = Request(self.url, headers={"User-Agent": "cta-rail-pipeline/0.1 (+local-mvp)"})
        try:
            response = self.fetcher(request, timeout=self.timeout)
            body = read_bounded(response, self.max_response_bytes, "CTA alerts")
            return body, json.loads(body.decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise SourceError(f"CTA alerts fetch failed: {exc}") from exc
