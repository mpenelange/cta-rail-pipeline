import unittest
from cta_pipeline.tui import TUI,_question_lines

class FakeService:
    def ask(self,question,selections=None):
        if not selections: return {"type":"clarification","field":"station_0","question":"Which station?","options":[{"id":"1","label":"Blue"}]}
        self.selections=selections; return {"type":"answer","answer":"Five minutes","sources":["live"]}

class TUITests(unittest.TestCase):
    def test_question_wraps_with_a_hanging_prompt(self):
        self.assertEqual(_question_lines("one two three four",12),["> one two ","  three four"])
    def test_submit_preserves_question_across_clarification(self):
        tui=TUI(FakeService()); tui.question="next train at Monroe"; tui.submit()
        self.assertEqual(tui.result["type"],"clarification")
        tui.submit("1"); self.assertEqual(tui.result["answer"],"Five minutes"); self.assertEqual(tui.question,"next train at Monroe")
        self.assertEqual(tui.service.selections,{"station_0":"1"})
    def test_reset_clears_the_complete_interaction(self):
        tui=TUI(FakeService()); tui.question="old question"; tui.result={"type":"answer"}; tui.selections={"station_0":"1"}; tui.selection=3
        tui.reset()
        self.assertEqual((tui.question,tui.result,tui.selections,tui.selection),("",None,{},0))
