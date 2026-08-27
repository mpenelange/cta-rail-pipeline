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
    def scalar(self, sql, params=()):
        with self.connect() as con:
            row = con.execute(sql, params).fetchone()
            return row[0] if row else None
