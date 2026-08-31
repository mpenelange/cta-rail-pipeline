import sqlite3
from pathlib import Path

SCHEMA="""
CREATE TABLE IF NOT EXISTS source_runs(id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT NOT NULL,status TEXT NOT NULL,source_hash TEXT,items_seen INTEGER NOT NULL DEFAULT 0,items_changed INTEGER NOT NULL DEFAULT 0,error TEXT);
CREATE TABLE IF NOT EXISTS documents(source_id TEXT PRIMARY KEY,content_hash TEXT NOT NULL,document TEXT NOT NULL,search_text TEXT NOT NULL,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,version INTEGER NOT NULL,active INTEGER NOT NULL CHECK(active IN (0,1)));
CREATE TABLE IF NOT EXISTS document_versions(id INTEGER PRIMARY KEY,source_id TEXT NOT NULL REFERENCES documents(source_id),version INTEGER NOT NULL,content_hash TEXT NOT NULL,document TEXT NOT NULL,observed_at TEXT NOT NULL,UNIQUE(source_id,version));
CREATE VIRTUAL TABLE IF NOT EXISTS document_search USING fts5(source_id UNINDEXED,search_text);
"""

class Database:
    def __init__(self,path): self.path=Path(path)
    def connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True); connection=sqlite3.connect(self.path,timeout=10); connection.row_factory=sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON"); connection.execute("PRAGMA journal_mode=WAL"); return connection
    def migrate(self):
        with self.connect() as connection: connection.executescript(SCHEMA)
