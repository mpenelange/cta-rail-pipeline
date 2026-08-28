import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from cta_pipeline.__main__ import main, run_live
from cta_pipeline.db import Database
from cta_pipeline.telemetry import (AnomalyExplainer, GTFSRealtimeClient, TelemetryError,
                                    TelemetryPipeline, canonical_feed)
from cta_pipeline.server import make_server


NOW = 2_000_000_000


def envelope(timestamp=NOW, entities=None):
    return {"header": {"gtfsRealtimeVersion": "2.0", "timestamp": str(timestamp)},
            "entity": entities or []}


def vehicle(entity_id="v1", vehicle_id="train-1", timestamp=NOW, lat=41.0, lon=-87.0,
            route="Red", direction=1):
    return {"id": entity_id, "vehicle": {"trip": {"tripId": "t1", "routeId": route,
             "directionId": direction}, "vehicle": {"id": vehicle_id, "label": "<Train>"},
             "position": {"latitude": lat, "longitude": lon}, "stopId": "stop-a",
             "currentStatus": "STOPPED_AT", "timestamp": str(timestamp)}}


def trip(entity_id="t1", delay=600, arrival=NOW + 600, route="Red", direction=1,
         stop="stop-a"):
    return {"id": entity_id, "tripUpdate": {"trip": {"tripId": entity_id,
            "routeId": route, "directionId": direction}, "timestamp": str(NOW),
            "stopTimeUpdate": [{"stopSequence": 1, "stopId": stop,
            "arrival": {"delay": delay, "time": str(arrival)}}]}}


class ClientEnvelopeTests(unittest.TestCase):
    def test_key_encoded_timeout_injected_fetcher_and_no_secret_in_error(self):
        seen = {}
        def fetch(req, timeout):
            seen.update(url=req.full_url, timeout=timeout)
            return json.dumps(envelope()).encode()
        client = GTFSRealtimeClient("VehiclePositions", api_key="a b&secret", fetcher=fetch,
                                    timeout=3)
        _, doc = client.fetch()
        self.assertEqual(doc["header"]["timestamp"], NOW)
        self.assertIn("key=a+b%26secret", seen["url"]); self.assertEqual(seen["timeout"], 3)
        with self.assertRaisesRegex(TelemetryError, "CTA_API_KEY is required"):
            GTFSRealtimeClient("TripUpdates", api_key="").fetch()
        def broken(req, timeout): raise OSError(req.full_url)
        with self.assertRaises(TelemetryError) as caught:
            GTFSRealtimeClient("TripUpdates", api_key="do-not-leak", fetcher=broken).fetch()
        self.assertNotIn("do-not-leak", str(caught.exception))

    def test_schema_variants_malformed_error_and_bounds(self):
        valid = envelope(entities=[{"id": "deleted", "isDeleted": True}, vehicle()])
        raw = json.dumps(valid).encode()
        _, got = GTFSRealtimeClient("VehiclePositions", api_key="x",
                                    fetcher=lambda *_a, **_k: raw).fetch()
        self.assertEqual(len(got["entity"]), 2)
        bad = [{}, [], {"header": {}, "entity": []},
               {"header": {"gtfsRealtimeVersion": "2.0", "timestamp": NOW}, "entity": {}},
               {"header": {"gtfsRealtimeVersion": "2.0", "timestamp": NOW},
                "entity": [{"id": "x", "vehicle": "bad"}]},
               {"header": {"gtfsRealtimeVersion": "2.0", "timestamp": NOW},
                "entity": [], "error": "denied"}]
        for doc in bad:
            with self.subTest(doc=doc), self.assertRaises(TelemetryError):
                canonical_feed("VehiclePositions", doc)
        with self.assertRaisesRegex(TelemetryError, "too large"):
            GTFSRealtimeClient("VehiclePositions", api_key="x", max_response_bytes=10,
                               fetcher=lambda *_a, **_k: io.BytesIO(b"x" * 11)).fetch()


class PersistenceAnomalyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "db"); self.db.migrate()

    def pipeline(self, vehicles, trips, clock=lambda: "2033-05-18T03:33:20Z", **kw):
        payloads = [json.dumps(vehicles).encode(), json.dumps(trips).encode()]
        clients = [GTFSRealtimeClient("VehiclePositions", "x", lambda *_a, **_k: payloads[0]),
                   GTFSRealtimeClient("TripUpdates", "x", lambda *_a, **_k: payloads[1])]
        return TelemetryPipeline(self.db, *clients, clock=clock, **kw)

    def test_migration_idempotence_dedupe_and_latest_history(self):
        self.db.migrate()
        p = self.pipeline(envelope(entities=[vehicle()]), envelope(entities=[trip()]))
        one = p.ingest(); two = p.ingest()
        self.assertEqual(one["vehicles"], 1); self.assertEqual(two["snapshots_created"], 0)
        self.assertEqual(self.db.scalar("select count(*) from telemetry_snapshots"), 2)
        self.assertEqual(self.db.scalar("select count(*) from vehicle_state"), 1)
        self.assertEqual(self.db.scalar("select count(*) from vehicle_observations"), 1)
        self.assertEqual(self.db.scalar("select count(*) from trip_prediction_observations"), 1)
        self.assertEqual(self.db.scalar("select count(*) from schema_migrations"), 4)

    def test_retention_prunes_old_history_but_keeps_current(self):
        with self.db.connect() as con:
            con.execute("insert into telemetry_runs(started_at,finished_at,status) values('old','old','success')")
            run = con.execute("select last_insert_rowid()").fetchone()[0]
            con.execute("insert into telemetry_snapshots(run_id,feed_type,content_hash,canonical_json,feed_timestamp,created_at) values(?,?,?,?,?,?)",
                        (run, "vehicle_positions", "h", "{}", 1, "2000-01-01T00:00:00Z"))
        self.pipeline(envelope(), envelope(), retention_hours=24).ingest()
        self.assertEqual(self.db.scalar("select count(*) from telemetry_snapshots where content_hash='h'"), 0)

    def test_failed_or_malformed_cycle_is_atomic_and_audit_redacts_key(self):
        secret="database-secret-must-not-appear"
        def broken(req,timeout): raise OSError(req.full_url)
        pipeline=TelemetryPipeline(self.db,GTFSRealtimeClient("VehiclePositions",secret,broken),
            GTFSRealtimeClient("TripUpdates",secret,broken),clock=lambda:"2033-05-18T03:33:20Z")
        with self.assertRaises(TelemetryError): pipeline.ingest()
        with self.db.connect() as con:
            error=con.execute("select error from telemetry_runs order by id desc limit 1").fetchone()[0]
        self.assertNotIn(secret,error); self.assertEqual(self.db.scalar("select count(*) from telemetry_snapshots"),0)
        malformed=GTFSRealtimeClient("VehiclePositions","x",lambda *_a,**_k:json.dumps({}).encode())
        good=GTFSRealtimeClient("TripUpdates","x",lambda *_a,**_k:json.dumps(envelope()).encode())
        with self.assertRaises(TelemetryError): TelemetryPipeline(self.db,malformed,good).ingest()
        self.assertEqual(self.db.scalar("select count(*) from vehicle_state"),0)

    def test_all_deterministic_anomaly_rules_and_fingerprints(self):
        vehicles = envelope(NOW - 500, [vehicle(timestamp=NOW - 500)])
        trips = envelope(NOW - 500, [trip("t1", delay=600, arrival=NOW + 60),
                                     trip("t2", delay=0, arrival=NOW + 2000)])
        p = self.pipeline(vehicles, trips, stale_seconds=120, delay_seconds=300,
                          gap_seconds=900, stationary_seconds=60)
        first = p.ingest()
        # A later identical-position observation supports stationary detection.
        p.clock = lambda: "2033-05-18T03:35:20Z"
        p.vehicle_client.fetcher = lambda *_a, **_k: json.dumps(envelope(NOW - 380, [vehicle(timestamp=NOW - 380)])).encode()
        second = p.ingest()
        with self.db.connect() as con: kinds = {r[0] for r in con.execute("select kind from anomalies")}
        self.assertTrue({"stale_feed", "material_delay", "arrival_gap", "stationary_vehicle"} <= kinds)
        self.assertGreater(first["new_anomalies"], 0)
        self.assertGreater(second["new_anomalies"], 0)
        count = self.db.scalar("select count(*) from anomalies")
        p.ingest()
        self.assertEqual(self.db.scalar("select count(*) from anomalies"), count)

    def test_llm_only_new_fingerprint_and_fallback(self):
        class Explain:
            def __init__(self): self.calls = 0
            def explain(self, anomaly):
                self.calls += 1
                return {"text": "Bounded explanation", "method": "openai", "model": "test"}
        ex = Explain(); p = self.pipeline(envelope(NOW - 500), envelope(NOW - 500), stale_seconds=1,
                                          explainer=ex)
        p.ingest(); p.clock=lambda: "2033-05-18T03:35:20Z"; p.ingest(); self.assertEqual(ex.calls, 2)
        with self.db.connect() as con: row = con.execute("select deterministic_text,explanation_text,method,model from anomalies").fetchone()
        self.assertEqual(row[1], row[0]); self.assertEqual(row[2], "deterministic-current")
        class Broken:
            def explain(self, anomaly): raise RuntimeError("provider down")
        db2 = Database(Path(self.tmp.name) / "db2"); db2.migrate()
        self.pipeline(envelope(NOW - 600), envelope(), stale_seconds=1,
                      explainer=Broken()).__class__(db2,
            GTFSRealtimeClient("VehiclePositions", "x", lambda *_a, **_k: json.dumps(envelope(NOW-600)).encode()),
            GTFSRealtimeClient("TripUpdates", "x", lambda *_a, **_k: json.dumps(envelope()).encode()),
            clock=lambda: "2033-05-18T03:33:20Z", stale_seconds=1, explainer=Broken()).ingest()
        with db2.connect() as con: fallback = con.execute("select deterministic_text,explanation_text,method from anomalies").fetchone()
        self.assertEqual(fallback[0], fallback[1]); self.assertEqual(fallback[2], "deterministic-fallback")


