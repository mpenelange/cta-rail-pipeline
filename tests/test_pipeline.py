import json, os, tempfile, threading, unittest
from pathlib import Path
from urllib.request import urlopen

from cta_pipeline.client import CTAAlertsClient
from cta_pipeline.normalize import normalize_payload
from cta_pipeline.extract import LocalExtractor, validate_extraction, OpenAIExtractor
from cta_pipeline.db import Database
from cta_pipeline.pipeline import Pipeline
from cta_pipeline.server import make_server

FIXTURE = Path(__file__).parent / 'fixtures' / 'alerts.json'

class NormalizeTests(unittest.TestCase):
    def test_normalizes_single_alert_html_cdata_and_service_object(self):
        data = {"CTARailAlerts": {"Alert": {"AlertId":"7", "Headline":"<![CDATA[<b>Delays &amp; closures</b>]]>", "ShortDescription":"A\r\nB", "Severity":"High", "Service":{"Route":"Red", "StopName":"95th"}}}}
        alert = normalize_payload(data)[0]
        self.assertEqual(alert['source_id'], '7')
        self.assertEqual(alert['headline'], 'Delays & closures')
        self.assertEqual(alert['description'], 'A\nB')
        self.assertEqual(alert['lines'], ['Red'])
        self.assertEqual(alert['stations'], ['95th'])
    def test_normalizes_absent_and_lists(self):
        # CTA documents an explicit empty Alert array; a missing field is malformed.
        self.assertEqual(normalize_payload({'CTARailAlerts': {'Alert': []}}), [])
        got = normalize_payload({'CTARailAlerts': {'Alert': [{'AlertId':'1','Service':[{'Route':'Blue'}]}, {'AlertId':'2'}]}})
        self.assertEqual([x['source_id'] for x in got], ['1','2'])
        self.assertEqual(got[0]['lines'], ['Blue'])

class ExtractionTests(unittest.TestCase):
    def test_local_extraction_is_structured(self):
        result = LocalExtractor().extract({'headline':'Planned elevator maintenance', 'description':'Red Line trains will bypass Grand. Use accessible station at Chicago.', 'lines':['Red'], 'stations':['Grand']})
        self.assertTrue(result['planned'])
        self.assertEqual(result['event_type'], 'maintenance')
        self.assertIn('Grand', result['affected_stations'])
        self.assertIn('bypass', result['effects'].lower())
        self.assertTrue(validate_extraction(result))
    def test_openai_invalid_response_falls_back(self):
        e = OpenAIExtractor(fetcher=lambda *a, **k: b'{"bad":true}')
        r = e.extract({'headline':'Delay','description':'Signal problem','lines':['Blue'],'stations':[]})
        self.assertEqual(r['event_type'], 'delay')

class PersistenceTests(unittest.TestCase):
    def test_idempotence_and_version_on_normalized_change(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d)/'x.db'); db.migrate()
            p = Pipeline(db, extractor=LocalExtractor())
            alert = {'source_id':'1','headline':'Delay','description':'Signal issue','severity':'High','start_time':'','end_time':'','lines':['Red'],'stations':['Clark']}
            a = p.persist([alert], b'raw one')
            b = p.persist([alert], b'raw two')
            alert['description'] = 'Signal issue resolved soon'
            c = p.persist([alert], b'raw three')
            self.assertEqual(a['new_versions'], 1); self.assertEqual(b['new_versions'], 0); self.assertEqual(c['new_versions'], 1)
            self.assertEqual(db.scalar('select count(*) from raw_snapshots'), 3)
            self.assertEqual(db.scalar('select count(*) from alert_versions'), 2)
            self.assertEqual(db.scalar('select count(*) from extractions'), 2)

class E2ETests(unittest.TestCase):
    def test_mocked_client_pipeline_and_http_api_dashboard(self):
        raw = FIXTURE.read_bytes()
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d)/'x.db'); db.migrate()
            client = CTAAlertsClient(fetcher=lambda req, timeout: raw)
            outcome = Pipeline(db, client=client).ingest()
            self.assertEqual(outcome['alerts_seen'], 2)
            httpd = make_server(db, 0)
            t = threading.Thread(target=httpd.serve_forever, daemon=True); t.start()
            base = 'http://127.0.0.1:%s' % httpd.server_address[1]
            try:
                self.assertEqual(json.loads(urlopen(base+'/api/health').read())['status'], 'ok')
                alerts = json.loads(urlopen(base+'/api/alerts').read())['alerts']
                self.assertEqual(len(alerts), 2)
                self.assertIn(b'CTA Rail Disruption Intelligence', urlopen(base+'/').read())
                self.assertIn(b'alert', urlopen(base+'/api/alerts/'+str(alerts[0]['id'])).read())
            finally: httpd.shutdown(); httpd.server_close()

if __name__ == '__main__': unittest.main()
