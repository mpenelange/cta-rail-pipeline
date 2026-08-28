import gzip
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

from cta_pipeline.__main__ import run_live
from cta_pipeline.db import Database, TELEMETRY_MIGRATION
from cta_pipeline.server import make_server
from cta_pipeline.telemetry import (GTFSRealtimeClient, TelemetryError,
                                    TelemetryPipeline, canonical_feed)
from tests.test_telemetry import NOW, envelope, trip, vehicle


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "cta.db"); self.db.migrate()

    def pipeline(self, vehicles, trips, clocks, **kwargs):
        vraw=json.dumps(vehicles).encode(); traw=json.dumps(trips).encode()
        return TelemetryPipeline(
            self.db,
            GTFSRealtimeClient("VehiclePositions", "x", lambda *_a, **_k: vraw),
            GTFSRealtimeClient("TripUpdates", "x", lambda *_a, **_k: traw),
            clock=lambda: next(clocks), **kwargs)

    def test_stationary_streak_survives_short_poll_intervals_and_migrates_current_schema(self):
        old=Path(self.tmp.name)/"old.db"
        with closing(sqlite3.connect(old)) as con:
            con.executescript(TELEMETRY_MIGRATION)
            con.execute("insert into telemetry_runs(id,started_at,status) values(1,'old','success')")
            con.execute("insert into telemetry_snapshots(run_id,feed_type,content_hash,canonical_json,feed_timestamp,created_at) values(1,'vehicle_positions','legacy',?,1,'old')",(b'{"entity":[]}',))
            con.commit()
        legacy=Database(old); legacy.migrate(); legacy.migrate()
        with legacy.connect() as con:
            columns={r[1] for r in con.execute("pragma table_info(vehicle_state)")}
            migrated_blob=con.execute("select canonical_json from telemetry_snapshots where content_hash='legacy'").fetchone()[0]
        self.assertIn("stationary_since", columns)
        self.assertEqual(gzip.decompress(migrated_blob),b'{"entity":[]}')

        clocks=iter(f"2033-05-18T03:{33+i//2:02d}:{20+(i%2)*30:02d}Z" for i in range(22))
        p=self.pipeline(envelope(entities=[vehicle()]), envelope(), clocks,
                        stationary_seconds=600, stale_seconds=10**9)
        for i in range(22):
            p.vehicle_client.fetcher=lambda *_a, i=i, **_k: json.dumps(
                envelope(NOW+i*30, [vehicle(timestamp=NOW+i*30)])).encode()
            p.ingest()
        self.assertEqual(self.db.scalar("select active from anomalies where kind='stationary_vehicle'"), 1)

    def test_successful_vehicle_feed_authoritatively_reconciles_state(self):
        p=self.pipeline(envelope(entities=[vehicle()]), envelope(), iter(["2033-05-18T03:33:20Z"]))
        p.ingest(); self.assertEqual(self.db.scalar("select count(*) from vehicle_state"), 1)
        p.clock=lambda:"2033-05-18T03:33:50Z"
        p.vehicle_client.fetcher=lambda *_a,**_k:json.dumps(envelope(NOW+30,[])).encode()
        p.ingest(); self.assertEqual(self.db.scalar("select count(*) from vehicle_state"), 0)

    def test_expansion_is_atomic_snapshots_replay_and_repeated_storage_is_bounded(self):
        huge=envelope(entities=[{**trip(f"t{i}"), "tripUpdate": {**trip(f"t{i}")["tripUpdate"],
             "stopTimeUpdate": [{"stopSequence": sequence, "stopId": f"s{sequence}"}
                                for sequence in range(5001)]}} for i in range(11)])
        p=self.pipeline(envelope(), huge, iter(["2033-05-18T03:33:20Z"]))
        with self.assertRaisesRegex(TelemetryError, "expanded predictions"):
            p.ingest()
        self.assertEqual(self.db.scalar("select count(*) from trip_prediction_observations"), 0)

        clocks=iter(f"2033-05-18T03:{i//2:02d}:{(i%2)*30:02d}Z" for i in range(120))
        p=self.pipeline(envelope(entities=[vehicle()]), envelope(entities=[trip(delay=0)]), clocks)
        for _ in range(120): p.ingest()
        self.assertLessEqual(self.db.scalar("select count(*) from vehicle_observations"), 1)
        self.assertLessEqual(self.db.scalar("select count(*) from trip_prediction_observations"), 1)
        with self.db.connect() as con:
            blob=con.execute("select canonical_json from telemetry_snapshots limit 1").fetchone()[0]
        self.assertEqual(json.loads(gzip.decompress(blob)),
                         canonical_feed("VehiclePositions",envelope(entities=[vehicle()])))
        self.assertLess(Path(self.db.path).stat().st_size, 2_000_000)

    def test_duplicate_fingerprint_calls_model_once_and_recurrence_refreshes_display(self):
        class Explain:
            def __init__(self): self.calls=0
            def explain(self, anomaly):
                self.calls+=1; return {"text":"model text","method":"openai","model":"m"}
        ex=Explain()
        first=trip("entity-one",delay=600); first["tripUpdate"]["trip"]["tripId"]="same"
        second=trip("entity-two",delay=900); second["tripUpdate"]["trip"]["tripId"]="same"
        second["tripUpdate"]["stopTimeUpdate"][0]["stopSequence"]=2
        duplicate=envelope(entities=[first,second])
        p=self.pipeline(envelope(),duplicate,iter(["2033-05-18T03:33:20Z"]),explainer=ex)
        p.ingest(); self.assertEqual(ex.calls,1)
        p.clock=lambda:"2033-05-18T03:33:50Z"
        p.trip_client.fetcher=lambda *_a,**_k:json.dumps(envelope(entities=[trip("same",delay=1200)])).encode()
        p.ingest(); self.assertEqual(ex.calls,1)
        with self.db.connect() as con:
            row=con.execute("select deterministic_text,context_json,explanation_text,method,first_seen_at,last_seen_at from anomalies where kind='material_delay'").fetchone()
        self.assertIn("1200",row[0]); self.assertEqual(json.loads(row[1])["delay_seconds"],1200)
        self.assertEqual(row[2],row[0]); self.assertEqual(row[3],"deterministic-current")
        self.assertNotEqual(row[4],row[5])

    def test_live_waits_for_blocking_poller_without_server_shutdown_deadlock(self):
        entered=threading.Event(); release=threading.Event(); stop=threading.Event()
        class Poll:
            def ingest(self): entered.set(); release.wait(2)
        class Alerts:
            def ingest(self,with_ridership=False): pass
        server=make_server(self.db,0)
        worker=threading.Thread(target=run_live,args=(self.db,server),kwargs={"telemetry":Poll(),"alerts":Alerts(),"stop_event":stop,"telemetry_interval":.01})
        worker.start(); self.assertTrue(entered.wait(1)); stop.set(); server.shutdown()
        time.sleep(.05); self.assertTrue(worker.is_alive())
        release.set(); worker.join(2); self.assertFalse(worker.is_alive())


if __name__ == "__main__": unittest.main()
