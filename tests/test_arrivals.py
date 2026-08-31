import io,json,unittest
from unittest.mock import patch
from cta_pipeline.arrivals import ArrivalsClient,resolve_station,wants_arrivals

class ArrivalTests(unittest.TestCase):
    def test_resolves_line_qualified_station(self):
        station=resolve_station("When does the next Blue Line train arrive at Monroe?")
        self.assertEqual(station["name"],"Monroe (Blue)"); self.assertTrue(wants_arrivals("next train at Monroe"))
    def test_resolves_common_names_and_unlabeled_line_terminal(self):
        self.assertEqual(resolve_station("next train at Jefferson Park")["name"],"Jefferson Park Transit Center")
        self.assertEqual(resolve_station("next Blue Line train at O'Hare")["name"],"O'Hare")
    def test_fetches_bounded_predictions(self):
        body={"ctatt":{"tmst":"20260830 12:00:00","errCd":"0","eta":[{"rt":"Blue","destNm":"O'Hare","prdt":"p","arrT":"a","isApp":"0","isSch":"0","isDly":"0"}]}}
        def fetch(request,timeout): self.assertNotIn("secret",request.headers.values()); return io.BytesIO(json.dumps(body).encode())
        with patch.dict("os.environ",{"CTA_API_KEY":"secret"},clear=True): result=ArrivalsClient(fetch).fetch({"map_id":"40790","name":"Monroe (Blue)"})
        self.assertEqual(result["station_name"],"Monroe (Blue)"); self.assertEqual(result["predictions"][0]["rt"],"Blue")
