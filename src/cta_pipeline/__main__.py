import argparse,json,os
from pathlib import Path
from .config import load_dotenv
from .db import Database
from .pipeline import Pipeline
from .questions import QuestionService
from .tui import run_tui

def parser():
    value=argparse.ArgumentParser(description="CTA-backed ingestion and RAG demo"); value.add_argument("--env-file",default=".env"); value.add_argument("--db")
    commands=value.add_subparsers(dest="command"); commands.add_parser("ingest",help="fetch and index one source snapshot")
    tui=commands.add_parser("tui",help="ingest once and open the terminal interface"); tui.add_argument("--skip-ingest",action="store_true")
    return value

def main(argv=None):
    args=parser().parse_args(argv)
    try: load_dotenv(args.env_file)
    except (OSError,UnicodeError,ValueError) as error: parser().error(str(error))
    database=Database(Path(args.db or os.getenv("CTA_DB_PATH","./data/rag.db"))); database.migrate(); pipeline=Pipeline(database)
    if args.command=="ingest": print(json.dumps(pipeline.ingest())); return 0
    if not getattr(args,"skip_ingest",False):
        try: pipeline.ingest()
        except Exception as error: print(f"Initial ingestion failed: {error}")
    run_tui(QuestionService(pipeline))
    return 0

if __name__=="__main__": raise SystemExit(main())
