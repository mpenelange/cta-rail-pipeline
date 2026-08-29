import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cta_pipeline.arrivals import ArrivalsError, PlannerError, TrainTrackerArrivalsClient, load_station_catalog, parse_arrivals_document, plan_lookup, validate_arrivals_plan
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading


class StationCatalogTests(unittest.TestCase):
    def test_bundled_catalog_has_every_gtfs_parent_and_both_western_blue_stations(self):
        catalog = load_station_catalog()
        self.assertEqual(catalog["source"]["parent_station_count"], 143)
        self.assertEqual(len(catalog["stations"]), 143)
        western = {row["name"]: row["map_id"] for row in catalog["stations"] if row["name"].startswith("Western (Blue")}
        self.assertEqual(western, {
            "Western (Blue - Forest Park Branch)": "40220",
            "Western (Blue - O'Hare Branch)": "40670",
        })

    def test_planner_sends_bounded_untrusted_catalog_and_accepts_strict_none(self):
        captured = {}
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *_args): self.close()
        def fetcher(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response(json.dumps({"choices":[{"message":{"content":json.dumps({"operation":"none"})}}]}).encode())
        with patch.dict("os.environ", {"OPENAI_BASE_URL":"https://model.invalid/v1", "OPENAI_API_KEY":"secret", "OPENAI_MODEL":"planner"}, clear=True):
            plan = plan_lookup("What is the Red Line status?", load_station_catalog(), fetcher=fetcher)
        self.assertEqual(plan, {"operation":"none"})
        payload = json.loads(captured["request"].data)
        self.assertEqual(captured["timeout"], 20)
        self.assertEqual(payload["response_format"], {"type":"json_object"})
        self.assertIn("untrusted", payload["messages"][0]["content"].lower())
        self.assertIn("40010", payload["messages"][1]["content"])
        self.assertNotIn("secret", json.dumps(payload))

    def test_arrivals_client_builds_fixed_query_and_returns_canonical_live_prediction(self):
        captured = {}
        document = {"ctatt":{"tmst":"20260829 12:00:00", "errCd":"0", "errNm":None, "eta":[{
            "staId":"40670", "stpId":"30130", "staNm":"Western", "stpDe":"Service toward O'Hare",
            "rn":"123", "rt":"Blue", "destSt":"30171", "destNm":"O'Hare", "trDr":"1",
            "prdt":"20260829 11:59:30", "arrT":"20260829 12:04:30", "isApp":"0", "isSch":"0",
            "isFlt":"0", "isDly":"1", "flags":None, "lat":"41.9", "lon":"-87.7", "heading":"320"
        }]}}
        class Response(io.BytesIO): pass
        def fetcher(request, timeout):
            captured["url"] = request.full_url; captured["headers"] = dict(request.header_items()); captured["timeout"] = timeout
            return Response(json.dumps(document).encode())
        with patch.dict("os.environ", {"CTA_API_KEY":"cta-secret"}, clear=True):
            result = TrainTrackerArrivalsClient(fetcher=fetcher).fetch("40670", "Western (Blue - O'Hare Branch)")
        self.assertEqual(captured["timeout"], 15)
        self.assertEqual(captured["url"], "https://lapi.transitchicago.com/api/1.0/ttarrivals.aspx?key=cta-secret&mapid=40670&max=10&outputType=JSON")
        self.assertIn("User-agent", captured["headers"])
        self.assertEqual(result["station"], {"map_id":"40670", "name":"Western (Blue - O'Hare Branch)"})
        self.assertEqual(result["as_of"], "2026-08-29T12:00:00-05:00")
        self.assertEqual(result["predictions"][0]["wait_seconds"], 270)
        self.assertEqual(result["predictions"][0]["wait_minutes"], 5)
        self.assertTrue(result["predictions"][0]["delayed"])
        self.assertFalse(result["predictions"][0]["scheduled"])

    def test_arrivals_parser_accepts_empty_eta_and_optional_authoritative_fields(self):
        empty = parse_arrivals_document({"ctatt":{"tmst":"20260829 12:00:00", "errCd":"0", "eta":[]}}, "40670", "Western")
        self.assertEqual(empty["predictions"], [])
        item = {"staId":"40670", "stpId":"30130", "staNm":"Western", "stpDe":"Service toward O'Hare",
                "rn":"123", "rt":"Blue", "destSt":"30171", "destNm":"O'Hare", "trDr":"1",
                "prdt":"20260829 11:59:30", "arrT":"20260829 12:04:30", "isApp":"1", "isSch":"0", "isFlt":"0", "isDly":"0",
                "unknownFutureField":"ignored by the strict output allowlist"}
        result = parse_arrivals_document({"ctatt":{"tmst":"20260829 12:00:00", "errCd":"0", "eta":[item]}}, "40670", "Western")
        prediction = result["predictions"][0]
        self.assertNotIn("lat", prediction)
        self.assertNotIn("lon", prediction)
        self.assertNotIn("heading", prediction)
        self.assertTrue(prediction["approaching"])
        self.assertNotIn("unknownFutureField",prediction)

    def test_catalog_malformed_shapes_ids_coordinates_and_duplicates_fail_safely(self):
        valid=load_station_catalog()
        mutations=[]
        bad=json.loads(json.dumps(valid)); bad["stations"][0]["map_id"]="12345"; mutations.append(bad)
        bad=json.loads(json.dumps(valid)); bad["stations"][1]["map_id"]=bad["stations"][0]["map_id"]; mutations.append(bad)
        bad=json.loads(json.dumps(valid)); bad["stations"][0]["lat"]=999; mutations.append(bad)
        mutations.extend(([], {"source":{},"stations":[]}))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"catalog.json"
            for value in mutations:
                with self.subTest(value_type=type(value).__name__):
                    path.write_text(json.dumps(value))
                    with self.assertRaisesRegex(ValueError,"station catalog unavailable"):
                        load_station_catalog(path)

    def test_catalog_rejects_well_shaped_143_row_content_replacement(self):
        replacement=load_station_catalog()
        replacement=json.loads(json.dumps(replacement))
        replacement["stations"][0]["name"]="Plausible Replacement Name"
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"catalog.json"
            path.write_text(json.dumps(replacement,separators=(",",":")))
            with self.assertRaisesRegex(ValueError,"station catalog unavailable"):
                load_station_catalog(path)

    def test_planner_rejects_invalid_operations_shapes_and_non_catalog_station_ids(self):
        outputs=[{"operation":"delete","url":"https://evil.invalid"}, {"operation":"none","station_id":"40670"},
                 {"operation":"arrivals","station_id":"49999"}, {"operation":"clarify","question":""}]
        class Response(io.BytesIO): pass
        def fetcher(_request, timeout):
            content=json.dumps(outputs.pop(0))
            return Response(json.dumps({"choices":[{"message":{"content":content}}]}).encode())
        env={"OPENAI_BASE_URL":"https://model.invalid/v1","OPENAI_API_KEY":"secret","OPENAI_MODEL":"model"}
        with patch.dict("os.environ",env,clear=True):
            for _ in range(4):
                with self.assertRaises(Exception): plan_lookup("question",load_station_catalog(),fetcher=fetcher)

    def test_station_family_validation_is_generic_for_harlem_and_chicago(self):
        catalog=load_station_catalog()
        ambiguous=validate_arrivals_plan("Next train at Harlem Blue Line?",catalog,{"operation":"arrivals","station_id":"40750"})
        self.assertEqual(ambiguous["operation"],"clarify")
        self.assertIn("40750",{row["map_id"] for row in catalog["stations"] if row["name"] in ambiguous["question"]})
        self.assertEqual(validate_arrivals_plan("Next train at Harlem Blue Line O'Hare branch?",catalog,{"operation":"arrivals","station_id":"40750"})["operation"],"arrivals")
        self.assertEqual(validate_arrivals_plan("Next train at Chicago Red Line?",catalog,{"operation":"arrivals","station_id":"41450"})["operation"],"arrivals")

    def test_parser_rejects_nonexistent_spring_time_and_unresolved_fall_fold(self):
        base={"staId":"40670","stpId":"30130","staNm":"Western","stpDe":"Service toward O'Hare","rn":"123","rt":"Blue",
              "destSt":"30171","destNm":"O'Hare","trDr":"1","isApp":"0","isSch":"0","isFlt":"0","isDly":"0"}
        documents=(
            {"ctatt":{"tmst":"20260308 01:59:00","errCd":"0","eta":[{**base,"prdt":"20260308 01:58:30","arrT":"20260308 02:30:00"}]}},
            {"ctatt":{"tmst":"20261101 01:10:00","errCd":"0","eta":[{**base,"prdt":"20261101 01:09:30","arrT":"20261101 01:20:00"}]}},
        )
        for document in documents:
            with self.subTest(tmst=document["ctatt"]["tmst"]), self.assertRaises(ValueError):
                parse_arrivals_document(document,"40670","Western")

    def test_parser_rejects_provider_errors_malformed_fields_controls_dst_coordinates_and_duplicates_atomically(self):
        base={"staId":"40670","stpId":"30130","staNm":"Western","stpDe":"Service toward O'Hare","rn":"123","rt":"Blue",
              "destSt":"30171","destNm":"O'Hare","trDr":"1","prdt":"20260829 11:59:30","arrT":"20260829 12:04:30",
              "isApp":"0","isSch":"0","isFlt":"0","isDly":"0","lat":"41.9","lon":"-87.7","heading":"320"}
        documents=[]
        documents.append({"ctatt":{"tmst":"20260829 12:00:00","errCd":"100","errNm":"secret provider detail","eta":[]}})
        for field,value in (("staNm","bad\nname"),("rt","evil"),("trDr","2"),("lat","90"),("heading","360"),("arrT","20260308 02:30:00")):
            item={**base,field:value}; documents.append({"ctatt":{"tmst":"20260829 12:00:00","errCd":"0","eta":[item]}})
        documents.append({"ctatt":{"tmst":"20260829 12:00:00","errCd":"0","eta":[base,dict(base)]}})
        documents.extend(({}, {"ctatt":{"tmst":"bad","errCd":"0","eta":[]}}, {"ctatt":{"tmst":"20260829 12:00:00","errCd":"0","eta":"bad"}}))
        for document in documents:
            with self.subTest(document=str(document)[:50]), self.assertRaises(ValueError):
                parse_arrivals_document(document,"40670","Western")

    def test_parser_rejects_non_finite_coordinates(self):
        base={"staId":"40670","stpId":"30130","staNm":"Western","stpDe":"Service toward O'Hare","rn":"123","rt":"Blue",
              "destSt":"30171","destNm":"O'Hare","trDr":"1","prdt":"20260829 11:59:30","arrT":"20260829 12:04:30",
              "isApp":"0","isSch":"0","isFlt":"0","isDly":"0","lat":"41.9","lon":"-87.7"}
        for field,value in (("lat","NaN"),("lon","NaN"),("lat","Inf"),("lon","-Inf")):
            with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                item={**base,field:value}
                parse_arrivals_document({"ctatt":{"tmst":"20260829 12:00:00","errCd":"0","eta":[item]}},"40670","Western")

    def test_parser_requires_five_digit_station_and_stop_ids(self):
        item={"staId":"40670","stpId":"1","staNm":"Western","stpDe":"Service toward O'Hare","rn":"123","rt":"Blue",
              "destSt":"30171","destNm":"O'Hare","trDr":"1","prdt":"20260829 11:59:30","arrT":"20260829 12:04:30",
              "isApp":"0","isSch":"0","isFlt":"0","isDly":"0"}
        with self.assertRaises(ValueError):
            parse_arrivals_document({"ctatt":{"tmst":"20260829 12:00:00","errCd":"0","eta":[item]}},"40670","Western")

    def test_fall_back_interval_uses_consistent_utc_instants_across_folds(self):
        item={"staId":"40670","stpId":"30130","staNm":"Western","stpDe":"Service toward O'Hare","rn":"123","rt":"Blue",
              "destSt":"30171","destNm":"O'Hare","trDr":"1","prdt":"20261101 00:58:00","arrT":"20261101 02:01:00",
              "isApp":"0","isSch":"0","isFlt":"0","isDly":"0"}
        result=parse_arrivals_document({"ctatt":{"tmst":"20261101 00:59:00","errCd":"0","eta":[item]}},"40670","Western")
        self.assertEqual(result["predictions"][0]["wait_seconds"],7320)
        self.assertEqual(result["as_of"],"2026-11-01T00:59:00-05:00")
        self.assertEqual(result["predictions"][0]["arrT"],"2026-11-01T02:01:00-06:00")

    def test_fall_back_fold_is_resolved_only_when_relationships_select_one_instant(self):
        item={"staId":"40670","stpId":"30130","staNm":"Western","stpDe":"Service toward O'Hare","rn":"123","rt":"Blue",
              "destSt":"30171","destNm":"O'Hare","trDr":"1","prdt":"20261101 01:49:00","arrT":"20261101 01:10:00",
              "isApp":"0","isSch":"0","isFlt":"0","isDly":"0"}
        result=parse_arrivals_document({"ctatt":{"tmst":"20261101 01:50:00","errCd":"0","eta":[item]}},"40670","Western")
        self.assertEqual(result["as_of"],"2026-11-01T01:50:00-05:00")
        self.assertEqual(result["predictions"][0]["prdt"],"2026-11-01T01:49:00-05:00")
        self.assertEqual(result["predictions"][0]["arrT"],"2026-11-01T01:10:00-06:00")
        self.assertEqual(result["predictions"][0]["wait_seconds"],1200)

    def test_prediction_times_obey_operational_horizon_and_generation_relationships(self):
        base={"staId":"40670","stpId":"30130","staNm":"Western","stpDe":"Service toward O'Hare","rn":"123","rt":"Blue",
              "destSt":"30171","destNm":"O'Hare","trDr":"1","prdt":"20260829 11:59:30","arrT":"20260829 12:04:30",
              "isApp":"0","isSch":"0","isFlt":"0","isDly":"0"}
        invalid=(
            {**base,"arrT":"20990101 00:00:00"},
            {**base,"prdt":"20260829 12:05:00"},
            {**base,"prdt":"20260829 10:00:00"},
            {**base,"arrT":"20260829 11:50:00"},
        )
        for item in invalid:
            with self.subTest(prdt=item["prdt"],arrT=item["arrT"]), self.assertRaises(ValueError):
                parse_arrivals_document({"ctatt":{"tmst":"20260829 12:00:00","errCd":"0","eta":[item]}},"40670","Western")
        due={**base,"prdt":"20260829 11:59:00","arrT":"20260829 11:59:01","isApp":"1"}
        result=parse_arrivals_document({"ctatt":{"tmst":"20260829 12:00:00","errCd":"0","eta":[due]}},"40670","Western")
        self.assertEqual(result["predictions"][0]["wait_seconds"],0)

    def test_predictions_are_sorted_by_resolved_arrival_with_stable_tie_breakers(self):
        base={"staId":"40670","stpId":"30130","staNm":"Western","stpDe":"Service toward O'Hare","rt":"Blue",
              "destSt":"30171","destNm":"O'Hare","trDr":"1","prdt":"20260829 11:59:30",
              "isApp":"0","isSch":"0","isFlt":"0","isDly":"0"}
        eta=[
            {**base,"rn":"300","arrT":"20260829 12:09:00"},
            {**base,"rn":"200","arrT":"20260829 12:04:00"},
            {**base,"rn":"100","stpId":"30131","arrT":"20260829 12:04:00"},
        ]
        result=parse_arrivals_document({"ctatt":{"tmst":"20260829 12:00:00","errCd":"0","eta":eta}},"40670","Western")
        self.assertEqual([(row["rn"],row["stpId"]) for row in result["predictions"]],
                         [("100","30131"),("200","30130"),("300","30130")])

    def test_arrivals_client_rejects_oversized_document_before_json_parsing(self):
        class Response(io.BytesIO): pass
        with patch.dict("os.environ",{"CTA_API_KEY":"secret"},clear=True):
            with self.assertRaises(Exception):
                TrainTrackerArrivalsClient(fetcher=lambda *_args,**_kwargs:Response(b"x"*(1024*1024+1))).fetch("40670","Western")

    def test_openai_and_cta_redirects_never_forward_credentials(self):
        sink_headers=[]
        class Sink(BaseHTTPRequestHandler):
            def log_message(self,*_args): pass
            def do_GET(self): sink_headers.append((self.headers.get("Authorization"),self.path)); self.send_response(204); self.end_headers()
            do_POST=do_GET
        sink=ThreadingHTTPServer(("127.0.0.1",0),Sink); threading.Thread(target=sink.serve_forever,daemon=True).start()
        self.addCleanup(sink.server_close); self.addCleanup(sink.shutdown)
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self,*_args): pass
            def do_POST(self): self.rfile.read(int(self.headers.get("Content-Length","0"))); self.redirect()
            def do_GET(self): self.redirect()
            def redirect(self):
                self.send_response(302); self.send_header("Location",f"http://127.0.0.1:{sink.server_address[1]}/sink"); self.end_headers()
        redirect=ThreadingHTTPServer(("127.0.0.1",0),Redirect); threading.Thread(target=redirect.serve_forever,daemon=True).start()
        self.addCleanup(redirect.server_close); self.addCleanup(redirect.shutdown)
        env={"OPENAI_BASE_URL":f"http://127.0.0.1:{redirect.server_address[1]}/v1","OPENAI_API_KEY":"openai-secret","OPENAI_MODEL":"model","CTA_API_KEY":"cta-secret"}
        with patch.dict("os.environ",env,clear=True):
            with self.assertRaises(PlannerError): plan_lookup("status?",load_station_catalog())
            client=TrainTrackerArrivalsClient(); client.url=f"http://127.0.0.1:{redirect.server_address[1]}/ttarrivals.aspx"
            with self.assertRaises(ArrivalsError): client.fetch("40670","Western")
        self.assertEqual(sink_headers,[])


if __name__ == "__main__":
    unittest.main()
