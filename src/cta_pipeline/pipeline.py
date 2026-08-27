import hashlib
import json
from datetime import datetime, timezone

from .client import CTAAlertsClient
from .extract import LocalExtractor, OpenAIExtractor
from .normalize import bound_normalized_alert, canonical_bytes, normalize_payload
from .extract import validate_extraction
from .limits import MAX_ERROR, MAX_RAW_SNAPSHOT_BYTES


def now(): return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Pipeline:
    def __init__(self, db, client=None, extractor=None, clock=now):
        self.db = db; self.client = client or CTAAlertsClient(); self.clock = clock
        self.extractor = extractor or (OpenAIExtractor() if __import__('os').getenv('OPENAI_API_KEY') else LocalExtractor())
    def ingest(self, with_ridership=False, ridership_client=None):
        try:
            raw, document = self.client.fetch()
            alerts = normalize_payload(document)
            outcome = self.persist(alerts, raw)
        except Exception as exc:
            if not getattr(exc, "_cta_audited", False):
                self._audit_failure(exc)
            raise
        if with_ridership and ridership_client:
            try:
                outcome["ridership_rows"] = ridership_client.refresh(self.db)
                if ridership_client.last_refresh:
                    outcome["ridership_refresh"] = ridership_client.last_refresh
            except Exception as exc: outcome["ridership_warning"] = str(exc)
        return outcome
    def _audit_failure(self, exc, stamp=None):
        stamp = stamp or self.clock()
        try:
            with self.db.connect() as con:
                con.execute("INSERT INTO ingestion_runs(source,started_at,finished_at,status,error) VALUES(?,?,?,?,?)",
                            ("cta-alerts", stamp, stamp, "failed", str(exc)[:MAX_ERROR]))
            try: exc._cta_audited = True
            except Exception: pass
        except Exception:
            # The original failure remains the useful error if SQLite itself is unavailable.
            pass
    def persist(self, alerts, raw):
        stamp = self.clock()
        try:
            if not isinstance(raw, bytes) or len(raw) > MAX_RAW_SNAPSHOT_BYTES:
                raise ValueError(f"raw snapshot too large (limit {MAX_RAW_SNAPSHOT_BYTES} bytes)")
            if not isinstance(alerts, list): raise ValueError("normalized alerts must be a list")
            alerts = [bound_normalized_alert(alert) for alert in alerts]
            if len(alerts) > 5000: raise ValueError("too many normalized alerts")
            source_ids = [alert["source_id"] for alert in alerts]
            if len(source_ids) != len(set(source_ids)): raise ValueError("duplicate CTA alert source_id")
            prepared = []
            with self.db.connect() as con:
                hashes = {r["source_id"]: r["current_hash"] for r in con.execute("SELECT source_id,current_hash FROM alerts")}
            for alert in alerts:
                encoded = canonical_bytes(alert); digest = hashlib.sha256(encoded).hexdigest()
                extraction = None
                if hashes.get(alert["source_id"]) != digest:
                    extraction = self.extractor.extract(alert)
                    if not validate_extraction(extraction): raise ValueError("extraction schema validation failed")
                prepared.append((alert, encoded, digest, extraction))
            raw_hash = hashlib.sha256(raw).hexdigest(); new_versions = 0
            with self.db.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                cur = con.execute("INSERT INTO ingestion_runs(source,started_at,status) VALUES(?,?,?)", ("cta-alerts", stamp, "running"))
                run_id = cur.lastrowid
                con.execute("INSERT INTO raw_snapshots(run_id,content_hash,raw_document,created_at) VALUES(?,?,?,?)", (run_id, raw_hash, raw, stamp))
                con.execute("UPDATE alerts SET is_active=0 WHERE is_active=1")
                for alert, encoded, digest, extraction in prepared:
                    current = con.execute("SELECT * FROM alerts WHERE source_id=?", (alert["source_id"],)).fetchone()
                    if current and current["current_hash"] == digest:
                        con.execute("UPDATE alerts SET last_seen_at=?,is_active=1 WHERE id=?", (stamp, current["id"]))
                        continue
                    if extraction is None:
                        raise RuntimeError("alert changed during extraction preparation")
                    if current:
                        alert_id, version = current["id"], current["current_version"] + 1
                        con.execute("UPDATE alerts SET current_hash=?,current_version=?,last_seen_at=?,is_active=1 WHERE id=?", (digest, version, stamp, alert_id))
                    else:
                        cur = con.execute("INSERT INTO alerts(source_id,current_hash,current_version,first_seen_at,last_seen_at,is_active) VALUES(?,?,?,?,?,1)", (alert["source_id"], digest, 1, stamp, stamp))
                        alert_id, version = cur.lastrowid, 1
                    cur = con.execute("INSERT INTO alert_versions(alert_id,version,content_hash,normalized_json,created_at) VALUES(?,?,?,?,?)", (alert_id, version, digest, encoded.decode(), stamp))
                    con.execute("INSERT INTO extractions(alert_version_id,method,model,confidence,extraction_json,created_at) VALUES(?,?,?,?,?,?)", (cur.lastrowid, str(extraction.get("method","local"))[:64], str(extraction.get("model","deterministic-v1"))[:256], float(extraction["confidence"]), json.dumps(extraction, sort_keys=True), stamp))
                    new_versions += 1
                con.execute("UPDATE ingestion_runs SET finished_at=?,status=? WHERE id=?", (stamp, "success", run_id))
            return {"run_id": run_id, "alerts_seen": len(alerts), "new_versions": new_versions, "snapshot_hash": raw_hash}
        except Exception as exc:
            self._audit_failure(exc, stamp)
            raise
