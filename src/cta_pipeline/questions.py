from .entities import EntityResolver
from .llm import QuestionAnswerer
from .retrievers import RetrieverRegistry

class QuestionService:
    """Resolve catalog entities, retrieve their evidence neighborhood, and answer."""
    def __init__(self,pipeline,answerer=None,registry=None,resolver=None):
        self.pipeline=pipeline; self.answerer=answerer or QuestionAnswerer(); self.registry=registry or RetrieverRegistry(pipeline); self.resolver=resolver or EntityResolver()
    def ask(self,question,selections=None):
        question=question.strip()
        if not question or len(question)>1000: raise ValueError("Enter a question using 1 to 1,000 characters.")
        selections=selections or {}; analysis=self.resolver.analyze(question); stations=[]
        for index,mention in enumerate(analysis["stations"]):
            field=f"station_{index}"; selected=selections.get(field)
            if len(mention["candidates"])>1 and selected is None: return self.resolver.clarification(index,mention)
            station=next((row for row in mention["candidates"] if row["map_id"]==(selected or mention["candidates"][0]["map_id"])),None)
            if station is None: raise ValueError("That station is not one of the available choices.")
            stations.append(station)
        evidence,sources=self.registry.neighborhood(question,stations,analysis["routes"],analysis["freshness_requested"])
        entities={"stations":[{"map_id":row["map_id"],"name":row["name"]} for row in stations],"routes":analysis["routes"]}
        return {"type":"answer","answer":self.answerer.answer(question,{"entities":entities,"evidence":evidence}),"sources":sources}
