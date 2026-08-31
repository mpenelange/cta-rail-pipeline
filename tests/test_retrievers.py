import unittest
from cta_pipeline.retrievers import RetrieverRegistry

class FakePipeline:
    def snapshot(self): return {"source":"CTA alerts","as_of":"now"}
    def retrieve(self,question): return [{"source_id":"a1"}]

class RetrieverTests(unittest.TestCase):
    def test_clark_lake_routes_come_from_gtfs(self):
        registry=RetrieverRegistry(FakePipeline()); evidence,sources=registry.retrieve(["station_routes"],"what trains?",{"station":{"map_id":"40380","name":"Clark/Lake"}})
        self.assertEqual([route["name"] for route in evidence["station_routes"]["routes"]],["Blue Line","Brown Line","Green Line","Orange Line","Purple Line","Pink Line"])
        self.assertEqual(sources,["GTFS routes · Clark/Lake"])
    def test_trip_routes_find_direct_service(self):
        registry=RetrieverRegistry(FakePipeline()); entities={"origin":{"map_id":"41480","name":"Western (Brown)"},"destination":{"map_id":"41230","name":"47th (Red)"}}
        evidence,_sources=registry.retrieve(["trip_routes"],"how do I get there?",entities)
        self.assertEqual(evidence["trip_routes"]["direct_routes"],[]); self.assertTrue(evidence["trip_routes"]["transfer_options"])
    def test_blue_to_red_uses_interstation_gtfs_transfer(self):
        registry=RetrieverRegistry(FakePipeline()); entities={"origin":{"map_id":"40670","name":"Western (Blue - O'Hare Branch)"},"destination":{"map_id":"40190","name":"Sox-35th"}}
        evidence,_sources=registry.retrieve(["trip_routes"],"how do I get there?",entities); options=evidence["trip_routes"]["transfer_options"]
        self.assertTrue(any("Jackson" in option["station_name"] and option["from_route"]["name"]=="Blue Line" and option["to_route"]["name"]=="Red Line" for option in options))
    def test_route_stations_lists_every_red_line_station(self):
        registry=RetrieverRegistry(FakePipeline()); route={"route_id":"Red","name":"Red Line","color":"#c60c30"}
        evidence,sources=registry.retrieve(["route_stations"],"all stops",{"route":route}); value=evidence["route_stations"]
        names={row["station_name"] for row in value["stations"]}
        self.assertEqual(value["station_count"],33); self.assertIn("Howard",names); self.assertIn("95th/Dan Ryan",names); self.assertEqual(sources,["GTFS stations · Red Line"])
    def test_route_station_neighborhood_preserves_transfer_connections(self):
        registry=RetrieverRegistry(FakePipeline()); route={"route_id":"Red","name":"Red Line","color":"#c60c30"}
        evidence,_sources=registry.retrieve(["route_stations"],"transfer stations",{"route":route})
        stations={row["station_name"]:row for row in evidence["route_stations"]["stations"]}
        belmont=next(row for name,row in stations.items() if name.startswith("Belmont"))
        self.assertEqual({item["name"] for item in belmont["routes"]},{"Red Line","Brown Line","Purple Line"})
        self.assertTrue(any(link["station_name"]=="Washington" and any(item["name"]=="Blue Line" for item in link["routes"]) for link in stations["Lake (Subway)"]["transfer_links"]))
