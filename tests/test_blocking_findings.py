import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stderr
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from cta_pipeline.__main__ import main
from cta_pipeline.client import CTAAlertsClient, SourceError
from cta_pipeline.db import Database
from cta_pipeline.extract import LocalExtractor, OpenAIExtractor, validate_extraction
from cta_pipeline.normalize import PayloadError, normalize_payload
from cta_pipeline.pipeline import Pipeline
from cta_pipeline.ridership import RidershipClient
from cta_pipeline.server import make_server, query_alerts


def alert(source_id="a", description="first"):
    return {"source_id": source_id, "headline": "Delay", "description": description,
            "severity": "High", "start_time": "", "end_time": "", "lines": ["Red"],
            "stations": [], "station_ids": [], "major": False, "alert_url": ""}


class BoundedReadTests(unittest.TestCase):
    def test_all_network_clients_reject_oversized_streams(self):
        huge = b"x" * 129
        with self.assertRaisesRegex(SourceError, "too large"):
            CTAAlertsClient(lambda *_a, **_k: io.BytesIO(huge), max_response_bytes=128).fetch()
        with self.assertRaisesRegex(ValueError, "too large"):
            RidershipClient(lambda *_a, **_k: io.BytesIO(huge), max_response_bytes=128).fetch()
        old = os.environ.get("OPENAI_API_KEY"); os.environ["OPENAI_API_KEY"] = "test"
        try:
            result = OpenAIExtractor(lambda *_a, **_k: io.BytesIO(huge),
                                     max_response_bytes=128).extract(alert())
            self.assertEqual(result["method"], "local-fallback")
        finally:
            if old is None: os.environ.pop("OPENAI_API_KEY", None)
            else: os.environ["OPENAI_API_KEY"] = old

    def test_normalized_and_model_values_are_bounded(self):
        row = {"AlertId": "i" * 5000, "Headline": "h" * 20000,
               "Service": [{"Route": "R" * 1000} for _ in range(500)]}
        got = normalize_payload({"CTARailAlerts": {"Alert": [row]}})[0]
        self.assertLessEqual(len(got["source_id"]), 256)
        self.assertLessEqual(len(got["headline"]), 8000)
        self.assertLessEqual(len(got["lines"]), 100)
        candidate = LocalExtractor().extract({"headline": "h" * 50000,
                                               "description": "", "lines": ["x" * 1000] * 500,
                                               "stations": []})
        self.assertTrue(validate_extraction(candidate))
        self.assertLessEqual(len(candidate["summary"]), 180)
        self.assertLessEqual(len(candidate["affected_lines"]), 100)


class EnvelopeTests(unittest.TestCase):
    def test_rejects_malformed_and_error_envelopes(self):
        bad = [[], {"CTARailAlerts": {}}, {"CTAAlerts": {"ErrorCode": "12", "Alert": []}},
               {"unexpected": {"Alert": []}}, {"CTARailAlerts": {"Alert": {"x": 1}}}]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(PayloadError):
                normalize_payload(payload)

    def test_explicit_empty_alert_array_is_valid(self):
        self.assertEqual(normalize_payload({"CTARailAlerts": {"Alert": []}}), [])

    def test_rejects_malformed_members_missing_ids_and_duplicate_ids(self):
        bad_rows = [
            [42],
            [{"Headline": "No identifier"}],
            [{"AlertId": ""}],
            [{"AlertId": "   "}],
            [{"AlertId": "same"}, {"AlertID": "same"}],
        ]
        for rows in bad_rows:
            with self.subTest(rows=rows), self.assertRaises(PayloadError):
                normalize_payload({"CTAAlerts": {"ErrorCode": "0", "Alert": rows}})

    def test_invalid_snapshot_is_failed_and_preserves_active_alerts_atomically(self):
        bad_rows = [
            [42],
            [{"Headline": "No identifier"}],
            [{"AlertId": "   "}],
            [{"AlertId": "duplicate"}, {"Guid": "duplicate"}],
        ]
        for rows in bad_rows:
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as d:
                db = Database(Path(d) / "x.db"); db.migrate()
                Pipeline(db, extractor=LocalExtractor()).persist([alert("prior")], b"old")
                raw = json.dumps({"CTAAlerts": {"ErrorCode": "0", "Alert": rows}}).encode()
                client = CTAAlertsClient(lambda *_a, **_k: raw)

                with self.assertRaises(PayloadError):
                    Pipeline(db, client=client, extractor=LocalExtractor()).ingest()

                self.assertEqual(db.scalar("select count(*) from alerts"), 1)
                self.assertEqual(db.scalar("select count(*) from alert_versions"), 1)
                self.assertEqual(db.scalar("select is_active from alerts where source_id='prior'"), 1)
                self.assertEqual(db.scalar("select count(*) from raw_snapshots"), 1)
                self.assertEqual(db.scalar("select status from ingestion_runs order by id desc limit 1"), "failed")


