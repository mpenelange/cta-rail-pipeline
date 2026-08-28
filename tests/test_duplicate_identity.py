import json
import tempfile
import unittest
from pathlib import Path

from cta_pipeline.db import Database
from cta_pipeline.telemetry import (GTFSRealtimeClient, TelemetryError,
                                    TelemetryPipeline, canonical_feed)
from tests.test_telemetry import NOW, envelope, trip, vehicle


class DuplicateIdentityTests(unittest.TestCase):
    protected_tables = (
        "telemetry_snapshots", "vehicle_state", "prediction_state",
        "vehicle_observations", "trip_prediction_observations", "anomalies",
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "cta.db"); self.db.migrate()

    def pipeline(self, vehicles, trips, stamp="2033-05-18T03:33:20Z"):
        vraw = json.dumps(vehicles).encode(); traw = json.dumps(trips).encode()
        return TelemetryPipeline(
            self.db,
            GTFSRealtimeClient("VehiclePositions", "api-key-must-not-leak",
                               lambda *_a, **_k: vraw),
            GTFSRealtimeClient("TripUpdates", "api-key-must-not-leak",
                               lambda *_a, **_k: traw),
            clock=lambda: stamp, stale_seconds=10**9)

    def snapshot(self):
        with self.db.connect() as con:
            return {table: [tuple(row) for row in con.execute(
                f"select * from {table} order by rowid")] for table in self.protected_tables}

    def test_canonical_feed_rejects_duplicate_entity_ids_for_both_feeds(self):
        cases = (
            ("VehiclePositions", [vehicle("duplicate", "one"),
                                  vehicle("duplicate", "two")]),
            ("TripUpdates", [trip("duplicate"), trip("duplicate")]),
        )
        for feed_name, entities in cases:
            with self.subTest(feed_name=feed_name), self.assertRaisesRegex(
                    TelemetryError, "duplicate .* entity id"):
                canonical_feed(feed_name, envelope(entities=entities))

    def test_duplicate_resolved_vehicle_id_fails_atomically_and_is_redacted(self):
        self.pipeline(envelope(entities=[vehicle()]),
                      envelope(entities=[trip(delay=0)])).ingest()
        before = self.snapshot()
        prior_success = self.db.scalar(
            "select max(id) from telemetry_runs where status='success'")
        malformed = envelope(NOW + 1, [
            vehicle("entity-one", "resolved-duplicate"),
            vehicle("entity-two", "resolved-duplicate", lat=42.0),
        ])
        with self.assertRaisesRegex(TelemetryError, "duplicate resolved vehicle identity"):
            self.pipeline(malformed, envelope(NOW + 1),
                          "2033-05-18T03:33:21Z").ingest()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.db.scalar(
            "select max(id) from telemetry_runs where status='success'"), prior_success)
        with self.db.connect() as con:
            failed = con.execute(
                "select status,error,vehicles,predictions from telemetry_runs "
                "order by id desc limit 1").fetchone()
        self.assertEqual(tuple(failed[:1]), ("failed",))
        self.assertEqual(tuple(failed[2:]), (0, 0))
        self.assertNotIn("api-key-must-not-leak", failed[1])
        self.assertNotIn("resolved-duplicate", failed[1])

    def test_duplicate_prediction_key_fails_atomically_but_loop_sequences_are_distinct(self):
        looping = trip("loop", delay=0, stop="same-stop")
        looping["tripUpdate"]["stopTimeUpdate"].append({
            "stopSequence": 2, "stopId": "same-stop",
            "arrival": {"delay": 0, "time": str(NOW + 1200)}})
        result = self.pipeline(envelope(entities=[
            vehicle("vehicle-one", "resolved-one"),
            vehicle("vehicle-two", "resolved-two")]),
            envelope(entities=[looping])).ingest()
        self.assertEqual((result["vehicles"], result["predictions"]), (2, 2))
        self.assertEqual(self.db.scalar("select count(*) from prediction_state"), 2)

        before = self.snapshot()
        duplicate = trip("entity-one", delay=600, stop="duplicate-stop")
        other = trip("entity-two", delay=900, stop="duplicate-stop")
        other["tripUpdate"]["trip"]["tripId"] = "entity-one"
        with self.assertRaisesRegex(TelemetryError, "duplicate prediction identity"):
            self.pipeline(envelope(NOW + 1), envelope(NOW + 1, [duplicate, other]),
                          "2033-05-18T03:33:21Z").ingest()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.db.scalar(
            "select count(*) from telemetry_runs where status='failed'"), 1)
        with self.db.connect() as con:
            error = con.execute("select error from telemetry_runs where status='failed'").fetchone()[0]
        self.assertNotIn("api-key-must-not-leak", error)
        self.assertNotIn("duplicate-stop", error)


if __name__ == "__main__": unittest.main()
