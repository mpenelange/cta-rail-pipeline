import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.request import urlopen

from cta_pipeline.__main__ import main
from cta_pipeline.client import CTAAlertsClient, SourceError
from cta_pipeline.db import Database
from cta_pipeline.pipeline import Pipeline
from cta_pipeline.ridership import RidershipClient
from cta_pipeline.server import make_server
from cta_pipeline.extract import OpenAIExtractor


class ClientTests(unittest.TestCase):
    def test_request_has_user_agent_timeout_and_clear_invalid_json_error(self):
        seen = {}
        def fake(req, timeout):
            seen.update(timeout=timeout, ua=req.headers.get('User-agent'), url=req.full_url)
            return b'not-json'
        with self.assertRaisesRegex(SourceError, 'CTA alerts fetch failed'):
            CTAAlertsClient(fake, timeout=4).fetch()
        self.assertEqual(seen['timeout'], 4)
        self.assertIn('cta-rail-pipeline', seen['ua'])
        self.assertIn('routeid=Red,Blue,Brn,G,Org,P,Pexp,Pink,Y', seen['url'])


class MigrationAndFailureTests(unittest.TestCase):
    def test_migration_is_idempotent_and_foreign_keys_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database(Path(d)/'db.sqlite'); db.migrate(); db.migrate()
            self.assertEqual(db.scalar('select count(*) from schema_migrations'), 6)
            with db.connect() as con: self.assertEqual(con.execute('pragma foreign_keys').fetchone()[0], 1)
    def test_failed_ingest_is_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database(Path(d)/'db.sqlite'); db.migrate()
            p=Pipeline(db, client=CTAAlertsClient(lambda *_a,**_k: b'bad'))
            with self.assertRaises(SourceError): p.ingest()
            self.assertEqual(db.scalar("select status from ingestion_runs order by id desc limit 1"), 'failed')


class RidershipTests(unittest.TestCase):
    def test_latest_station_rides_upsert_and_attach_to_alert(self):
        date=json.dumps([{'date':'2026-08-26T00:00:00'}]).encode()
        raw=json.dumps([{'station_id':'40170','stationname':'Howard','date':'2026-08-26T00:00:00','rides':'12,345'},{'station_id':'40170','stationname':'Howard','date':'2026-08-25T00:00:00','rides':'999'}]).encode()
        with tempfile.TemporaryDirectory() as d:
            db=Database(Path(d)/'db.sqlite'); db.migrate()
            # The refresh first resolves the authoritative latest service date.
            rows=json.dumps([{'station_id':'40170','stationname':'Howard','date':'2026-08-26T00:00:00','rides':'12,345'}]).encode()
            responses=iter((date,rows))
            self.assertEqual(RidershipClient(lambda *_a,**_k: next(responses)).refresh(db),1)
            alert={'source_id':'x','headline':'Delay','description':'Signal delay','severity':'High','start_time':'','end_time':'','lines':['Red'],'stations':['Howard'],'station_ids':['40170'],'major':False,'alert_url':''}
            Pipeline(db).persist([alert],b'{}')
            from cta_pipeline.server import query_alerts
            self.assertEqual(query_alerts(db)[0]['estimated_exposure'],12345)


class HTTPTests(unittest.TestCase):
    def test_api_server_404_and_dashboard_does_not_embed_alert_html(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database(Path(d)/'db.sqlite'); db.migrate()
            alert={'source_id':'unsafe','headline':'<script>alert(1)</script>','description':'Delay','severity':'High','start_time':'','end_time':'','lines':['Red'],'stations':[],'station_ids':[],'major':False,'alert_url':''}
            Pipeline(db).persist([alert],b'{}')
            server=make_server(db,0); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            base=f'http://127.0.0.1:{server.server_address[1]}'
            try:
                page=urlopen(base+'/').read().decode()
                self.assertNotIn('<script>alert(1)</script>',page)
                with self.assertRaises(Exception) as cm: urlopen(base+'/api/alerts/nope')
                self.assertEqual(cm.exception.code,404)
            finally: server.shutdown(); server.server_close()

    def test_api_filters_line_severity_planned_and_search(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database(Path(d)/'db.sqlite'); db.migrate()
            alerts=[{'source_id':'r','headline':'Planned work','description':'Planned track maintenance','severity':'High','start_time':'','end_time':'','lines':['Red'],'stations':['Lake'],'station_ids':[],'major':False,'alert_url':''},{'source_id':'b','headline':'Blue delays','description':'Signal delay','severity':'Low','start_time':'','end_time':'','lines':['Blue'],'stations':[],'station_ids':[],'major':False,'alert_url':''}]
            Pipeline(db).persist(alerts,b'{}'); server=make_server(db,0); threading.Thread(target=server.serve_forever,daemon=True).start(); base=f'http://127.0.0.1:{server.server_address[1]}'
            try:
                for query in ('line=Red','severity=Major','planned=true','q=Lake'):
                    data=json.loads(urlopen(base+'/api/alerts?'+query).read())
                    self.assertEqual([x['source_id'] for x in data['alerts']],['r'])
            finally: server.shutdown(); server.server_close()


class LLMTests(unittest.TestCase):
    def test_schema_response_accepted_and_secret_not_sent_in_body(self):
        candidate={'summary':'Blue Line delays from a signal issue.','planned':False,'cause':'signal','effects':'Delays','actions':'Allow extra time.','affected_lines':['Blue'],'affected_stations':[],'accessibility_impact':'No impact stated.','event_type':'delay','confidence':0.9}
        response=json.dumps({'choices':[{'message':{'content':json.dumps(candidate)}}]}).encode(); seen={}
        def fake(req,timeout): seen['auth']=req.headers['Authorization']; seen['body']=req.data; return response
        old=os.environ.get('OPENAI_API_KEY'); os.environ['OPENAI_API_KEY']='super-secret-test-key'
        try:
            result=OpenAIExtractor(fake).extract({'headline':'Delay','description':'Signal','lines':['Blue'],'stations':[]})
            self.assertEqual(result['method'],'openai'); self.assertEqual(result['model'],os.getenv('OPENAI_MODEL','gpt-5-mini'))
            self.assertNotIn(b'super-secret-test-key',seen['body']); self.assertIn('Bearer super-secret-test-key',seen['auth'])
        finally:
            if old is None: os.environ.pop('OPENAI_API_KEY',None)
            else: os.environ['OPENAI_API_KEY']=old


class CLITests(unittest.TestCase):
    def test_demo_and_init_db_emit_json_and_demo_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'demo.db'; out=io.StringIO()
            old=os.environ.get('CTA_DB_PATH'); os.environ['CTA_DB_PATH']=str(path)
            try:
                with redirect_stdout(out): self.assertEqual(main(['init-db']),0)
                with redirect_stdout(out): self.assertEqual(main(['demo']),0)
                with redirect_stdout(out): self.assertEqual(main(['demo']),0)
                self.assertEqual(Database(path).scalar('select count(*) from alerts'),2)
                for line in out.getvalue().splitlines(): self.assertIsInstance(json.loads(line),dict)
            finally:
                if old is None: os.environ.pop('CTA_DB_PATH',None)
                else: os.environ['CTA_DB_PATH']=old


if __name__ == '__main__': unittest.main()
