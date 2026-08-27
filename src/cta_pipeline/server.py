import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
from .limits import MAX_API_QUERY, MAX_ID

LINE_COLORS = {"Red":"#c60c30","Blue":"#00a1de","Brn":"#62361b","G":"#009b3a","Org":"#f9461c","P":"#522398","Pexp":"#522398","Pink":"#e27ea6","Y":"#f9e300"}

CSS = """
:root{color-scheme:dark;--bg:#0b0f14;--panel:#121820;--line:#26313d;--muted:#95a4b5;--text:#edf3f8;--accent:#55b5e8}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,-apple-system,sans-serif}header{position:sticky;top:0;z-index:2;background:#0d131aee;backdrop-filter:blur(10px);border-bottom:1px solid var(--line);padding:16px max(18px,calc((100% - 1180px)/2));display:flex;justify-content:space-between;align-items:center}.brand{font-size:18px;font-weight:750}.brand span{color:var(--accent)}.health{color:#8cdaa5;font-size:12px}.wrap{max-width:1180px;margin:auto;padding:22px 18px}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.kpi,.card,.filters{background:var(--panel);border:1px solid var(--line);border-radius:8px}.kpi{padding:14px}.kpi b{display:block;font-size:25px}.kpi span,.meta{color:var(--muted);font-size:12px}.filters{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0;padding:12px}input,select{background:#0b1016;color:var(--text);border:1px solid #344252;border-radius:6px;padding:9px;min-width:140px}.alerts{display:grid;gap:10px}.card{padding:16px;border-left:4px solid #718096}.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.linechip,.sev{font-size:11px;font-weight:750;padding:4px 7px;border-radius:4px}.sev{background:#78350f;color:#fed7aa}.headline{font-size:16px;margin:10px 0 5px}.summary{line-height:1.5;color:#d6e0e8}.detail{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;border-top:1px solid var(--line);margin-top:12px;padding-top:12px}.detail b{display:block;font-size:11px;text-transform:uppercase;color:var(--muted);margin-bottom:4px}.empty{text-align:center;padding:50px;color:var(--muted)}button{cursor:pointer}.method{margin-left:auto;color:var(--muted);font-size:11px}@media(max-width:700px){.kpis{grid-template-columns:1fr 1fr}.detail{grid-template-columns:1fr}.method{margin-left:0}.filters>*{flex:1}.wrap{padding:14px 10px}}
"""
JS = """
const esc=s=>String(s??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
let all=[]; const el=id=>document.getElementById(id);
function render(){let q=el('q').value.toLowerCase(),line=el('line').value,sev=el('severity').value,plan=el('planned').value;let rows=all.filter(a=>(!line||a.lines.includes(line))&&(!sev||a.severity_label===sev)&&(!plan||String(a.extraction.planned)===plan)&&JSON.stringify(a).toLowerCase().includes(q));el('shown').textContent=rows.length;el('cards').innerHTML=rows.length?rows.map(a=>`<article class="card" style="border-left-color:${esc(a.line_colors[0]||'#718096')}"><div class="row"><span class="sev">${esc(a.severity_label)}</span>${a.lines.map((x,i)=>`<span class="linechip" style="background:${esc(a.line_colors[i])};color:${x==='Y'?'#111':'#fff'}">${esc(x)}</span>`).join('')}<span class="method">${esc(a.extraction.method)} · ${Math.round(a.extraction.confidence*100)}% · rev ${a.revision_count}</span></div><h2 class="headline">${esc(a.headline)}</h2><div class="summary">${esc(a.extraction.summary)}</div><div class="detail"><div><b>Cause & effect</b>${esc(a.extraction.cause)} · ${esc(a.extraction.effects)}</div><div><b>Recommended action</b>${esc(a.extraction.actions)}</div><div><b>Stations / exposure</b>${esc(a.stations.join(', ')||'Not specified')}${a.estimated_exposure!=null?' · ~'+a.estimated_exposure.toLocaleString()+' latest daily rides':''}</div></div><div class="meta" style="margin-top:10px">${esc(a.start_time||'Start not stated')} · ${esc(a.extraction.accessibility_impact)}</div></article>`).join(''):'<div class="empty">No disruptions match these filters.</div>'}
fetch('/api/alerts').then(r=>r.json()).then(d=>{all=d.alerts;el('total').textContent=all.length;el('major').textContent=all.filter(x=>['Critical','Major'].includes(x.severity_label)).length;el('revisions').textContent=all.reduce((n,x)=>n+x.revision_count,0);[...new Set(all.flatMap(x=>x.lines))].sort().forEach(x=>el('line').insertAdjacentHTML('beforeend',`<option>${esc(x)}</option>`));render()}).catch(()=>el('cards').innerHTML='<div class="empty">Dashboard data is unavailable.</div>');document.querySelectorAll('input,select').forEach(x=>x.addEventListener('input',render));
"""


def _severity(raw, major=False):
    low = str(raw).lower()
    try: score = int(float(raw))
    except ValueError: score = 0
    if major or score >= 80 or "critical" in low: return "Critical"
    if score >= 50 or "high" in low or "major" in low: return "Major"
    if score >= 20 or "medium" in low or "minor" in low: return "Minor"
    return "Advisory"


