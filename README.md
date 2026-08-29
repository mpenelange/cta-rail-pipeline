# CTA Rail Disruption Intelligence Pipeline

A local-first, Python 3.13 stdlib-only pipeline for CTA alerts and Train Tracker or GTFS-Realtime vehicle telemetry, with replayable SQLite history, deterministic anomaly signals, and a compact dashboard/API.

The image defaults to the offline-useful `serve` command, so a direct `docker run` does
not require `CTA_API_KEY`. Compose explicitly selects `live` and therefore requires the
CTA key for polling.

## Quick start

### Native macOS

Install [uv](https://docs.astral.sh/uv/), put a nonempty `CTA_API_KEY` in the project
`.env` (or export it), then validate and launch with:

```bash
./run-native.sh --check
./run-native.sh
```

Open the native dashboard at <http://localhost:8001>. Press Ctrl-C to stop. The script
uses `uv` to create a repo-local Python 3.13 `.venv` on first run, reuses a compatible
venv, and safely recreates an incompatible one. The stdlib-only app installs no packages.

Native configuration accepts `--env-file`, `--host`, `--port`, `--db-path`, and
`--openai-base-url`. Persistent native defaults can instead use `CTA_NATIVE_HOST`,
`CTA_NATIVE_PORT`, `CTA_NATIVE_DB_PATH`, and `OPENAI_NATIVE_BASE_URL`; these avoid
inheriting Docker's database path. Existing process environment values override the env
file. The native dotenv reader supports blank/comment lines, optional `export`, names
made from letters, digits, and underscores, and unquoted or matching single/double-quoted
values. It does not perform shell expansion. `CTA_LLM_ANOMALIES` accepts
`true`/`false`, `1`/`0`, `yes`/`no`, or `on`/`off` (case-insensitive). Docker remains
optional.

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
PYTHONPATH=src python3 -m cta_pipeline telemetry-ingest
PYTHONPATH=src python3 -m cta_pipeline live --host 127.0.0.1 --port 8000
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
| `telemetry-ingest` | Fetch one Train Tracker or GTFS-Realtime telemetry cycle and emit a JSON summary |
| `live [--host ... --port ...]` | Serve while polling telemetry every 30s and alerts every 300s |

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
- `GET /api/telemetry` — bounded operations summary, active source, and actual source timestamps.
- `GET /api/vehicles?limit=100` — current vehicle state (maximum 200).
- `GET /api/anomalies?limit=100` — recent active deterministic anomalies (maximum 200).

Responses include normalized agency facts, line colors, deterministic or model extraction provenance, confidence, exposure where a numeric station ID matches, and revision count. SQL values are parameterized. JSON uses the standard encoder and the dashboard escapes all API-derived content before inserting it into the document. A restrictive content-security policy and `nosniff` header are set.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `CTA_DB_PATH` | `./data/cta.db` | SQLite file path |
| `CTA_API_KEY` | unset | Required Train Tracker key; also used by Customer Alerts |
| `CTA_GTFS_API_KEY` | unset | Optional distinct CTA beta GTFS-Realtime key |
| `CTA_TELEMETRY_INTERVAL` | `30` | Seconds between telemetry cycles in `live` |
| `CTA_ALERTS_INTERVAL` | `300` | Seconds between alert cycles in `live` |
| `CTA_RETENTION_HOURS` | `24` | Telemetry snapshot/observation retention |
| `CTA_LLM_ANOMALIES` | `false` | Explain only newly fingerprinted deterministic anomalies |
| `CTA_DASHBOARD_PORT` | `8000` | Compose host port; use `8001` for `8001:8000` |
| `OPENAI_API_KEY` | unset | Enables optional model extraction when non-empty |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API root |
| `OPENAI_MODEL` | `gpt-5-mini` | Extraction model name |

Copy `.env.example` as a reference; Python does not load `.env` files automatically. Export values in your shell or supply them through Compose.

### Davit / Docker Desktop flow

Keep your real `CTA_API_KEY` only in the sibling `.env` on your Mac. Never paste it into source, logs, commands, or the database. From the project directory, point Compose at that file explicitly (adjust the path if needed):

```bash
docker compose --env-file ../.env up --build
```

For a host port of 8001, set `CTA_DASHBOARD_PORT=8001` in that local env file, or run `CTA_DASHBOARD_PORT=8001 docker compose --env-file ../.env up --build`; the container still listens on `0.0.0.0:8000`. Compose passes `CTA_API_KEY` explicitly and refuses startup when it is absent.

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
| `telemetry_runs`, `telemetry_snapshots` | Cycle audit and deduplicated gzip-compressed canonical JSON evidence (stdlib gzip; decompress to replay) |
| `vehicle_state`, `vehicle_observations` | Latest state plus timestamped replay history |
| `trip_prediction_observations` | Timestamped stop predictions and reported delays |
| `anomalies` | Stable fingerprints, deterministic wording, active state, and explanation provenance |

The snapshot hash identifies exact source bytes. Alert hashes use sorted, compact UTF-8 JSON after normalization; fetch timestamps and raw key ordering therefore do not create revisions. Alert rows point to a contiguous current version, while all prior normalized versions remain queryable.

Telemetry persistence is bounded before writes: a cycle with more than 50,000 expanded
stop predictions is rejected atomically (authoritative entity collections are never
silently truncated). Canonical feeds are content-deduplicated and gzip-compressed while
remaining directly replayable. Vehicle and prediction histories record material changes
only; deduplicated snapshots are reassociated with the latest successful cycle. Latest
vehicle and prediction state is authoritatively reconciled to each successful feed.
Snapshots and inactive anomalies have time retention, independently of hard global
ceilings: 20,000 telemetry snapshots, 100,000 telemetry runs, 100,000 vehicle
observations, 100,000 trip observations, and 20,000 anomalies. A derived active-anomaly
set over 20,000 is rejected atomically rather than truncated. Snapshot-referenced runs
are exempt from run pruning, and failed cycles enforce the audit-run cap themselves.

## Extraction provenance, safety, and cost

The local extractor is always available and derives summary, planned status, cause, effects, actions, lines/stations, accessibility impact, event type, and bounded confidence using deterministic rules. This is an aid for disruption triage, not an official service guarantee or trip planner.

When `OPENAI_API_KEY` is present, the adapter requests JSON-schema output from the configured OpenAI-compatible endpoint. It validates/coerces only the documented shape; malformed JSON, wrong types, timeouts, rate limits, and provider failures fall back to local extraction without failing ingestion. The key is sent only in the `Authorization` header and is never persisted, logged, included in a model prompt, or returned by the API. Model method/name/confidence are stored with each result. Each changed alert can make one model request, so cost depends on alert churn and provider pricing; leave the key unset for zero model cost.

Telemetry source selection is deterministic. With a nonempty `CTA_GTFS_API_KEY`, the pipeline uses CTA beta GTFS-Realtime VehiclePositions and TripUpdates with that distinct key. Otherwise it makes one HTTPS Train Tracker Locations request per cycle for `red,blue,brn,g,org,p,pink,y`, transforms positions into the canonical internal vehicle feed, and supplies a replayable empty TripUpdates companion. Train Tracker mode preserves vehicle history, stationary and stale detection, and authoritative empty-feed reconciliation. Reported prediction delays and arrival-gap rules are GTFS-only; Train Tracker mode does not fabricate predictions. The dashboard and `/api/telemetry` expose the active source.

CTA documents a default Train Tracker positions limit of 50,000 requests per day. One all-routes request every 30 seconds is 2,880 requests per day. `Pexp` is not a Train Tracker route code and is used only by Customer Alerts.

Telemetry structured data is never sent wholesale to a model. If `CTA_LLM_ANOMALIES=true`, only a bounded summary/context for a newly created fingerprint is sent to the existing OpenAI-compatible endpoint. Strict JSON is required; every failure retains deterministic text and records fallback provenance. CTA and OpenAI API keys are never included in prompts, exceptions, logs, snapshots, or database rows.

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
