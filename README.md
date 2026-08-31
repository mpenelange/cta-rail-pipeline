# CTA Signal

CTA Signal is a terminal application for asking natural-language questions about Chicago rail service. A deterministic retrieval layer combines live arrivals and alerts with a GTFS station, route, and transfer graph, then passes the relevant evidence to an LLM for a grounded answer or requests clarification when a station is ambiguous.

## Run

Install [uv](https://docs.astral.sh/uv/), then:

```bash
cp .env.example .env
# Add OPENAI_API_KEY and CTA_API_KEY to .env
uv run cta-pipeline
```

The command ingests a fresh alert snapshot and opens the TUI. Use the arrow keys and
Enter for clarification choices; press Escape or Ctrl-C to exit. To reuse the local
corpus without fetching first:

```bash
uv run cta-pipeline tui --skip-ingest
```

Run ingestion without opening the interface:

```bash
uv run cta-pipeline ingest
```

The application loads `.env` automatically without overriding variables already in the
process environment. Select another file with `--env-file path/to/file`.

Configuration:

- `OPENAI_API_KEY` — required for generated answers.
- `OPENAI_BASE_URL` — optional OpenAI-compatible API root.
- `OPENAI_MODEL` — defaults to `gpt-5-mini`.
- `CTA_API_KEY` — required for live Train Tracker arrivals.
- `CTA_DB_PATH` — defaults to `./data/rag.db`.

## Test

```bash
uv run python -m unittest discover -s tests -v
```

The focused suite covers ingestion versioning, retrieval, station resolution,
clarification routing, LLM context boundaries, and TUI state transitions.