class APIAndCLITests(unittest.TestCase):
    def test_anomaly_explainer_strict_json_bounded_context_and_secret(self):
        seen={}; candidate=json.dumps({"text":"No cause is asserted."})
        response=json.dumps({"choices":[{"message":{"content":candidate}}]}).encode()
        def fetch(req,timeout): seen.update(body=req.data,auth=req.headers["Authorization"],timeout=timeout); return response
        old=os.environ.get("OPENAI_API_KEY"); os.environ["OPENAI_API_KEY"]="model-secret"
        try:
            result=AnomalyExplainer(fetch).explain({"kind":"stale_feed","severity":"warning","entity_key":"feed","deterministic_text":"old","context":{"age_seconds":500}})
            self.assertEqual(result["method"],"openai"); self.assertEqual(seen["timeout"],20)
            self.assertNotIn(b"model-secret",seen["body"]); self.assertIn("model-secret",seen["auth"])
            with self.assertRaises(TelemetryError): AnomalyExplainer(lambda *_a,**_k:b'{"choices":[]}').explain({"kind":"x","severity":"x","entity_key":"x","deterministic_text":"x","context":{}})
        finally:
            if old is None: os.environ.pop("OPENAI_API_KEY",None)
            else: os.environ["OPENAI_API_KEY"]=old

    def test_telemetry_apis_bounds_health_and_dashboard_escaping(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "db"); db.migrate()
            rawv = json.dumps(envelope(entities=[vehicle()])).encode()
            rawt = json.dumps(envelope(entities=[trip()])).encode()
            TelemetryPipeline(db, GTFSRealtimeClient("VehiclePositions", "x", lambda *_a, **_k: rawv),
                              GTFSRealtimeClient("TripUpdates", "x", lambda *_a, **_k: rawt),
                              clock=lambda: "2033-05-18T03:33:20Z").ingest()
            server = make_server(db, 0); thread = threading.Thread(target=server.serve_forever); thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                summary = json.loads(urlopen(base + "/api/telemetry").read())
                self.assertEqual(summary["active_vehicles"], 1)
                self.assertLessEqual(len(json.loads(urlopen(base + "/api/vehicles?limit=1").read())["vehicles"]), 1)
                self.assertIn("anomalies", json.loads(urlopen(base + "/api/anomalies?limit=2").read()))
                health = json.loads(urlopen(base + "/api/health").read())
                self.assertIn("telemetry", health); self.assertNotIn("key", json.dumps(health).lower())
                page = urlopen(base + "/").read().decode()
                self.assertIn("Pipeline online", page); self.assertNotIn("<Train>", page)
                self.assertIn("Live operations", page)
                with self.assertRaises(HTTPError) as caught: urlopen(base + "/api/vehicles?limit=99999")
                self.assertEqual(caught.exception.code, 400)
            finally: server.shutdown(); server.server_close(); thread.join()

    def test_telemetry_ingest_cli_json_and_missing_key(self):
        with tempfile.TemporaryDirectory() as d:
            old_db = os.environ.get("CTA_DB_PATH"); old_key = os.environ.pop("CTA_API_KEY", None)
            os.environ["CTA_DB_PATH"] = str(Path(d) / "db")
            try:
                out = io.StringIO()
                with redirect_stdout(out): self.assertEqual(main(["telemetry-ingest"]), 1)
                self.assertIn("CTA_API_KEY", out.getvalue())
            finally:
                if old_db is None: os.environ.pop("CTA_DB_PATH", None)
                else: os.environ["CTA_DB_PATH"] = old_db
                if old_key is not None: os.environ["CTA_API_KEY"] = old_key

    def test_http_and_fixture_poll_run_concurrently_and_shutdown_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database(Path(d)/"db"); db.migrate(); calls={"telemetry":0,"alerts":0}
            class Poll:
                def ingest(self): calls["telemetry"]+=1
            class Alerts:
                def ingest(self,with_ridership=False): calls["alerts"]+=1
            server=make_server(db,0); stop=threading.Event()
            worker=threading.Thread(target=run_live,args=(db,server),kwargs={"telemetry":Poll(),"alerts":Alerts(),"telemetry_interval":.02,"alerts_interval":.05,"stop_event":stop})
            worker.start()
            try:
                health=json.loads(urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/health",timeout=2).read())
                self.assertEqual(health["status"],"ok")
                stop.wait(.08); self.assertGreaterEqual(calls["telemetry"],2); self.assertGreaterEqual(calls["alerts"],1)
            finally:
                stop.set(); server.shutdown(); worker.join(2)
            self.assertFalse(worker.is_alive())


if __name__ == "__main__": unittest.main()
