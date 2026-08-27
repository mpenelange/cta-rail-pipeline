import argparse
import json
import os
import sys
from pathlib import Path

from .client import CTAAlertsClient, SourceError
from .db import Database
from .demo import DEMO_PAYLOAD, DEMO_RIDERSHIP, DEMO_RIDERSHIP_DATE
from .pipeline import Pipeline
from .ridership import RidershipClient
from .server import make_server


def db_path(): return Path(os.getenv("CTA_DB_PATH", "./data/cta.db"))


def parser():
    p=argparse.ArgumentParser(prog="cta_pipeline",description="CTA Rail Disruption Intelligence Pipeline")
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("init-db",help="create or migrate the SQLite database")
    ingest=sub.add_parser("ingest",help="fetch and ingest live CTA alerts")
    ingest.add_argument("--with-ridership",action="store_true",help="also refresh latest station ridership")
    serve=sub.add_parser("serve",help="serve the dashboard and JSON API")
    serve.add_argument("--host",default="127.0.0.1"); serve.add_argument("--port",type=int,default=8000)
    sub.add_parser("demo",help="load realistic offline demo fixtures")
    run=sub.add_parser("run",help="ingest once, then serve")
    run.add_argument("--with-ridership",action="store_true"); run.add_argument("--host",default="127.0.0.1"); run.add_argument("--port",type=int,default=8000)
    return p


def emit(value, stream=sys.stdout): print(json.dumps(value,sort_keys=True),file=stream)


def main(argv=None):
    args=parser().parse_args(argv); db=Database(db_path()); db.migrate()
    if args.command=="init-db": emit({"database":str(db.path),"status":"ready"}); return 0
    if args.command=="demo":
        client=CTAAlertsClient(lambda *_a,**_k: DEMO_PAYLOAD)
        demo_rows=iter((DEMO_RIDERSHIP_DATE,DEMO_RIDERSHIP))
        ridership=RidershipClient(lambda *_a,**_k: next(demo_rows))
        result=Pipeline(db,client=client).ingest(with_ridership=True,ridership_client=ridership)
        emit({"database":str(db.path),"mode":"demo",**result}); return 0
    if args.command in ("ingest","run"):
        try: result=Pipeline(db).ingest(with_ridership=args.with_ridership,ridership_client=RidershipClient() if args.with_ridership else None)
        except Exception as exc: emit({"status":"error","error":str(exc)[:1000]},sys.stderr); return 1
        emit(result)
        if args.command=="ingest": return 0
    server=make_server(db,args.port,args.host)
    emit({"status":"serving","host":args.host,"port":server.server_address[1],"database":str(db.path)})
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0


if __name__=="__main__": raise SystemExit(main())
