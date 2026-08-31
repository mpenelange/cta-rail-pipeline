import argparse,json,os
from pathlib import Path
from .config import load_dotenv
from .db import Database
from .pipeline import Pipeline
from .web import make_server

def parser():
    value=argparse.ArgumentParser(description="CTA-backed ingestion and RAG demo"); value.add_argument("--env-file",default=".env"); value.add_argument("--db")
    commands=value.add_subparsers(dest="command",required=True); commands.add_parser("ingest",help="fetch and index one source snapshot")
    serve=commands.add_parser("serve",help="ingest once and serve the website"); serve.add_argument("--host",default="127.0.0.1"); serve.add_argument("--port",type=int,default=8001); serve.add_argument("--skip-ingest",action="store_true")
    return value

def main(argv=None):
    args=parser().parse_args(argv)
    try: load_dotenv(args.env_file)
    except (OSError,UnicodeError,ValueError) as error: parser().error(str(error))
    database=Database(Path(args.db or os.getenv("CTA_DB_PATH","./data/rag.db"))); database.migrate(); pipeline=Pipeline(database)
    if args.command=="ingest": print(json.dumps(pipeline.ingest())); return 0
    if not args.skip_ingest:
        try: pipeline.ingest()
        except Exception as error: print(f"Initial ingestion failed: {error}")
    server=make_server(pipeline,args.host,args.port); print(f"Serving http://{args.host}:{server.server_address[1]}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0

if __name__=="__main__": raise SystemExit(main())
