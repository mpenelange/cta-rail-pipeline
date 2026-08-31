# Ask the Pipeline

A deliberately small example of an ingestion + RAG pipeline. CTA service alerts are
the sample live source; the architecture is intended to be easy to reuse and explain.

```text
CTA API → normalize → versioned SQLite documents → FTS5 retrieval → LLM → website
```

Every ingestion run records its outcome. Changed alerts create immutable document
versions; unchanged alerts do not. A question is tokenized into a bounded SQLite
full-text query, and only the retrieved active documents are sent to an
OpenAI-compatible model. The website displays both the corpus and retrieved source IDs.

## Run locally

Install [uv](https://docs.astral.sh/uv/). The application has no third-party runtime
dependencies; uv creates the environment and installs the local package automatically.

```bash
cp .env.example .env
# Add your OPENAI_API_KEY to .env
uv run cta-pipeline serve
```

Alternatively, copy `.env.example` to `.env` and fill in the values. The application
loads `.env` automatically; variables already exported by the shell take precedence.
Use `--env-file path/to/file` to select a different file.

Open <http://127.0.0.1:8001>. `serve` performs one ingestion before starting. To use
an existing local corpus without fetching, pass `--skip-ingest`. Run ingestion alone
with `uv run cta-pipeline ingest`.

Port 8001 is the default. Override it when needed:

```bash
uv run cta-pipeline serve --port 9000
```

Configuration:

- `OPENAI_API_KEY` — required for answers.
- `OPENAI_BASE_URL` — optional OpenAI-compatible API root; defaults to OpenAI.
- `OPENAI_MODEL` — defaults to `gpt-5-mini`.
- `CTA_API_KEY` — enables live Train Tracker arrival questions.
- `CTA_DB_PATH` — defaults to `./data/rag.db`.

## HTTP interface

- `GET /` — question UI, current corpus, and recent ingestion runs.
- `GET /api/snapshot` — normalized active documents.
- `GET /api/runs` — ingestion history.
- `GET /api/health` — process health.
- `POST /api/ask` — `{"question":"What is affecting the Red Line?"}`.

## Test

```bash
uv run python -m unittest discover -s tests -v
```

The focused suite tests versioning/idempotency, retrieval, LLM context boundaries,
and safe website rendering.
