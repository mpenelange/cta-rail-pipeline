# CTA Rail Disruption Intelligence Pipeline

A polished, local-first Python 3.13 MVP that fetches CTA rail alerts, preserves raw evidence, versions meaningful alert changes, extracts rider-facing intelligence, optionally enriches matched stations with ridership, and serves a compact dark dashboard and JSON API. It has no third-party runtime or test dependencies.

## Quick start

```bash
cd /root/apps/cta-rail-pipeline
export PYTHONPATH=src
python3 -m cta_pipeline init-db
python3 -m cta_pipeline demo
python3 -m cta_pipeline serve
```

Open <http://127.0.0.1:8000>. The default database is `./data/cta.db`. `demo` is fully offline and safe to repeat.

To ingest the live feed and start the server:

```bash
PYTHONPATH=src python3 -m cta_pipeline ingest --with-ridership
PYTHONPATH=src python3 -m cta_pipeline run --host 127.0.0.1 --port 8000
```

## Architecture

The application is intentionally small and inspectable:

```text
CTA JSON ── normalize ── canonical SHA-256 ── SQLite current + history
   │                                                │
   └── immutable raw snapshot                 deterministic extraction
                                                    │
Socrata latest station rides ───────────────────────┤
                                                    ▼
                                      stdlib HTTP API + dashboard
```

- `client.py` uses `urllib` with a 15-second default timeout and an explicit User-Agent.
- `normalize.py` tolerates absent/single/list alert and service shapes, CTA CDATA objects/wrappers, HTML entities/tags, and CRLF input.
- `db.py` applies an idempotent SQLite migration with foreign keys and WAL mode.
- `pipeline.py` stores every source document, hashes canonical normalized content, and creates a version/extraction only after a meaningful change.
- `extract.py` provides deterministic extraction and the optional OpenAI-compatible adapter.
- `ridership.py` resolves the latest available service date, paginates every station row for that date, and atomically replaces the cache with persisted date/count/completeness metadata.
- `server.py` provides the dashboard and API using `ThreadingHTTPServer`; it defaults to loopback only.

## Commands

| Command | Purpose |
|---|---|
| `init-db` | Create directories, apply migrations, and report the database path |
| `ingest [--with-ridership]` | Fetch and persist live CTA alerts; optionally refresh exposure data |
| `demo` | Load two realistic offline alerts and station rides |
| `serve [--host HOST --port PORT]` | Serve dashboard/API; defaults to `127.0.0.1:8000` |
| `run [--with-ridership] [--host ... --port ...]` | Perform one ingestion, then serve |

Commands print one machine-readable JSON result. Fetch failures print a concise JSON error to stderr, return nonzero, and record a failed ingestion run while preserving prior data.

Example demo output:

```json
{"alerts_seen": 2, "mode": "demo", "new_versions": 2, "ridership_rows": 2, "run_id": 1, "snapshot_hash": "1fde6d..."}
```

## HTTP interface

- `GET /` — responsive dashboard with line, severity, planned status, and text filters.
- `GET /api/health` — database health (`{"status":"ok","database":"ok"}`).
- `GET /api/alerts?line=Red&severity=Major&planned=true&q=Lake` — active alerts from the latest successful authoritative snapshot. Every filter is optional; the response includes ridership refresh metadata.
- `GET /api/alerts/<id>` — alert detail by local numeric ID or CTA source ID; unknown IDs return 404.

Responses include normalized agency facts, line colors, deterministic or model extraction provenance, confidence, exposure where a numeric station ID matches, and revision count. SQL values are parameterized. JSON uses the standard encoder and the dashboard escapes all API-derived content before inserting it into the document. A restrictive content-security policy and `nosniff` header are set.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `CTA_DB_PATH` | `./data/cta.db` | SQLite file path |
| `OPENAI_API_KEY` | unset | Enables optional model extraction when non-empty |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API root |
| `OPENAI_MODEL` | `gpt-5-mini` | Extraction model name |

Copy `.env.example` as a reference; Python does not load `.env` files automatically. Export values in your shell or supply them through Compose.

## Data model

| Table | Role |
|---|---|
| `schema_migrations` | Applied schema versions |
| `ingestion_runs` | Success/failure audit record and bounded error text |
| `raw_snapshots` | Immutable source bytes plus SHA-256 for each successful run |
| `alerts` | Stable CTA identity, current version/hash, first/last observation, and active state |
| `alert_versions` | Immutable canonical normalized JSON history |
| `extractions` | One structured extraction per immutable alert version |
| `station_ridership` | Latest known station entry count for exposure context |
| `ridership_refresh_state` | Latest cache service date, row count, completeness, and fetch timestamp |

The snapshot hash identifies exact source bytes. Alert hashes use sorted, compact UTF-8 JSON after normalization; fetch timestamps and raw key ordering therefore do not create revisions. Alert rows point to a contiguous current version, while all prior normalized versions remain queryable.

## Extraction provenance, safety, and cost

The local extractor is always available and derives summary, planned status, cause, effects, actions, lines/stations, accessibility impact, event type, and bounded confidence using deterministic rules. This is an aid for disruption triage, not an official service guarantee or trip planner.

When `OPENAI_API_KEY` is present, the adapter requests JSON-schema output from the configured OpenAI-compatible endpoint. It validates/coerces only the documented shape; malformed JSON, wrong types, timeouts, rate limits, and provider failures fall back to local extraction without failing ingestion. The key is sent only in the `Authorization` header and is never persisted, logged, included in a model prompt, or returned by the API. Model method/name/confidence are stored with each result. Each changed alert can make one model request, so cost depends on alert churn and provider pricing; leave the key unset for zero model cost.

Ridership is optional, cached, and failure-tolerant. Exposure is the sum of station-entry observations for the explicitly reported cache service date—not a live passenger count. Missing matches remain missing rather than being guessed, and partial refreshes are labeled.

## Development and verification

```bash
make test
make compile
make demo
```

The tests use injected byte fetchers and temporary file-backed SQLite databases; they never require network access. Strict RED/GREEN execution notes are preserved in `artifacts/tdd.log`.

Docker:

```bash
docker compose up --build
curl -f http://127.0.0.1:8000/api/health
```

The container initializes and serves the mounted `/data/cta.db`. Run `docker compose run --rm app demo` to seed offline data.

## Sources and terms caveat

- [CTA Customer Alerts API](https://www.transitchicago.com/developers/alerts/)
- [CTA alerts JSON endpoint](https://lapi.transitchicago.com/api/1.0/alerts.aspx?outputType=JSON&routeid=Red,Blue,Brn,G,Org,P,Pexp,Pink,Y)
- [Chicago Data Portal: CTA L Station Entries](https://data.cityofchicago.org/Transportation/CTA-Ridership-L-Station-Entries-Daily-Totals/5neh-572f)
- [Chicago Data Portal terms of use](https://www.chicago.gov/city/en/narr/foia/data_disclaimer.html)

Review current CTA and City of Chicago source terms, attribution guidance, rate limits, and data disclaimers before public or commercial deployment. This project is independent and not endorsed by the Chicago Transit Authority.

## License

MIT; see `LICENSE`.
