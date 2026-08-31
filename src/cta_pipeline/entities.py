import json
import re
from pathlib import Path
from .retrievers import STATION_ROUTES_PATH
from .routes import display_station_name

GENERIC={"station","stop","transit","center","street","st","avenue","ave","the"}
FRESHNESS={"next","arrive","arrives","arriving","arrival","arrivals","eta","current","currently","now","soon","minutes","time","times"}

def words(value): return re.findall(r"[a-z0-9]+",value.casefold().replace("o'hare","ohare"))

class EntityResolver:
    def __init__(self,path=STATION_ROUTES_PATH):
        value=json.loads(Path(path).read_text(encoding="utf-8")); self.stations=value["stations"]
        routes={route["route_id"]:route for station in self.stations for route in station["routes"]}; self.routes=routes
    def analyze(self,question):
        tokens=words(question); token_set=set(tokens); routes=self._routes(question); route_ids={route["route_id"] for route in routes}; grouped={}
        for station in self.stations:
            base=station["name"].split(" (",1)[0]; base_tokens=set(words(base))-GENERIC; key=None
            if base_tokens and base_tokens<=token_set: key=frozenset(base_tokens)
            else:
                ordinals={token for token in base_tokens if re.fullmatch(r"[0-9]+(?:st|nd|rd|th)",token)}&token_set
                if ordinals: key=frozenset(ordinals)
            if key is None: continue
            grouped.setdefault(key,[]).append(station)
        keys=list(grouped)
        for key in keys:
            if any(key<other for other in keys): grouped.pop(key,None)
        # A route qualifier can disambiguate one station ("Monroe Blue Line"),
        # but must not discard another endpoint in a multi-station trip question.
        if len(grouped)==1 and route_ids:
            key=next(iter(grouped)); filtered=[station for station in grouped[key] if route_ids&{route["route_id"] for route in station["routes"]}]
            if filtered: grouped[key]=filtered
        mentions=[]
        for key,candidates in grouped.items():
            position=min(tokens.index(token) for token in key); mentions.append({"text":" ".join(token for token in tokens if token in key),"position":position,"candidates":sorted(candidates,key=lambda row:row["name"])})
        mentions.sort(key=lambda item:item["position"])
        return {"stations":mentions,"routes":routes,"freshness_requested":bool(token_set&FRESHNESS)}
    def _routes(self,question):
        normalized=" ".join(words(question)); found=[]
        for route in self.routes.values():
            name=route["name"].casefold(); base=name.removesuffix(" line")
            if re.search(r"\b"+re.escape(name)+r"\b",normalized) or re.search(r"\b"+re.escape(base)+r"\s+line\b",normalized): found.append(route)
        return list({route["route_id"]:route for route in found}.values())
    def clarification(self,index,mention):
        return {"type":"clarification","field":f"station_{index}","question":f'Which station did you mean by “{mention["text"]}”?',"options":[{"id":row["map_id"],"label":display_station_name(row["name"])} for row in mention["candidates"]]}
