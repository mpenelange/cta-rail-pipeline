import io,json,unittest
from unittest.mock import patch
from cta_pipeline.arrivals import ArrivalsClient,clarification,resolve_station

class ArrivalTests(unittest.TestCase):
    def test_resolves_line_qualified_station(self):
        station=resolve_station("When does the next Blue Line train arrive at Monroe?")
        self.assertEqual(station["name"],"Monroe (Blue)")
    def test_resolves_common_names_and_unlabeled_line_terminal(self):
        self.assertEqual(resolve_station("next train at Jefferson Park")["name"],"Jefferson Park Transit Center")
        self.assertEqual(resolve_station("next Blue Line train at O'Hare")["name"],"O'Hare")
        self.assertEqual(resolve_station("what trains stop at Clark/Lake?")["name"],"Clark/Lake")
    def test_returns_structured_choices_and_resumes_with_selection(self):
        question="show me the next five train arrival times into Monroe"
        prompt=clarification(question)
        self.assertEqual(prompt["type"],"clarification")
        self.assertEqual([option["label"] for option in prompt["options"]],["Monroe — Blue Line","Monroe — Red Line"])
        self.assertEqual(resolve_station(question,station_id="40790")["name"],"Monroe (Blue)")
    def test_fetches_bounded_predictions(self):
        body={"ctatt":{"tmst":"20260830 12:00:00","errCd":"0","eta":[{"rt":"Blue","destNm":"O'Hare","prdt":"p","arrT":"a","isApp":"0","isSch":"0","isDly":"0"}]}}
        def fetch(request,timeout): self.assertNotIn("secret",request.headers.values()); return io.BytesIO(json.dumps(body).encode())
        with patch.dict("os.environ",{"CTA_API_KEY":"secret"},clear=True): result=ArrivalsClient(fetch).fetch({"map_id":"40790","name":"Monroe (Blue)"})
        self.assertEqual(result["station_name"],"Monroe — Blue Line"); self.assertEqual(result["predictions"][0]["rt"],"Blue Line")
