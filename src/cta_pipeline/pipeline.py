import hashlib
import json
import re
from datetime import datetime,timezone
from .client import CTAAlertsClient
from .normalize import canonical_bytes,normalize_payload

def utc_now(): return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

def searchable_text(alert):
    fields=(alert.get("headline",""),alert.get("description","")," ".join(alert.get("lines",[]))," ".join(alert.get("stations",[])),alert.get("severity",""))
    return " ".join(fields)

class Pipeline:
    """Fetch, normalize, version, and retrieve source documents."""
    def __init__(self,database,client=None,clock=utc_now): self.database=database; self.client=client or CTAAlertsClient(); self.clock=clock
    def ingest(self):
        started=self.clock()
        try:
            raw,payload=self.client.fetch(); return self._store(raw,normalize_payload(payload),started)
        except Exception as error:
            with self.database.connect() as connection: connection.execute("INSERT INTO source_runs(started_at,finished_at,status,error) VALUES(?,?,?,?)",(started,self.clock(),"failed",str(error)[:500]))
            raise
    def _store(self,raw,alerts,started):
        observed=self.clock(); source_hash=hashlib.sha256(raw).hexdigest(); prepared=[]
        for alert in alerts:
            encoded=canonical_bytes(alert); prepared.append((alert["source_id"],hashlib.sha256(encoded).hexdigest(),encoded.decode(),searchable_text(alert)))
        changed=0
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE"); connection.execute("UPDATE documents SET active=0"); connection.execute("DELETE FROM document_search")
            for source_id,digest,document,search_text in prepared:
                current=connection.execute("SELECT content_hash,version FROM documents WHERE source_id=?",(source_id,)).fetchone()
                if current and current["content_hash"]==digest:
                    connection.execute("UPDATE documents SET active=1,last_seen_at=? WHERE source_id=?",(observed,source_id)); version=current["version"]
                else:
                    changed+=1; version=current["version"]+1 if current else 1
                    if current: connection.execute("UPDATE documents SET content_hash=?,document=?,search_text=?,last_seen_at=?,version=?,active=1 WHERE source_id=?",(digest,document,search_text,observed,version,source_id))
                    else: connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,1)",(source_id,digest,document,search_text,observed,observed,version))
                    connection.execute("INSERT INTO document_versions(source_id,version,content_hash,document,observed_at) VALUES(?,?,?,?,?)",(source_id,version,digest,document,observed))
                connection.execute("INSERT INTO document_search(source_id,search_text) VALUES(?,?)",(source_id,search_text))
            run_id=connection.execute("INSERT INTO source_runs(started_at,finished_at,status,source_hash,items_seen,items_changed) VALUES(?,?,?,?,?,?)",(started,observed,"success",source_hash,len(prepared),changed)).lastrowid
        return {"run_id":run_id,"items_seen":len(prepared),"items_changed":changed,"source_hash":source_hash}
    def retrieve(self,question,limit=6):
        terms=[]
        for term in re.findall(r"[A-Za-z0-9]+",question.lower()):
            if len(term)>2 and term not in {"the","and","for","are","what","with","about","status"} and term not in terms: terms.append(term)
        with self.database.connect() as connection:
            if terms:
                query=" OR ".join(f'"{term}"' for term in terms[:12])
                rows=connection.execute("SELECT d.* FROM document_search s JOIN documents d ON d.source_id=s.source_id WHERE document_search MATCH ? AND d.active=1 ORDER BY bm25(document_search) LIMIT ?",(query,limit)).fetchall()
            else: rows=[]
            if not rows: rows=connection.execute("SELECT * FROM documents WHERE active=1 ORDER BY last_seen_at DESC LIMIT ?",(limit,)).fetchall()
        return [dict(json.loads(row["document"]),version=row["version"],last_seen_at=row["last_seen_at"]) for row in rows]
    def snapshot(self):
        with self.database.connect() as connection:
            rows=connection.execute("SELECT * FROM documents WHERE active=1 ORDER BY source_id").fetchall(); run=connection.execute("SELECT * FROM source_runs ORDER BY id DESC LIMIT 1").fetchone()
        return {"source":"CTA Customer Alerts API","as_of":run["finished_at"] if run and run["status"]=="success" else None,"documents":[dict(json.loads(row["document"]),version=row["version"]) for row in rows]}
    def runs(self,limit=10):
        with self.database.connect() as connection: return [dict(row) for row in connection.execute("SELECT * FROM source_runs ORDER BY id DESC LIMIT ?",(limit,))]
