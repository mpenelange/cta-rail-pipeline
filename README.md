# CTA Signal

A deliberately small ingestion and retrieval-augmented generation pipeline with a
terminal interface. CTA is the sample live source; the reusable pipeline is the point.

```text
question → deterministic GTFS entity retrieval ───────────────┐
CTA alerts → normalize → version in SQLite → FTS5 retrieval ───┤
CTA GTFS → station/route/transfer graph ────────────────────────┼→ grounded LLM answer → TUI
CTA arrivals → resolved station → live retrieval ──────────────┤
structured clarification choices ──────────────────────────────┘
```

Changed alerts create immutable document versions. Questions retrieve only relevant
active documents. Arrival questions fetch predictions just in time rather than indexing
volatile data. Rather than asking an LLM to classify each question into a brittle schema,
the backend finds station and route entities in the authoritative GTFS catalog and retrieves
their connected evidence neighborhood: routes, stops, transfers, relevant alerts, and live
arrivals when the wording asks for fresh timing. Ambiguous station names produce keyboard-
selectable choices before retrieval. The LLM is used only for grounded synthesis, so new
wording does not require a new intent flow.

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