class RevisionAndActivityTests(unittest.TestCase):
    def test_a_b_a_creates_three_versions(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "x.db"); db.migrate(); p = Pipeline(db, extractor=LocalExtractor())
            p.persist([alert(description="A")], b"1")
            p.persist([alert(description="B")], b"2")
            p.persist([alert(description="A")], b"3")
            self.assertEqual(db.scalar("select current_version from alerts where source_id='a'"), 3)
            self.assertEqual(db.scalar("select count(*) from alert_versions"), 3)

    def test_v1_unique_hash_database_upgrades_idempotently(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"; db = Database(path)
            with closing(sqlite3.connect(path)) as con:
                con.executescript("""
                CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                INSERT INTO schema_migrations VALUES(1,'old');
                CREATE TABLE ingestion_runs(id INTEGER PRIMARY KEY, source TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, error TEXT);
                CREATE TABLE raw_snapshots(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES ingestion_runs(id), content_hash TEXT NOT NULL, raw_document BLOB NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE alerts(id INTEGER PRIMARY KEY, source_id TEXT NOT NULL UNIQUE, current_hash TEXT NOT NULL, current_version INTEGER NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);
                CREATE TABLE alert_versions(id INTEGER PRIMARY KEY, alert_id INTEGER NOT NULL REFERENCES alerts(id), version INTEGER NOT NULL, content_hash TEXT NOT NULL, normalized_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(alert_id, version), UNIQUE(alert_id, content_hash));
                CREATE TABLE extractions(id INTEGER PRIMARY KEY, alert_version_id INTEGER NOT NULL UNIQUE REFERENCES alert_versions(id), method TEXT NOT NULL, model TEXT NOT NULL, confidence REAL NOT NULL, extraction_json TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE station_ridership(station_id TEXT PRIMARY KEY, station_name TEXT, latest_date TEXT NOT NULL, rides INTEGER NOT NULL, fetched_at TEXT NOT NULL);
                INSERT INTO ingestion_runs VALUES(1,'cta-alerts','old','old','success',NULL);
                INSERT INTO alerts VALUES(1,'a','hash',1,'old','old');
                INSERT INTO alert_versions VALUES(1,1,1,'hash','{}','old');
                INSERT INTO extractions VALUES(1,1,'local','v1',0.5,'{}','old');
                """)
            db.migrate(); db.migrate()
            self.assertEqual(db.scalar("select count(*) from schema_migrations"), 6)
            with db.connect() as con:
                cols = {r[1] for r in con.execute("pragma table_info(alerts)")}
            self.assertIn("is_active", cols)
            self.assertEqual(db.scalar("select count(*) from extractions"), 1)
            with db.connect() as con:
                self.assertEqual(con.execute("pragma foreign_key_check").fetchall(), [])

    def test_snapshot_deactivates_missing_and_reappearance_reactivates(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "x.db"); db.migrate(); p = Pipeline(db, extractor=LocalExtractor())
            p.persist([alert("a"), alert("b")], b"1")
            p.persist([alert("a")], b"2")
            self.assertEqual([x["source_id"] for x in query_alerts(db)], ["a"])
            self.assertEqual(db.scalar("select is_active from alerts where source_id='b'"), 0)
            p.persist([alert("a"), alert("b")], b"3")
            self.assertEqual({x["source_id"] for x in query_alerts(db)}, {"a", "b"})
            self.assertEqual(db.scalar("select count(*) from alert_versions where alert_id=(select id from alerts where source_id='b')"), 1)


class FailureAndTransactionTests(unittest.TestCase):
    def test_normalization_and_extraction_failures_are_audited_and_atomic(self):
        class Broken:
            def extract(self, _alert): raise RuntimeError("model exploded")
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "x.db"); db.migrate()
            Pipeline(db, extractor=LocalExtractor()).persist([alert("prior")], b"old")
            with self.assertRaisesRegex(RuntimeError, "model exploded"):
                Pipeline(db, extractor=Broken()).persist([alert("new")], b"new")
            self.assertEqual(db.scalar("select count(*) from alerts"), 1)
            self.assertEqual(db.scalar("select status from ingestion_runs order by id desc limit 1"), "failed")
            bad_client = CTAAlertsClient(lambda *_a, **_k: b'{"CTARailAlerts":{}}')
            with self.assertRaises(PayloadError): Pipeline(db, client=bad_client).ingest()
            self.assertEqual(db.scalar("select status from ingestion_runs order by id desc limit 1"), "failed")

    def test_extraction_happens_before_running_write_transaction(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "x.db"); db.migrate()
            class Inspector(LocalExtractor):
                def extract(self, item):
                    self.assert_no_running = db.scalar("select count(*) from ingestion_runs where status='running'")
                    return super().extract(item)
            extractor = Inspector(); Pipeline(db, extractor=extractor).persist([alert()], b"raw")
            self.assertEqual(extractor.assert_no_running, 0)

    def test_cli_catches_non_fetch_pipeline_error_as_json(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("CTA_DB_PATH"); os.environ["CTA_DB_PATH"] = str(Path(d) / "db")
            err = io.StringIO()
            try:
                from unittest.mock import patch
                with patch("cta_pipeline.__main__.Pipeline.ingest", side_effect=RuntimeError("boom")), redirect_stderr(err):
                    self.assertEqual(main(["ingest"]), 1)
                self.assertEqual(json.loads(err.getvalue())["status"], "error")
            finally:
                if old is None: os.environ.pop("CTA_DB_PATH", None)
                else: os.environ["CTA_DB_PATH"] = old


class SocrataSemanticsTests(unittest.TestCase):
    def test_queries_latest_date_then_all_rows_and_reports_metadata(self):
        seen = []
        def fetch(req, timeout):
            seen.append(req.full_url)
            if len(seen) == 1: return b'[{"date":"2026-08-25T00:00:00.000"}]'
            return b'[{"station_id":"1","stationname":"One","date":"2026-08-25T00:00:00.000","rides":"10"},{"station_id":"2","stationname":"Two","date":"2026-08-25T00:00:00.000","rides":"20"}]'
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "x.db"); db.migrate(); client = RidershipClient(fetch)
            self.assertEqual(client.refresh(db), 2)
            self.assertIn("max%28date%29", seen[0])
            self.assertIn("%24where=date", seen[1])
            self.assertEqual(client.last_refresh["service_date"], "2026-08-25T00:00:00.000")
            self.assertEqual(client.last_refresh["row_count"], 2)
            self.assertEqual(client.last_refresh["status"], "complete")
            self.assertEqual(db.scalar("select count(*) from station_ridership"), 2)

    def test_bad_schema_does_not_replace_cache(self):
        responses = iter([b'[{"date":"2026-08-25T00:00:00.000"}]', b'[{"station_id":"1"}]'])
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "x.db"); db.migrate()
            with db.connect() as con:
                con.execute("insert into station_ridership values('old','Old','2020',1,'then')")
            with self.assertRaisesRegex(ValueError, "schema"):
                RidershipClient(lambda *_a, **_k: next(responses)).refresh(db)
            self.assertEqual(db.scalar("select count(*) from station_ridership"), 1)

    def test_fetches_more_than_one_station_page(self):
        date = "2026-08-25T00:00:00.000"; calls = 0
        first = [{"station_id": str(i), "stationname": str(i), "date": date, "rides": "1"}
                 for i in range(1000)]
        def fetch(_req, timeout):
            nonlocal calls; calls += 1
            if calls == 1: return json.dumps([{"date": date}]).encode()
            if calls == 2: return json.dumps(first).encode()
            return json.dumps([{"station_id": "last", "stationname": "Last", "date": date, "rides": "2"}]).encode()
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "x.db"); db.migrate(); client = RidershipClient(fetch)
            self.assertEqual(client.refresh(db), 1001)
            self.assertEqual(calls, 3)
            self.assertEqual(db.scalar("select row_count from ridership_refresh_state"), 1001)


class APIBoundTests(unittest.TestCase):
    def test_rejects_oversized_query_and_identifier(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "x.db"); db.migrate()
            server = make_server(db, 0)
            thread = threading.Thread(target=server.serve_forever); thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                for url in (base + "/api/alerts?q=" + "x" * 600,
                            base + "/api/alerts/" + "x" * 300):
                    with self.assertRaises(HTTPError) as caught: urlopen(url)
                    self.assertEqual(caught.exception.code, 414)
            finally:
                server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__": unittest.main()
