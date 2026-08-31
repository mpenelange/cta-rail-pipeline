import unittest
from cta_pipeline.questions import QuestionService

class FakePipeline: pass
class FakeAnswerer:
    def answer(self,question,context): self.context=context; return "Grounded answer"
class FakeRegistry:
    def neighborhood(self,question,stations,routes,freshness_requested=False):
        self.call=(question,stations,routes,freshness_requested)
        return {"neighborhood":True},["GTFS"]

class QuestionServiceTests(unittest.TestCase):
    def setUp(self):
        self.answerer=FakeAnswerer(); self.registry=FakeRegistry()
        self.service=QuestionService(FakePipeline(),self.answerer,self.registry)
    def test_station_service_uses_entity_neighborhood(self):
        result=self.service.ask("what trains can I take at Clark and Lake?")
        self.assertEqual(result["type"],"answer"); self.assertEqual(self.registry.call[1][0]["map_id"],"40380")
        self.assertIn("evidence",self.answerer.context)
    def test_ambiguous_station_returns_catalog_choices(self):
        result=self.service.ask("next five trains arriving at Monroe")
        self.assertEqual(result["field"],"station_0"); self.assertEqual(len(result["options"]),2)
        self.assertFalse(hasattr(self.registry,"call"))
    def test_selection_retrieves_live_neighborhood(self):
        result=self.service.ask("next five trains arriving at Monroe",{"station_0":"40790"})
        self.assertEqual(result["type"],"answer"); self.assertTrue(self.registry.call[3])
        self.assertEqual(self.registry.call[1][0]["name"],"Monroe (Blue)")
    def test_trip_question_clarifies_each_catalog_entity(self):
        question="what train from Western to 35th Street?"
        first=self.service.ask(question); self.assertEqual(first["field"],"station_0")
        second=self.service.ask(question,{"station_0":"40670"}); self.assertEqual(second["field"],"station_1")
        result=self.service.ask(question,{"station_0":"40670","station_1":"40120"})
        self.assertEqual(result["type"],"answer"); self.assertEqual(len(self.registry.call[1]),2)
    def test_route_list_and_broad_questions_share_the_same_engine(self):
        result=self.service.ask("make me a list of every Red Line stop")
        self.assertEqual(result["type"],"answer"); self.assertEqual(self.registry.call[2][0]["name"],"Red Line")
        result=self.service.ask("is rail service disrupted?")
        self.assertEqual(result["type"],"answer"); self.assertEqual(self.registry.call[1:3],([],[]))
