import io,json,os,unittest
from unittest.mock import patch
from cta_pipeline.llm import LLMError,QuestionAnswerer

class LLMTests(unittest.TestCase):
    def test_sends_only_question_and_retrieved_context(self):
        captured={}
        def fetch(request,timeout):
            captured["body"]=json.loads(request.data); return io.BytesIO(json.dumps({"choices":[{"message":{"content":"The Red Line is delayed."}}]}).encode())
        with patch.dict(os.environ,{"OPENAI_API_KEY":"secret","OPENAI_MODEL":"demo"},clear=True):
            answer=QuestionAnswerer(fetch).answer("Red status?",{"documents":[{"source_id":"a1","headline":"Delay"}]})
        self.assertEqual(answer,"The Red Line is delayed."); encoded=json.dumps(captured["body"]); self.assertIn("a1",encoded); self.assertNotIn("secret",encoded)
    def test_requires_configuration(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(LLMError): QuestionAnswerer().answer("hello",{})
