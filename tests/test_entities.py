import unittest
from cta_pipeline.entities import EntityResolver

class EntityResolverTests(unittest.TestCase):
    def setUp(self): self.resolver=EntityResolver()
    def test_prefers_full_clark_lake_entity_over_lake(self):
        result=self.resolver.analyze("what trains can I take at Clark and Lake?")
        self.assertEqual([row["map_id"] for row in result["stations"][0]["candidates"]],["40380"])
    def test_route_qualifier_disambiguates_one_station(self):
        result=self.resolver.analyze("next Blue Line train at Monroe")
        self.assertEqual([row["map_id"] for row in result["stations"][0]["candidates"]],["40790"])
    def test_route_does_not_filter_a_trip_destination(self):
        result=self.resolver.analyze("take the Blue Line from Western to 35th Street")
        self.assertEqual(len(result["stations"]),2); self.assertGreater(len(result["stations"][0]["candidates"]),1); self.assertGreater(len(result["stations"][1]["candidates"]),1)
    def test_route_and_freshness_are_independent_features(self):
        route=self.resolver.analyze("list every Red Line stop"); fresh=self.resolver.analyze("what are the next ten trains at Clark and Lake")
        self.assertEqual(route["routes"][0]["name"],"Red Line"); self.assertEqual(route["stations"],[]); self.assertTrue(fresh["freshness_requested"])
