import json,tempfile,unittest
from pathlib import Path
from cta_pipeline.db import Database
from cta_pipeline.pipeline import Pipeline

def payload(headline="Red Line delays"):
    value={"CTAAlerts":{"ErrorCode":"0","Alert":[{"AlertId":"a1","Headline":headline,"ShortDescription":"Signal problem at Belmont","SeverityScore":"50","Service":{"Service":[{"Route":"Red"},{"StopName":"Belmont","StopId":"41320"}]}}]}}
    return json.dumps(value).encode(),value

class FakeClient:
    def __init__(self,value): self.value=value
    def fetch(self): return self.value

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.db=Database(Path(self.temp.name)/"test.db"); self.db.migrate()
    def test_ingestion_is_idempotent_and_versions_changes(self):
        clock=iter(("t1","t2","t3","t4","t5","t6")).__next__; pipeline=Pipeline(self.db,FakeClient(payload()),clock)
        self.assertEqual(pipeline.ingest()["items_changed"],1)
        self.assertEqual(pipeline.ingest()["items_changed"],0)
        pipeline.client=FakeClient(payload("Red Line service restored")); self.assertEqual(pipeline.ingest()["items_changed"],1)
        document=pipeline.snapshot()["documents"][0]; self.assertEqual(document["version"],2); self.assertIn("restored",document["headline"])
    def test_retrieval_returns_relevant_active_documents(self):
        Pipeline(self.db,FakeClient(payload()),lambda:"now").ingest()
        documents=Pipeline(self.db).retrieve("What is happening on the Red Line?")
        self.assertEqual([item["source_id"] for item in documents],["a1"])
