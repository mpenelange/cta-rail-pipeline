import unittest
from cta_pipeline.web import home_page

class WebTests(unittest.TestCase):
    def test_home_explains_rag_and_escapes_documents(self):
        snapshot={"as_of":"now","documents":[{"source_id":"<id>","headline":"<script>","description":"safe","lines":["Red"],"version":1}]}
        page=home_page(snapshot,[{"id":1,"status":"success","items_seen":1,"items_changed":1,"finished_at":"now"}]).decode()
        self.assertIn("retrieved for each question",page); self.assertIn("&lt;script&gt;",page); self.assertNotIn("<script></h3>",page)
