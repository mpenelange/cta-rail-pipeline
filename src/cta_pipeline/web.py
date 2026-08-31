import json
from html import escape
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlparse
from .arrivals import ArrivalLookupError,ArrivalsClient,resolve_station,wants_arrivals
from .llm import LLMError,QuestionAnswerer

def home_page(snapshot,runs):
    documents=snapshot["documents"]
    cards="".join(f'<article><b>{escape(", ".join(d["lines"]) or "System")}</b><h3>{escape(d["headline"] or "Service alert")}</h3><p>{escape(d["description"] or "No description supplied.")}</p><small>Document {escape(d["source_id"])} · version {d["version"]}</small></article>' for d in documents)
    run_rows="".join(f'<tr><td>#{r["id"]}</td><td>{escape(r["status"])}</td><td>{r["items_seen"]}</td><td>{r["items_changed"]}</td><td>{escape(r["finished_at"])}</td></tr>' for r in runs)
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Ask the Pipeline</title><style>
:root{{--ink:#17212b;--muted:#63707c;--paper:#f4f1ea;--card:#fff;--red:#c62828}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:16px system-ui,sans-serif}}main{{max-width:1050px;margin:auto;padding:48px 24px}}h1{{font:700 clamp(2.6rem,7vw,5.5rem)/.95 Georgia,serif;max-width:800px;margin:12px 0 24px}}.label,article b{{color:var(--red);font-size:.75rem;letter-spacing:.12em;text-transform:uppercase}}.lede{{max-width:700px;color:var(--muted);font-size:1.15rem;line-height:1.6}}.ask{{margin:36px 0;padding:24px;background:var(--ink);color:white;border-radius:14px}}form{{display:flex;gap:10px}}input{{flex:1;padding:14px;border:0;border-radius:8px;font-size:1rem}}button{{padding:0 22px;border:0;border-radius:8px;background:#ef5350;font-weight:800}}#answer{{line-height:1.6;white-space:pre-wrap}}#sources{{color:#bbc4cc;font-size:.85rem}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}}article{{background:var(--card);padding:22px;border-radius:12px;border:1px solid #ddd8cf}}article h3{{margin:8px 0}}article p,small{{color:var(--muted)}}section{{margin-top:46px}}table{{width:100%;border-collapse:collapse;background:white}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #eee;font-size:.85rem}}@media(max-width:600px){{form{{display:block}}button,input{{width:100%;min-height:48px}}button{{margin-top:8px}}table{{display:block;overflow:auto}}}}
</style></head><body><main><div class="label">Example RAG ingestion pipeline</div><h1>Ask questions of live, processed data.</h1><p class="lede">CTA alerts are ingested, normalized, versioned, indexed, retrieved for each question, and supplied to an LLM as grounded context. CTA is the example; the pipeline is the point.</p><section class="ask"><form id="ask"><input id="question" maxlength="1000" placeholder="What is affecting the Red Line?" required><button>Ask</button></form><p id="answer">Snapshot: {escape(snapshot["as_of"] or "No successful ingestion yet")}</p><div id="sources"></div></section><section><div class="label">Corpus</div><h2>{len(documents)} active documents</h2><div class="grid">{cards or '<p>No active documents.</p>'}</div></section><section><div class="label">Pipeline observability</div><h2>Recent source runs</h2><table><tr><th>Run</th><th>Status</th><th>Seen</th><th>Changed</th><th>Finished</th></tr>{run_rows}</table></section></main><script>
const form=document.querySelector('#ask'),out=document.querySelector('#answer'),sources=document.querySelector('#sources');form.addEventListener('submit',async e=>{{e.preventDefault();out.textContent='Retrieving documents and asking the model…';sources.textContent='';try{{const response=await fetch('/api/ask',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{question:document.querySelector('#question').value}})}});const data=await response.json();if(!response.ok)throw new Error(data.error);out.textContent=data.answer;sources.textContent='Retrieved: '+(data.sources.join(', ')||'none')}}catch(error){{out.textContent=error.message||'Question unavailable'}}}});
</script></body></html>'''.encode()

def make_handler(pipeline,answerer=None,arrivals=None):
    answerer=answerer or QuestionAnswerer(); arrivals=arrivals or ArrivalsClient()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_args): pass
        def send(self,status,value,kind="application/json; charset=utf-8"):
            body=value if isinstance(value,bytes) else json.dumps(value).encode(); self.send_response(status); self.send_header("Content-Type",kind); self.send_header("Content-Length",str(len(body))); self.send_header("X-Content-Type-Options","nosniff"); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            path=urlparse(self.path).path
            if path=="/": self.send(200,home_page(pipeline.snapshot(),pipeline.runs()),"text/html; charset=utf-8")
            elif path=="/api/snapshot": self.send(200,pipeline.snapshot())
            elif path=="/api/runs": self.send(200,{"runs":pipeline.runs()})
            elif path=="/api/health": self.send(200,{"status":"ok"})
            else: self.send(404,{"error":"not found"})
        def do_POST(self):
            if urlparse(self.path).path!="/api/ask": self.send(404,{"error":"not found"}); return
            try:
                length=int(self.headers.get("Content-Length","0")); value=json.loads(self.rfile.read(length)) if self.headers.get_content_type()=="application/json" and 1<=length<=4096 else None
                question=value.get("question") if isinstance(value,dict) and set(value)=={"question"} else None
                if not isinstance(question,str) or not question.strip() or len(question)>1000: raise ValueError()
            except (ValueError,json.JSONDecodeError): self.send(400,{"error":"invalid question"}); return
            documents=pipeline.retrieve(question); snapshot=pipeline.snapshot()
            context={"source":snapshot["source"],"as_of":snapshot["as_of"],"documents":documents}
            try:
                if wants_arrivals(question): context["live_arrivals"]=arrivals.fetch(resolve_station(question))
            except ArrivalLookupError as error: self.send(200,{"answer":str(error),"sources":[]}); return
            try: self.send(200,{"answer":answerer.answer(question.strip(),context),"sources":[d["source_id"] for d in documents]})
            except LLMError as error: self.send(503,{"error":str(error)})
    return Handler

def make_server(pipeline,host="127.0.0.1",port=8001,answerer=None,arrivals=None): return ThreadingHTTPServer((host,port),make_handler(pipeline,answerer,arrivals))