def query_alerts(db):
    sql = """SELECT a.id,a.source_id,a.current_version,a.first_seen_at,a.last_seen_at,v.normalized_json,e.extraction_json,e.method,e.model,e.confidence FROM alerts a JOIN alert_versions v ON v.alert_id=a.id AND v.version=a.current_version JOIN extractions e ON e.alert_version_id=v.id WHERE a.is_active=1 ORDER BY CASE json_extract(v.normalized_json,'$.major') WHEN 1 THEN 0 ELSE 1 END,a.last_seen_at DESC,a.id"""
    with db.connect() as con:
        rows = con.execute(sql).fetchall(); rides = {r["station_id"]:r["rides"] for r in con.execute("SELECT station_id,rides FROM station_ridership")}
    result=[]
    for row in rows:
        a=json.loads(row["normalized_json"]); ex=json.loads(row["extraction_json"])
        matched=[rides[s] for s in a.get("station_ids",[]) if s in rides]
        result.append({"id":row["id"],"source_id":row["source_id"],**a,"severity_label":_severity(a["severity"],a.get("major")),"line_colors":[LINE_COLORS.get(x,"#718096") for x in a["lines"]],"extraction":ex,"revision_count":row["current_version"],"first_seen_at":row["first_seen_at"],"last_seen_at":row["last_seen_at"],"estimated_exposure":sum(matched) if matched else None})
    return result


def dashboard():
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>CTA Rail Disruption Intelligence</title><style>{CSS}</style></head><body><header><div class="brand"><span>CTA</span> Rail Disruption Intelligence</div><div class="health">● Local pipeline</div></header><main class="wrap"><section class="kpis"><div class="kpi"><b id="total">—</b><span>Current alerts</span></div><div class="kpi"><b id="major">—</b><span>Major / critical</span></div><div class="kpi"><b id="shown">—</b><span>Matching filters</span></div><div class="kpi"><b id="revisions">—</b><span>Total revisions</span></div></section><section class="filters" aria-label="Alert filters"><input id="q" type="search" placeholder="Search alerts or stations" aria-label="Search"><select id="line" aria-label="Line"><option value="">All lines</option></select><select id="severity" aria-label="Severity"><option value="">All severity</option><option>Critical</option><option>Major</option><option>Minor</option><option>Advisory</option></select><select id="planned" aria-label="Planned status"><option value="">Planned + unplanned</option><option value="true">Planned</option><option value="false">Unplanned</option></select></section><section id="cards" class="alerts"><div class="empty">Loading current disruptions…</div></section></main><script>{JS}</script></body></html>'''.encode()


def make_handler(db):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args): pass
        def send(self, status, body, content_type="application/json; charset=utf-8"):
            if isinstance(body, (dict,list)): body=json.dumps(body,ensure_ascii=False).encode()
            self.send_response(status); self.send_header("Content-Type",content_type); self.send_header("Content-Length",str(len(body))); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Content-Security-Policy","default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'"); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            path=urlparse(self.path).path
            if path in ("/api/health","/api/health/"):
                try: db.scalar("SELECT 1"); self.send(200,{"status":"ok","database":"ok"})
                except Exception: self.send(503,{"status":"degraded","database":"error"})
            elif path=="/api/alerts":
                raw_query=urlparse(self.path).query
                if len(raw_query)>MAX_API_QUERY: self.send(414,{"error":"query too long"}); return
                rows=query_alerts(db); query=parse_qs(raw_query)
                line=query.get("line",[""])[0]; severity=query.get("severity",[""])[0]
                planned=query.get("planned",[""])[0].lower(); search=query.get("q",[""])[0].lower()
                if line: rows=[x for x in rows if line in x["lines"]]
                if severity: rows=[x for x in rows if x["severity_label"]==severity]
                if planned in ("true","false"): rows=[x for x in rows if x["extraction"]["planned"]==(planned=="true")]
                if search: rows=[x for x in rows if search in json.dumps(x,ensure_ascii=False).lower()]
                with db.connect() as con:
                    state=con.execute("SELECT service_date,row_count,status,fetched_at FROM ridership_refresh_state WHERE id=1").fetchone()
                refresh=dict(state) if state else None
                self.send(200,{"alerts":rows,"count":len(rows),"ridership_refresh":refresh})
            elif path.startswith("/api/alerts/"):
                ident=unquote(path.rsplit('/',1)[-1])
                if len(ident)>MAX_ID: self.send(414,{"error":"identifier too long"}); return
                rows=query_alerts(db); item=next((x for x in rows if str(x["id"])==ident or x["source_id"]==ident),None)
                self.send(200,{"alert":item}) if item else self.send(404,{"error":"alert not found"})
            elif path=="/": self.send(200,dashboard(),"text/html; charset=utf-8")
            else: self.send(404,{"error":"not found"})
    return Handler


def make_server(db, port=8000, host="127.0.0.1"):
    return ThreadingHTTPServer((host,port),make_handler(db))
