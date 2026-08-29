import copy
import gzip
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from cta_pipeline.db import Database, MIGRATION, TELEMETRY_MIGRATION
from cta_pipeline.telemetry import (EmptyTripUpdatesClient, GTFSRealtimeClient,
                                    TelemetryError, TelemetryPipeline,
                                    TrainTrackerPositionsClient)
from cta_pipeline.server import make_server, query_telemetry


FIXTURE = Path(__file__).parent / "fixtures" / "traintracker_positions.json"


class TrainTrackerClientTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def client(self, document=None, **kwargs):
        seen = kwargs.pop("seen", None)
        payload = json.dumps(document if document is not None else self.document).encode()
        def fetch(request, timeout):
            if seen is not None:
                seen.append((request, timeout))
            return payload
        return TrainTrackerPositionsClient("placeholder-key", fetcher=fetch, **kwargs)

    def test_one_https_request_has_all_official_routes_and_maps_canonical_feed(self):
        seen = []
        raw, feed = self.client(seen=seen).fetch()
        self.assertEqual(len(seen), 1)
        request, timeout = seen[0]
        parsed = urlsplit(request.full_url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "lapi.transitchicago.com")
        self.assertEqual(parsed.path, "/api/1.0/ttpositions.aspx")
        self.assertEqual(query["rt"], ["red,blue,brn,g,org,p,pink,y"])
        self.assertEqual(query["outputType"], ["JSON"])
        self.assertNotIn("Pexp", request.full_url)
        self.assertEqual(timeout, 15.0)
        self.assertEqual(raw, json.dumps(self.document).encode())
        self.assertEqual(feed["header"]["timestamp"], 1788024896)
        entity = feed["entity"][0]
        self.assertEqual(entity["id"], "3:Red3:827")
        vehicle = entity["vehicle"]
        self.assertEqual(vehicle["trip"], {"tripId":"3:Red3:827", "routeId":"Red", "directionId":1})
        self.assertEqual(vehicle["stopId"], "30125")
        self.assertEqual(vehicle["position"], {"latitude":41.90383, "longitude":-87.63685})
        self.assertEqual(vehicle["vehicle"], {"id":"3:Red3:827", "label":"Howard"})
        self.assertEqual(vehicle["timestamp"], 1788024864)

    def test_empty_companion_uses_same_cycle_timestamp(self):
        positions = self.client()
        _, vehicle_feed = positions.fetch()
        raw, trip_feed = EmptyTripUpdatesClient(positions).fetch()
        self.assertEqual(raw, b"")
        self.assertEqual(trip_feed, {"header":{"gtfsRealtimeVersion":"2.0", "timestamp":vehicle_feed["header"]["timestamp"]}, "entity":[]})

    def test_rejects_malformed_or_ambiguous_documents_atomically(self):
        cases = []
        provider = copy.deepcopy(self.document); provider["ctatt"]["errCd"] = "7"; provider["ctatt"]["errNm"] = "denied https://host/?key=provider-secret"
        cases.append((provider, "TrainTrackerPositions error code 7"))
        duplicate_route = copy.deepcopy(self.document); duplicate_route["ctatt"]["route"][-1] = copy.deepcopy(duplicate_route["ctatt"]["route"][0])
        cases.append((duplicate_route, "duplicate TrainTracker route"))
        duplicate_train = copy.deepcopy(self.document); duplicate_train["ctatt"]["route"][0]["train"].append(copy.deepcopy(duplicate_train["ctatt"]["route"][0]["train"][0]))
        cases.append((duplicate_train, "duplicate resolved train identity"))
        malformed_time = copy.deepcopy(self.document); malformed_time["ctatt"]["tmst"] = "2026-11-01T01:30:00"
        cases.append((malformed_time, "invalid TrainTracker timestamp"))
        bad_coordinate = copy.deepcopy(self.document); bad_coordinate["ctatt"]["route"][0]["train"][0]["lat"] = "91"
        cases.append((bad_coordinate, "invalid latitude"))
        for document, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(TelemetryError, message) as caught:
                self.client(document).fetch()
            self.assertNotIn("provider-secret", str(caught.exception))

    def test_bounds_key_response_routes_trains_and_text(self):
        with self.assertRaisesRegex(TelemetryError, "CTA_API_KEY is required"):
            TrainTrackerPositionsClient("").fetch()
        with self.assertRaisesRegex(TelemetryError, "too large"):
            TrainTrackerPositionsClient("x", fetcher=lambda *_a, **_k: b"x" * 11,
                                        max_response_bytes=10).fetch()
        too_many_routes = copy.deepcopy(self.document)
        too_many_routes["ctatt"]["route"].append({"@name":"red", "train":[]})
        with self.assertRaises(TelemetryError): self.client(too_many_routes).fetch()
        huge_text = copy.deepcopy(self.document)
        huge_text["ctatt"]["route"][0]["train"][0]["destNm"] = "x" * 257
        with self.assertRaises(TelemetryError): self.client(huge_text).fetch()

    def test_rejects_undocumented_directions_and_control_bearing_persisted_text(self):
        for direction in (0, 2, 999, "999"):
            bad = copy.deepcopy(self.document)
            bad["ctatt"]["route"][0]["train"][0]["trDr"] = direction
            with self.subTest(trDr=direction), self.assertRaisesRegex(TelemetryError, "invalid direction"):
                self.client(bad).fetch()
        valid = copy.deepcopy(self.document)
        valid["ctatt"]["route"][0]["train"][0]["trDr"] = "5"
        self.assertEqual(self.client(valid).fetch()[1]["entity"][0]["vehicle"]["trip"]["directionId"], 5)

        marker = "DO-NOT-ECHO"
        for field in ("rn", "nextStpId", "destNm"):
            for control in ("\x00", "\x1f", "\x7f", "\u0085", "\u202e"):
                bad = copy.deepcopy(self.document)
                bad["ctatt"]["route"][0]["train"][0][field] = marker + control
                with self.subTest(field=field, control=ord(control)), self.assertRaises(TelemetryError) as caught:
                    self.client(bad).fetch()
                self.assertRegex(str(caught.exception), r"^invalid (train run|next stop|destination label)$")
                self.assertNotIn(marker, str(caught.exception))

    def test_is_dly_is_strict_atomic_and_preserved_through_migration_replay_and_api(self):
        legacy_path = Path(self.tmp.name) / "published.db" if hasattr(self, "tmp") else Path(
            tempfile.mkdtemp()) / "published.db"
        self.addCleanup(lambda: legacy_path.parent.exists() and __import__("shutil").rmtree(legacy_path.parent))
        with closing(sqlite3.connect(legacy_path)) as con:
            con.executescript(MIGRATION)
            con.executescript(TELEMETRY_MIGRATION)
            con.execute("insert into schema_migrations values(1,'old')")
            con.execute("insert into schema_migrations values(2,'old')")
            con.execute("insert into schema_migrations values(3,'old')")
            con.execute("insert into schema_migrations values(4,'old')")
            con.commit()
        db = Database(legacy_path); db.migrate(); db.migrate()
        with db.connect() as con:
            self.assertEqual([r[0] for r in con.execute("select version from schema_migrations order by version")], [1,2,3,4,5,6])
            self.assertIn("is_delayed", {r[1] for r in con.execute("pragma table_info(vehicle_state)")})
            self.assertIn("is_delayed", {r[1] for r in con.execute("pragma table_info(vehicle_observations)")})

        document = copy.deepcopy(self.document)
        document["ctatt"]["route"][0]["train"][0]["isDly"] = "1"
        positions = self.client(document)
        _, canonical = positions.fetch()
        self.assertEqual(canonical["entity"][0]["vehicle"]["isDelayed"], 1)
        result = TelemetryPipeline(db, positions, EmptyTripUpdatesClient(positions),
            clock=lambda:"2026-08-29T17:35:00Z", source="traintracker").ingest()
        with db.connect() as con:
            state = con.execute("select current_status,is_delayed from vehicle_state").fetchone()
            observation = con.execute("select current_status,is_delayed from vehicle_observations").fetchone()
            blob = con.execute("select canonical_json from telemetry_snapshots where feed_type='vehicle_positions'").fetchone()[0]
        self.assertEqual(tuple(state), ("IN_TRANSIT_TO", 1))
        self.assertEqual(tuple(observation), ("IN_TRANSIT_TO", 1))
        self.assertEqual(json.loads(gzip.decompress(blob))["entity"][0]["vehicle"]["isDelayed"], 1)
        server=make_server(db,0); thread=__import__("threading").Thread(target=server.serve_forever); thread.start()
        try:
            rows=json.loads(urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/vehicles").read())["vehicles"]
        finally:
            server.shutdown(); server.server_close(); thread.join()
        self.assertEqual(rows[0]["is_delayed"], 1)
        self.assertEqual(rows[0]["current_status"], "IN_TRANSIT_TO")

        before = {table: db.scalar(f"select count(*) from {table}") for table in
                  ("telemetry_runs","telemetry_snapshots","vehicle_state","vehicle_observations")}
        for malformed in (None, "", "2", 1, True):
            bad = copy.deepcopy(document)
            if malformed is None: bad["ctatt"]["route"][0]["train"][0].pop("isDly")
            else: bad["ctatt"]["route"][0]["train"][0]["isDly"] = malformed
            with self.subTest(isDly=malformed), self.assertRaisesRegex(TelemetryError, "invalid isDly"):
                bad_positions = self.client(bad)
                TelemetryPipeline(db, bad_positions, EmptyTripUpdatesClient(bad_positions),
                    clock=lambda:"2026-08-29T17:36:00Z", source="traintracker").ingest()
        after = {table: db.scalar(f"select count(*) from {table}") for table in before}
        self.assertEqual(after["telemetry_runs"], before["telemetry_runs"] + 5)
        self.assertEqual({k:v for k,v in after.items() if k != "telemetry_runs"},
                         {k:v for k,v in before.items() if k != "telemetry_runs"})
        self.assertEqual(result["vehicles"], 1)


class SourceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "db"); self.db.migrate()

    def test_default_selects_traintracker_or_optional_gtfs_without_key_leak(self):
        with patch.dict(os.environ, {"CTA_API_KEY":"train-key"}, clear=True):
            pipeline = TelemetryPipeline(self.db)
            self.assertIsInstance(pipeline.vehicle_client, TrainTrackerPositionsClient)
            self.assertIsInstance(pipeline.trip_client, EmptyTripUpdatesClient)
            self.assertEqual(pipeline.source, "traintracker")
        with patch.dict(os.environ, {"CTA_API_KEY":"train-key", "CTA_GTFS_API_KEY":"gtfs-key"}, clear=True):
            pipeline = TelemetryPipeline(self.db)
            self.assertIsInstance(pipeline.vehicle_client, GTFSRealtimeClient)
            self.assertIsInstance(pipeline.trip_client, GTFSRealtimeClient)
            self.assertEqual(pipeline.vehicle_client.api_key, "gtfs-key")
            self.assertEqual(pipeline.source, "gtfs-realtime")

    def test_valid_empty_traintracker_cycle_authoritatively_reconciles(self):
        payload = FIXTURE.read_bytes()
        positions = TrainTrackerPositionsClient("x", fetcher=lambda *_a, **_k: payload)
        first = TelemetryPipeline(self.db, positions, EmptyTripUpdatesClient(positions),
                                  clock=lambda:"2026-08-29T17:35:00Z", source="traintracker").ingest()
        self.assertEqual(first["vehicles"], 1)
        empty = json.loads(payload); empty["ctatt"]["route"][0]["train"] = []
        positions = TrainTrackerPositionsClient("x", fetcher=lambda *_a, **_k: json.dumps(empty).encode())
        second = TelemetryPipeline(self.db, positions, EmptyTripUpdatesClient(positions),
                                   clock=lambda:"2026-08-29T17:36:00Z", source="traintracker").ingest()
        self.assertEqual(second["vehicles"], 0)
        self.assertEqual(self.db.scalar("select count(*) from vehicle_state"), 0)
        self.assertEqual(second["predictions"], 0)

    def test_failure_audit_retains_only_generic_source_specific_detail(self):
        provider = json.loads(FIXTURE.read_bytes())
        provider["ctatt"]["errCd"] = "42"
        provider["ctatt"]["errNm"] = "provider-secret https://example/?key=secret"
        positions = TrainTrackerPositionsClient("client-secret", fetcher=lambda *_a, **_k: json.dumps(provider).encode())
        pipeline = TelemetryPipeline(self.db, positions, EmptyTripUpdatesClient(positions),
                                     clock=lambda:"2026-08-29T17:35:00Z", source="traintracker")
        with self.assertRaisesRegex(TelemetryError, "TrainTrackerPositions error code 42"):
            pipeline.ingest()
        with self.db.connect() as con:
            error = con.execute("select error from telemetry_runs order by id desc limit 1").fetchone()[0]
        self.assertEqual(error, "TelemetryError: TrainTrackerPositions error code 42")
        self.assertNotIn("secret", error)

    def test_api_and_dashboard_expose_active_source(self):
        payload = FIXTURE.read_bytes()
        positions = TrainTrackerPositionsClient("x", fetcher=lambda *_a, **_k: payload)
        TelemetryPipeline(self.db, positions, EmptyTripUpdatesClient(positions),
                          clock=lambda:"2026-08-29T17:35:00Z", source="traintracker").ingest()
        with patch.dict(os.environ, {"CTA_API_KEY":"x"}, clear=True):
            server=make_server(self.db,0); thread=__import__("threading").Thread(target=server.serve_forever); thread.start()
            try:
                base=f"http://127.0.0.1:{server.server_address[1]}"
                telemetry=json.loads(urlopen(base+"/api/telemetry").read())
                page=urlopen(base+"/").read().decode()
            finally:
                server.shutdown(); server.server_close(); thread.join()
        self.assertEqual(telemetry["source"],"traintracker")
        self.assertEqual(telemetry["active_vehicles"],1)
        self.assertIn('id="telemetry-source"',page)
        self.assertIn("d.source",page)

    def test_run_source_is_constrained_persisted_and_migrated_without_env_mislabeling(self):
        payload = FIXTURE.read_bytes()
        positions = TrainTrackerPositionsClient("x", fetcher=lambda *_a, **_k: payload)
        TelemetryPipeline(self.db, positions, EmptyTripUpdatesClient(positions),
            clock=lambda:"2026-08-29T17:35:00Z", source="traintracker").ingest()
        failing = GTFSRealtimeClient("VehiclePositions", "x",
            fetcher=lambda *_a, **_k: (_ for _ in ()).throw(OSError("secret-key")))
        with self.assertRaises(TelemetryError):
            TelemetryPipeline(self.db, failing, GTFSRealtimeClient("TripUpdates", "x"),
                clock=lambda:"2026-08-29T17:36:00Z", source="gtfs-realtime").ingest()
        with patch.dict(os.environ, {"CTA_GTFS_API_KEY":"later-key"}, clear=True):
            self.assertEqual(query_telemetry(self.db)["source"], "traintracker")
        with self.db.connect() as con:
            runs = [tuple(r) for r in con.execute("select status,source,error from telemetry_runs order by id")]
        self.assertEqual([(r[0],r[1]) for r in runs],
                         [("success","traintracker"),("failed","gtfs-realtime")])
        self.assertNotIn("secret", runs[-1][2])

        empty = json.loads(payload); empty["ctatt"]["route"][0]["train"] = []
        empty_positions = TrainTrackerPositionsClient("x", fetcher=lambda *_a, **_k: json.dumps(empty).encode())
        TelemetryPipeline(self.db, empty_positions, EmptyTripUpdatesClient(empty_positions),
            clock=lambda:"2026-08-29T17:37:00Z", source="traintracker").ingest()
        self.assertEqual(query_telemetry(self.db)["source"], "traintracker")
        self.assertEqual(query_telemetry(self.db)["active_vehicles"], 0)
        with self.assertRaisesRegex(ValueError, "telemetry source"):
            TelemetryPipeline(self.db, empty_positions, EmptyTripUpdatesClient(empty_positions), source="other")

        legacy_path = Path(self.tmp.name) / "pre-source.db"
        with closing(sqlite3.connect(legacy_path)) as con:
            con.executescript(MIGRATION); con.executescript(TELEMETRY_MIGRATION)
            con.execute("alter table vehicle_state add column stationary_since integer")
            con.execute("alter table vehicle_state add column is_delayed integer check(is_delayed in (0,1))")
            con.execute("alter table vehicle_observations add column is_delayed integer check(is_delayed in (0,1))")
            for version in range(1,6): con.execute("insert into schema_migrations values(?,'old')",(version,))
            con.execute("insert into telemetry_runs(started_at,finished_at,status) values('old','old','success')")
            con.commit()
        legacy = Database(legacy_path); legacy.migrate(); legacy.migrate()
        with legacy.connect() as con:
            self.assertEqual(con.execute("select source from telemetry_runs").fetchone()[0], "gtfs-realtime")
            self.assertEqual([r[0] for r in con.execute("select version from schema_migrations order by version")], [1,2,3,4,5,6])
            definition = con.execute("select sql from sqlite_master where name='telemetry_runs'").fetchone()[0]
        self.assertIn("CHECK", definition.upper())


class ConfigurationDocumentationTests(unittest.TestCase):
    def test_optional_gtfs_key_is_wired_and_source_limitations_documented(self):
        root=Path(__file__).parents[1]
        env=(root/".env.example").read_text(encoding="utf-8")
        compose=(root/"compose.yaml").read_text(encoding="utf-8")
        readme=(root/"README.md").read_text(encoding="utf-8")
        self.assertIn("CTA_GTFS_API_KEY=",env)
        self.assertIn("CTA_GTFS_API_KEY: ${CTA_GTFS_API_KEY:-}",compose)
        self.assertIn("Train Tracker",readme)
        self.assertIn("50,000",readme)
        self.assertIn("2,880",readme)
        self.assertIn("GTFS-only",readme)
        self.assertIn("CTA_GTFS_API_KEY",readme)


if __name__ == "__main__":
    unittest.main()
