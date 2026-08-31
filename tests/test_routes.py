import unittest
from cta_pipeline.routes import display_station_name,route_name

class RouteTests(unittest.TestCase):
    def test_expands_cta_route_codes(self):
        self.assertEqual(route_name("G"),"Green Line"); self.assertEqual(route_name("Brn"),"Brown Line")
        self.assertEqual(route_name("Org"),"Orange Line"); self.assertEqual(route_name("P"),"Purple Line")
    def test_expands_station_qualifiers_without_losing_branches(self):
        self.assertEqual(display_station_name("Monroe (Blue)"),"Monroe — Blue Line")
        self.assertEqual(display_station_name("Western (Blue - Forest Park Branch)"),"Western — Blue Line - Forest Park Branch")
        self.assertEqual(display_station_name("Belmont (Red/Brown/Purple)"),"Belmont — Red Line/Brown Line/Purple Line")
