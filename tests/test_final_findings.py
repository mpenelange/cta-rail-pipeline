import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cta_pipeline.db import Database
from cta_pipeline.telemetry import GTFSRealtimeClient, TelemetryError, TelemetryPipeline
from tests.test_telemetry import NOW, envelope, trip


class FinalBlockingFindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "cta.db"); self.db.migrate()

    def pipeline(self, trips, stamp="2033-05-18T03:33:20Z"):
        vraw = json.dumps(envelope()).encode(); traw = json.dumps(trips).encode()
        return TelemetryPipeline(
            self.db,
            GTFSRealtimeClient("VehiclePositions", "x", lambda *_a, **_k: vraw),
            GTFSRealtimeClient("TripUpdates", "x", lambda *_a, **_k: traw),
            clock=lambda: stamp, stale_seconds=10**9)

    def test_prediction_state_reconciles_rotating_empty_and_failed_feeds(self):
        p = self.pipeline(envelope(entities=[trip("one", delay=0)])); p.ingest()
        self.assertEqual(self.db.scalar("select count(*) from prediction_state"), 1)
        p.trip_client.fetcher = lambda *_a, **_k: json.dumps(
            envelope(NOW + 1, [trip("two", delay=0)])).encode()
        p.ingest()
        with self.db.connect() as con:
            keys = [r[0] for r in con.execute("select prediction_key from prediction_state")]
        self.assertEqual(len(keys), 1); self.assertIn("two", keys[0])
        p.trip_client.fetcher = lambda *_a, **_k: json.dumps(envelope(NOW + 2)).encode()
        p.ingest(); self.assertEqual(self.db.scalar("select count(*) from prediction_state"), 0)
        p.trip_client.fetcher = lambda *_a, **_k: b"not-json"
        with self.assertRaises(TelemetryError): p.ingest()
        self.assertEqual(self.db.scalar("select count(*) from prediction_state"), 0)

    def test_old_identical_content_is_reassociated_with_latest_success(self):
        p = self.pipeline(envelope(), "2033-05-18T03:33:20Z"); first = p.ingest()
        p.clock = lambda: "2033-05-19T03:33:21Z"; latest = p.ingest()
        self.assertEqual(self.db.scalar("select count(*) from telemetry_snapshots"), 2)
        with self.db.connect() as con:
            rows = list(con.execute("select run_id,canonical_json,created_at from telemetry_snapshots"))
        self.assertEqual({r[0] for r in rows}, {latest["run_id"]})
        self.assertNotEqual(first["run_id"], latest["run_id"])
        self.assertTrue(all(r[2] == "2033-05-19T03:33:21Z" for r in rows))
        self.assertTrue(all(json.loads(gzip.decompress(r[1]))["entity"] == [] for r in rows))

    def test_continuous_failures_are_capped_without_orphaning_snapshots(self):
        p = self.pipeline(envelope()); success = p.ingest()
        p.vehicle_client.fetcher = lambda *_a, **_k: (_ for _ in ()).throw(OSError("secret"))
        with patch("cta_pipeline.telemetry.MAX_RUN_ROWS", 3):
            for _ in range(8):
                with self.assertRaises(TelemetryError): p.ingest()
        self.assertLessEqual(self.db.scalar("select count(*) from telemetry_runs"), 3)
        self.assertEqual(self.db.scalar(
            "select count(*) from telemetry_snapshots where run_id=?", (success["run_id"],)), 2)

    def test_inactive_anomalies_retire_and_oversized_set_rolls_back(self):
        p = self.pipeline(envelope(entities=[trip("late", delay=600)])); p.ingest()
        p.clock = lambda: "2033-05-20T03:33:20Z"
        p.trip_client.fetcher = lambda *_a, **_k: json.dumps(envelope(NOW + 1)).encode()
        p.ingest(); self.assertEqual(self.db.scalar("select count(*) from anomalies"), 0)
        before_runs = self.db.scalar("select count(*) from telemetry_runs")
        many = [trip(f"t{i}", delay=600, stop=f"s{i}") for i in range(3)]
        p.trip_client.fetcher = lambda *_a, **_k: json.dumps(envelope(NOW + 2, many)).encode()
        with patch("cta_pipeline.telemetry.MAX_ANOMALY_ROWS", 2):
            with self.assertRaisesRegex(TelemetryError, "anomalies"):
                p.ingest()
        self.assertEqual(self.db.scalar("select count(*) from anomalies"), 0)
        self.assertEqual(self.db.scalar("select count(*) from telemetry_runs"), before_runs + 1)

    def test_migration_and_all_history_caps_are_idempotent(self):
        self.db.migrate(); self.db.migrate()
        p = self.pipeline(envelope())
        with patch.multiple("cta_pipeline.telemetry", MAX_SNAPSHOT_ROWS=2,
                            MAX_RUN_ROWS=3, MAX_VEHICLE_OBSERVATION_ROWS=2,
                            MAX_TRIP_OBSERVATION_ROWS=2, MAX_ANOMALY_ROWS=2):
            for i in range(6):
                p.clock = lambda i=i: f"2033-05-18T03:3{i}:20Z"
                p.trip_client.fetcher = lambda *_a, i=i, **_k: json.dumps(
                    envelope(NOW + i, [trip(f"t{i}", delay=0)])).encode()
                p.ingest()
        for table, limit in (("telemetry_snapshots", 2), ("telemetry_runs", 3),
                             ("vehicle_observations", 2),
                             ("trip_prediction_observations", 2), ("anomalies", 2)):
            self.assertLessEqual(self.db.scalar(f"select count(*) from {table}"), limit)


if __name__ == "__main__": unittest.main()
