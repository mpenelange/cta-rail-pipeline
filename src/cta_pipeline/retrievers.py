import json
from pathlib import Path
from .arrivals import ArrivalsClient
from .routes import display_station_name

STATION_ROUTES_PATH=Path(__file__).with_name("cta_station_routes.json")

class RetrieverRegistry:
    """Execute only named, registered evidence capabilities."""
    def __init__(self,pipeline,arrivals=None,station_routes_path=STATION_ROUTES_PATH):
        self.pipeline=pipeline; self.arrivals=arrivals or ArrivalsClient()
        value=json.loads(Path(station_routes_path).read_text(encoding="utf-8")); self.route_source=value["source"]
        self.station_routes={row["map_id"]:row for row in value["stations"]}
        self.transfers=value.get("transfers",[])
        self.capabilities={"service_alerts":self._service_alerts,"station_routes":self._station_routes,"route_stations":self._route_stations,"live_arrivals":self._live_arrivals,"trip_routes":self._trip_routes}
    def retrieve(self,needs,question,entities=None):
        entities=entities or {}
        evidence={}; sources=[]
        for need in needs:
            if need not in self.capabilities: raise ValueError("unknown retrieval capability")
            value,labels=self.capabilities[need](question,entities); evidence[need]=value; sources.extend(labels)
        return evidence,list(dict.fromkeys(sources))
    def neighborhood(self,question,stations,routes,freshness_requested=False):
        evidence={}; sources=[]
        alerts,labels=self._service_alerts(question,{}); evidence["service_alerts"]=alerts; sources.extend(labels)
        station_values=[]
        for station in stations:
            value,labels=self._station_routes(question,{"station":station}); station_values.append(value); sources.extend(labels)
        if station_values: evidence["station_routes"]=station_values
        route_values=[]
        for route in routes:
            value,labels=self._route_stations(question,{"route":route}); route_values.append(value); sources.extend(labels)
        if route_values: evidence["route_stations"]=route_values
        if len(stations)>=2:
            value,labels=self._trip_routes(question,{"origin":stations[0],"destination":stations[1]}); evidence["trip_routes"]=value; sources.extend(labels)
        if freshness_requested and stations:
            value,labels=self._live_arrivals(question,{"station":stations[0]}); evidence["live_arrivals"]=value; sources.extend(labels)
        return evidence,list(dict.fromkeys(sources))
    def _service_alerts(self,question,_entities):
        snapshot=self.pipeline.snapshot(); documents=self.pipeline.retrieve(question)
        return {"source":snapshot["source"],"as_of":snapshot["as_of"],"documents":documents},[d["source_id"] for d in documents]
    def _station_routes(self,_question,entities):
        station=entities.get("station")
        if not station: raise ValueError("station_routes requires a station")
        row=self.station_routes.get(station["map_id"])
        if not row: raise ValueError("station route data unavailable")
        value={"source":self.route_source,"station_id":row["map_id"],"station_name":display_station_name(row["name"]),"routes":row["routes"]}
        return value,["GTFS routes · "+value["station_name"]]
    def _live_arrivals(self,_question,entities):
        station=entities.get("station")
        if not station: raise ValueError("live_arrivals requires a station")
        value=self.arrivals.fetch(station); return value,["Live arrivals · "+display_station_name(value["station_name"])]
    def _route_stations(self,_question,entities):
        route=entities.get("route")
        if not route: raise ValueError("route_stations requires a route")
        stations=[]
        for row in self.station_routes.values():
            if not any(item["route_id"]==route["route_id"] for item in row["routes"]): continue
            transfer_links=[]
            for edge in self.transfers:
                if row["map_id"]==edge["from_station_id"]: linked_id=edge["to_station_id"]
                elif row["map_id"]==edge["to_station_id"]: linked_id=edge["from_station_id"]
                else: continue
                linked=self.station_routes[linked_id]
                transfer_links.append({"station_id":linked_id,"station_name":display_station_name(linked["name"]),"routes":linked["routes"]})
            stations.append({"station_id":row["map_id"],"station_name":display_station_name(row["name"]),"routes":row["routes"],"transfer_links":transfer_links})
        stations.sort(key=lambda row:row["station_name"])
        value={"source":self.route_source,"route":route,"ordering":"alphabetical","station_count":len(stations),"stations":stations}
        return value,["GTFS stations · "+route["name"]]
    def _trip_routes(self,_question,entities):
        origin=entities.get("origin"); destination=entities.get("destination")
        if not origin or not destination: raise ValueError("trip_routes requires origin and destination")
        origin_row=self.station_routes.get(origin["map_id"]); destination_row=self.station_routes.get(destination["map_id"])
        if not origin_row or not destination_row: raise ValueError("trip route data unavailable")
        origin_routes={row["route_id"]:row for row in origin_row["routes"]}; destination_routes={row["route_id"]:row for row in destination_row["routes"]}
        direct=[origin_routes[key] for key in sorted(origin_routes.keys()&destination_routes.keys())]
        transfers=[]
        if not direct:
            for row in self.station_routes.values():
                ids={route["route_id"] for route in row["routes"]}
                for first in origin_routes.keys()&ids:
                    for second in destination_routes.keys()&ids:
                        if first!=second: transfers.append({"station_id":row["map_id"],"station_name":display_station_name(row["name"]),"from_route":origin_routes[first],"to_route":destination_routes[second]})
            for edge in self.transfers:
                for first_id,second_id in ((edge["from_station_id"],edge["to_station_id"]),(edge["to_station_id"],edge["from_station_id"])):
                    first_station=self.station_routes[first_id]; second_station=self.station_routes[second_id]
                    first_ids={route["route_id"] for route in first_station["routes"]}; second_ids={route["route_id"] for route in second_station["routes"]}
                    for first in sorted(origin_routes.keys()&first_ids):
                        for second in sorted(destination_routes.keys()&second_ids):
                            if first!=second: transfers.append({"station_id":first_id+":"+second_id,"station_name":display_station_name(first_station["name"])+" ↔ "+display_station_name(second_station["name"]),"from_route":origin_routes[first],"to_route":destination_routes[second]})
            transfers=list({(item["station_id"],item["from_route"]["route_id"],item["to_route"]["route_id"]):item for item in transfers}.values())
        value={"source":self.route_source,"origin":display_station_name(origin_row["name"]),"destination":display_station_name(destination_row["name"]),"direct_routes":direct,"transfer_options":transfers[:12]}
        return value,["GTFS trip routes · "+value["origin"]+" → "+value["destination"]]
