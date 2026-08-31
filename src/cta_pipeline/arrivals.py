import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError,URLError
from urllib.parse import urlencode
from urllib.request import Request,urlopen

CATALOG_PATH=Path(__file__).with_name("cta_rail_stations.json")
ARRIVALS_URL="https://lapi.transitchicago.com/api/1.0/ttarrivals.aspx"
LINES={"red","blue","brown","green","orange","pink","purple","yellow"}

class ArrivalLookupError(RuntimeError): pass

def _words(value): return set(re.findall(r"[a-z0-9]+",value.lower().replace("o'hare","ohare")))

def load_stations():
    value=json.loads(CATALOG_PATH.read_text(encoding="utf-8")); return value["stations"]

def wants_arrivals(question):
    words=_words(question)
    return bool(words & {"arrive","arrival","arrivals","next","eta","when"}) and bool(words & {"train","line","arrive","arrival","arrivals","eta"})

def resolve_station(question,stations=None):
    stations=stations or load_stations(); words=_words(question); requested_lines=words & LINES; candidates=[]
    for station in stations:
        base=station["name"].split(" (",1)[0]
        base_words=_words(base)
        # Riders commonly omit generic suffixes used by the GTFS display name.
        short_words=base_words-{"transit","center","station"}
        if base_words and (base_words<=words or (len(short_words)>=2 and short_words<=words)):
            station_lines=_words(station["name"]) & LINES
            # Some single-line stations, including O'Hare, have no line qualifier in
            # their GTFS name. An absent qualifier is not evidence against the match.
            if not requested_lines or not station_lines or requested_lines<=station_lines: candidates.append(station)
    unique={row["map_id"]:row for row in candidates}
    if len(unique)==1: return next(iter(unique.values()))
    if not unique: raise ArrivalLookupError("Which CTA station do you mean?")
    raise ArrivalLookupError("Which station do you mean: "+", ".join(sorted(row["name"] for row in unique.values()))+"?")

class ArrivalsClient:
    def __init__(self,fetcher=None): self.fetcher=fetcher or urlopen
    def fetch(self,station):
        key=os.getenv("CTA_API_KEY")
        if not key: raise ArrivalLookupError("CTA_API_KEY is not configured")
        url=ARRIVALS_URL+"?"+urlencode({"key":key,"mapid":station["map_id"],"outputType":"JSON","max":10})
        try:
            response=self.fetcher(Request(url,headers={"User-Agent":"cta-pipeline/0.2"}),timeout=15); raw=response.read(262145)
            if len(raw)>262144: raise ValueError("response too large")
            root=json.loads(raw)["ctatt"]
            if str(root.get("errCd"))!="0": raise ValueError(root.get("errNm") or "CTA error")
            predictions=[]
            for item in root.get("eta",[])[:10]:
                predictions.append({key:item.get(key) for key in ("rt","destNm","prdt","arrT","isApp","isSch","isDly")})
            return {"station_id":station["map_id"],"station_name":station["name"],"as_of":root.get("tmst"),"predictions":predictions}
        except (HTTPError,URLError,TimeoutError,OSError,ValueError,KeyError,TypeError,json.JSONDecodeError) as error:
            raise ArrivalLookupError("CTA arrivals are unavailable") from error
