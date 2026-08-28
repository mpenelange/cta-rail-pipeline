import gzip
import sqlite3
from pathlib import Path

MIGRATION = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ingestion_runs(id INTEGER PRIMARY KEY, source TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS raw_snapshots(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES ingestion_runs(id), content_hash TEXT NOT NULL, raw_document BLOB NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, source_id TEXT NOT NULL UNIQUE, current_hash TEXT NOT NULL, current_version INTEGER NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)));
CREATE TABLE IF NOT EXISTS alert_versions(id INTEGER PRIMARY KEY, alert_id INTEGER NOT NULL REFERENCES alerts(id), version INTEGER NOT NULL, content_hash TEXT NOT NULL, normalized_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(alert_id, version));
CREATE TABLE IF NOT EXISTS extractions(id INTEGER PRIMARY KEY, alert_version_id INTEGER NOT NULL UNIQUE REFERENCES alert_versions(id), method TEXT NOT NULL, model TEXT NOT NULL, confidence REAL NOT NULL, extraction_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS station_ridership(station_id TEXT PRIMARY KEY, station_name TEXT, latest_date TEXT NOT NULL, rides INTEGER NOT NULL, fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ridership_refresh_state(id INTEGER PRIMARY KEY CHECK(id=1), service_date TEXT NOT NULL, row_count INTEGER NOT NULL, status TEXT NOT NULL, fetched_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_alerts_last_seen ON alerts(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_versions_alert ON alert_versions(alert_id, version DESC);
"""

TELEMETRY_MIGRATION = """
CREATE TABLE IF NOT EXISTS telemetry_runs(id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, error TEXT, vehicle_feed_timestamp INTEGER, trip_feed_timestamp INTEGER, vehicles INTEGER NOT NULL DEFAULT 0, predictions INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS telemetry_snapshots(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES telemetry_runs(id), feed_type TEXT NOT NULL, content_hash TEXT NOT NULL, canonical_json BLOB NOT NULL, feed_timestamp INTEGER NOT NULL, created_at TEXT NOT NULL, UNIQUE(feed_type,content_hash));
CREATE TABLE IF NOT EXISTS vehicle_state(vehicle_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, route_id TEXT, direction_id INTEGER, trip_id TEXT, latitude REAL, longitude REAL, stop_id TEXT, current_status TEXT, vehicle_timestamp INTEGER, feed_timestamp INTEGER NOT NULL, observed_at TEXT NOT NULL, label TEXT);
CREATE TABLE IF NOT EXISTS vehicle_observations(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES telemetry_runs(id), vehicle_id TEXT NOT NULL, route_id TEXT, direction_id INTEGER, latitude REAL, longitude REAL, stop_id TEXT, current_status TEXT, vehicle_timestamp INTEGER, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trip_prediction_observations(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES telemetry_runs(id), trip_id TEXT NOT NULL, route_id TEXT, direction_id INTEGER, stop_id TEXT, stop_sequence INTEGER, arrival_time INTEGER, departure_time INTEGER, delay INTEGER, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS anomalies(id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, severity TEXT NOT NULL, entity_key TEXT NOT NULL, deterministic_text TEXT NOT NULL, context_json TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), explanation_text TEXT NOT NULL, method TEXT NOT NULL, model TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_telemetry_runs_finished ON telemetry_runs(finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_vehicle_obs_time ON vehicle_observations(observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_predictions_time ON trip_prediction_observations(observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_anomalies_active ON anomalies(active,last_seen_at DESC);
CREATE TABLE IF NOT EXISTS prediction_state(prediction_key TEXT PRIMARY KEY, signature TEXT NOT NULL);
"""


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback like sqlite3's context manager, then release the file."""
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


class Database:
    def __init__(self, path):
        self.path = Path(path)
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=10, factory=ClosingConnection)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=10000")
        return con
    def migrate(self):
        with self.connect() as con:
            con.executescript(MIGRATION)
            con.execute("INSERT OR IGNORE INTO schema_migrations VALUES(1, datetime('now'))")
        with self.connect() as con:
            columns = {row[1] for row in con.execute("PRAGMA table_info(alerts)")}
            if "is_active" not in columns:
                con.execute("ALTER TABLE alerts ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1))")
            indexes = list(con.execute("PRAGMA index_list(alert_versions)"))
            has_hash_unique = False
            for index in indexes:
                columns = [r[2] for r in con.execute("SELECT * FROM pragma_index_info(?)", (index[1],))]
                if index[2] and columns == ["alert_id", "content_hash"]:
                    has_hash_unique = True
            if has_hash_unique:
                con.execute("PRAGMA foreign_keys=OFF")
                con.executescript("""
                CREATE TABLE alert_versions_v2(id INTEGER PRIMARY KEY, alert_id INTEGER NOT NULL REFERENCES alerts(id), version INTEGER NOT NULL, content_hash TEXT NOT NULL, normalized_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(alert_id, version));
                INSERT INTO alert_versions_v2 SELECT * FROM alert_versions;
                DROP TABLE alert_versions;
                ALTER TABLE alert_versions_v2 RENAME TO alert_versions;
                CREATE INDEX IF NOT EXISTS idx_versions_alert ON alert_versions(alert_id, version DESC);
                """)
                con.execute("PRAGMA foreign_keys=ON")
            con.execute("INSERT OR IGNORE INTO schema_migrations VALUES(2, datetime('now'))")
        with self.connect() as con:
            con.executescript(TELEMETRY_MIGRATION)
            con.execute("INSERT OR IGNORE INTO schema_migrations VALUES(3, datetime('now'))")
            columns = {row[1] for row in con.execute("PRAGMA table_info(vehicle_state)")}
            if "stationary_since" not in columns:
                con.execute("ALTER TABLE vehicle_state ADD COLUMN stationary_since INTEGER")
            for row in con.execute("SELECT id,canonical_json FROM telemetry_snapshots"):
                raw=row[1].encode() if isinstance(row[1],str) else bytes(row[1])
                if not raw.startswith(b"\x1f\x8b"):
                    con.execute("UPDATE telemetry_snapshots SET canonical_json=? WHERE id=?",
                                (gzip.compress(raw,compresslevel=9,mtime=0),row[0]))
            con.execute("INSERT OR IGNORE INTO schema_migrations VALUES(4, datetime('now'))")
    def scalar(self, sql, params=()):
        with self.connect() as con:
            row = con.execute(sql, params).fetchone()
            return row[0] if row else None
