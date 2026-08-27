import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .limits import MAX_ID, MAX_LIST_TEXT, MAX_RIDERSHIP_BYTES, read_bounded

SOCRATA_URL = "https://data.cityofchicago.org/resource/5neh-572f.json"
MAX_STATION_ROWS = 1000
MAX_TOTAL_ROWS = 10000


class RidershipClient:
    def __init__(self, fetcher=None, timeout=15, max_response_bytes=MAX_RIDERSHIP_BYTES):
        self.fetcher = fetcher or urlopen
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.last_refresh = None

    def _request(self, params):
        req = Request(SOCRATA_URL + "?" + urlencode(params),
                      headers={"User-Agent": "cta-rail-pipeline/0.1"})
        response = self.fetcher(req, timeout=self.timeout)
        raw = read_bounded(response, self.max_response_bytes, "Socrata")
        rows = json.loads(raw.decode("utf-8"))
        if not isinstance(rows, list):
            raise ValueError("Socrata response must be an array")
        return rows

    def fetch(self):
        dates = self._request({"$select": "max(date) as date", "$limit": "1"})
        if len(dates) != 1 or not isinstance(dates[0], dict) or not isinstance(dates[0].get("date"), str):
            raise ValueError("Socrata latest-date schema is invalid")
        service_date = dates[0]["date"].strip()
        if not service_date:
            raise ValueError("Socrata latest service date is empty")
        rows = []
        while len(rows) < MAX_TOTAL_ROWS:
            page = self._request({"$select": "station_id,stationname,date,rides",
                                  "$where": "date=" + repr(service_date),
                                  "$order": "station_id ASC", "$limit": str(MAX_STATION_ROWS),
                                  "$offset": str(len(rows))})
            rows.extend(page)
            if len(page) < MAX_STATION_ROWS: return service_date, rows, "complete"
        return service_date, rows, "partial"

    def refresh(self, db):
        service_date, rows, status = self.fetch()
        parsed = []
        seen = set()
        required = ("station_id", "stationname", "date", "rides")
        for row in rows:
            if not isinstance(row, dict) or any(key not in row for key in required):
                raise ValueError("Socrata station row schema is invalid")
            sid = str(row["station_id"]).strip()[:MAX_ID]
            name = str(row["stationname"]).strip()[:MAX_LIST_TEXT]
            date = str(row["date"]).strip()
            if not sid or not date or date != service_date or sid in seen:
                raise ValueError("Socrata station row schema is inconsistent")
            try:
                rides = int(str(row["rides"]).replace(",", ""))
            except (TypeError, ValueError) as exc:
                raise ValueError("Socrata rides schema is invalid") from exc
            if rides < 0: raise ValueError("Socrata rides must be nonnegative")
            seen.add(sid); parsed.append((sid, name, date, rides))
        # The known station count is below this cap. Hitting it means completeness is unknown.
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM station_ridership")
            con.executemany("INSERT INTO station_ridership(station_id,station_name,latest_date,rides,fetched_at) VALUES(?,?,?,?,?)",
                            [(sid, name, date, rides, stamp) for sid, name, date, rides in parsed])
            con.execute("INSERT INTO ridership_refresh_state(id,service_date,row_count,status,fetched_at) VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET service_date=excluded.service_date,row_count=excluded.row_count,status=excluded.status,fetched_at=excluded.fetched_at",
                        (service_date, len(parsed), status, stamp))
        self.last_refresh = {"service_date": service_date, "row_count": len(parsed),
                             "status": status, "fetched_at": stamp}
        return len(parsed)
