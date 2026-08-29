"""Bounded GTFS-Realtime JSON ingestion and deterministic operational signals."""

import hashlib
import gzip
import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .limits import MAX_ERROR, read_bounded

MAX_TELEMETRY_BYTES = 8 * 1024 * 1024
MAX_ENTITIES = 20000
MAX_EXPANDED_PREDICTIONS = 50000
# Hard global row bounds. Time retention is an additional, independently configured bound.
MAX_SNAPSHOT_ROWS = 20000
MAX_RUN_ROWS = 100000
MAX_VEHICLE_OBSERVATION_ROWS = 100000
MAX_TRIP_OBSERVATION_ROWS = 100000
MAX_ANOMALY_ROWS = 20000
BASE_URL = "https://transitdata.transitchicago.com/GtfsRealtime"
TRAIN_TRACKER_URL = "https://lapi.transitchicago.com/api/1.0/ttpositions.aspx"
TRAIN_TRACKER_ROUTES = ("red", "blue", "brn", "g", "org", "p", "pink", "y")
TRAIN_TRACKER_ROUTE_IDS = {"red":"Red", "blue":"Blue", "brn":"Brn", "g":"G",
                           "org":"Org", "p":"P", "pink":"Pink", "y":"Y"}


class TelemetryError(RuntimeError): pass


class AnomalyExplainer:
    """Optional OpenAI-compatible explanation of one bounded derived anomaly."""
    def __init__(self, fetcher=None, max_response_bytes=1024 * 1024):
        self.fetcher=fetcher or urlopen; self.max_response_bytes=max_response_bytes
        self.base_url=os.getenv("OPENAI_BASE_URL","https://api.openai.com/v1").rstrip("/")
        self.model=os.getenv("OPENAI_MODEL","gpt-5-mini")[:256]
    def explain(self, anomaly):
        key=os.getenv("OPENAI_API_KEY")
        if not key: raise TelemetryError("OPENAI_API_KEY is not configured")
        context={k:anomaly[k] for k in ("kind","severity","entity_key","deterministic_text","context")}
        schema={"type":"object","properties":{"text":{"type":"string","maxLength":2000}},"required":["text"],"additionalProperties":False}
        payload={"model":self.model,"messages":[{"role":"system","content":"Explain this derived GTFS-Realtime anomaly cautiously. Do not infer causation. Return JSON."},{"role":"user","content":json.dumps(context,sort_keys=True)[:6000]}],"response_format":{"type":"json_schema","json_schema":{"name":"telemetry_anomaly","strict":True,"schema":schema}}}
        req=Request(self.base_url+"/chat/completions",data=json.dumps(payload).encode(),headers={"Authorization":"Bearer "+key,"Content-Type":"application/json","User-Agent":"cta-rail-pipeline/0.2"})
        raw=read_bounded(self.fetcher(req,timeout=20),self.max_response_bytes,"OpenAI")
        try:
            outer=json.loads(raw); choices=outer.get("choices")
            content=choices[0].get("message",{}).get("content") if isinstance(choices,list) and choices else None
            candidate=json.loads(content) if isinstance(content,str) else None
        except (ValueError,TypeError): candidate=None
        if not isinstance(candidate,dict) or set(candidate)!={"text"} or not isinstance(candidate["text"],str) or not candidate["text"] or len(candidate["text"])>2000:
            raise TelemetryError("anomaly explanation schema validation failed")
        return {"text":candidate["text"],"method":"openai","model":self.model}


def _integer(value, name, required=False):
    if value is None and not required: return None
    if isinstance(value, bool): raise TelemetryError(f"invalid {name}")
    try: return int(value)
    except (TypeError, ValueError): raise TelemetryError(f"invalid {name}") from None


def _number(value, name):
    if value is None: return None
    try: result = float(value)
    except (TypeError, ValueError): raise TelemetryError(f"invalid {name}") from None
    if not (-180 <= result <= 180): raise TelemetryError(f"invalid {name}")
    return result


def canonical_feed(feed_name, document):
    if not isinstance(document, dict) or document.get("error"):
        raise TelemetryError(f"malformed {feed_name} envelope")
    header, entities = document.get("header"), document.get("entity")
    if not isinstance(header, dict) or not isinstance(header.get("gtfsRealtimeVersion"), str):
        raise TelemetryError(f"malformed {feed_name} header")
    timestamp = _integer(header.get("timestamp"), "feed timestamp", required=True)
    if not isinstance(entities, list) or len(entities) > MAX_ENTITIES:
        raise TelemetryError(f"malformed or oversized {feed_name} entities")
    member = "vehicle" if feed_name == "VehiclePositions" else "tripUpdate"
    entity_ids = set()
    for entity in entities:
        if not isinstance(entity, dict) or not isinstance(entity.get("id"), str):
            raise TelemetryError(f"malformed {feed_name} entity")
        if entity["id"] in entity_ids:
            raise TelemetryError(f"duplicate {feed_name} entity id")
        entity_ids.add(entity["id"])
        if entity.get("isDeleted") is True: continue
        if member in entity and not isinstance(entity[member], dict):
            raise TelemetryError(f"malformed {feed_name} entity")
    canonical = {"header": {"gtfsRealtimeVersion": header["gtfsRealtimeVersion"],
                            "timestamp": timestamp}, "entity": entities}
    return canonical


class GTFSRealtimeClient:
    def __init__(self, feed_name, api_key=None, fetcher=None, timeout=15.0,
                 max_response_bytes=MAX_TELEMETRY_BYTES):
        if feed_name not in ("VehiclePositions", "TripUpdates"):
            raise ValueError("unsupported GTFS-Realtime feed")
        self.feed_name = feed_name; self.api_key = api_key
        self.fetcher = fetcher or urlopen; self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def fetch(self):
        if not self.api_key: raise TelemetryError("CTA_API_KEY is required for telemetry")
        url = f"{BASE_URL}/{self.feed_name}.json?" + urlencode({"key": self.api_key})
        request = Request(url, headers={"User-Agent": "cta-rail-pipeline/0.2"})
        try:
            raw = read_bounded(self.fetcher(request, timeout=self.timeout),
                               self.max_response_bytes, self.feed_name)
            document = json.loads(raw.decode("utf-8"))
            return raw, canonical_feed(self.feed_name, document)
        except TelemetryError: raise
        except ValueError as exc:
            if "response too large" in str(exc):
                raise TelemetryError(str(exc)) from None
            raise TelemetryError(f"{self.feed_name} fetch failed (ValueError)") from None
        except Exception as exc:
            # Never include provider exception text: it may contain the requested URL/key.
            kind = type(exc).__name__
            raise TelemetryError(f"{self.feed_name} fetch failed ({kind})") from None


def _traintracker_epoch(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}", value) is None:
        raise TelemetryError("invalid TrainTracker timestamp")
    try:
        naive = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
        chicago = ZoneInfo("America/Chicago")
        candidates = [naive.replace(tzinfo=chicago, fold=fold) for fold in (0, 1)]
        valid = [candidate for candidate in candidates
                 if datetime.fromtimestamp(candidate.timestamp(), chicago).replace(tzinfo=None) == naive]
    except (ValueError, OverflowError):
        raise TelemetryError("invalid TrainTracker timestamp") from None
    offsets = {candidate.utcoffset() for candidate in valid}
    if not valid or len(offsets) != 1:
        raise TelemetryError("invalid TrainTracker timestamp")
    return int(valid[0].timestamp())


def _required_text(value, name, limit=256, pattern=None):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise TelemetryError(f"invalid {name}")
    if any(unicodedata.category(character) in ("Cc", "Cf") for character in value):
        raise TelemetryError(f"invalid {name}")
    if pattern is not None and re.fullmatch(pattern, value) is None:
        raise TelemetryError(f"invalid {name}")
    return value


def _traintracker_flag(value, name):
    if not isinstance(value, str) or value not in ("0", "1"):
        raise TelemetryError(f"invalid {name}")
    return int(value)


class TrainTrackerPositionsClient:
    """Bounded adapter from CTA Train Tracker positions to the canonical vehicle feed."""
    def __init__(self, api_key=None, fetcher=None, timeout=15.0,
                 max_response_bytes=MAX_TELEMETRY_BYTES):
        self.api_key=api_key; self.fetcher=fetcher or urlopen; self.timeout=timeout
        self.max_response_bytes=max_response_bytes; self.feed_timestamp=None

    def fetch(self):
        if not self.api_key:
            raise TelemetryError("CTA_API_KEY is required for telemetry")
        url=TRAIN_TRACKER_URL+"?"+urlencode({"key":self.api_key,
            "rt":",".join(TRAIN_TRACKER_ROUTES),"outputType":"JSON"})
        request=Request(url,headers={"User-Agent":"cta-rail-pipeline/0.2"})
        try:
            raw=read_bounded(self.fetcher(request,timeout=self.timeout),
                             self.max_response_bytes,"TrainTrackerPositions")
            document=json.loads(raw.decode("utf-8"))
            feed=self._canonical(document)
            self.feed_timestamp=feed["header"]["timestamp"]
            return raw,feed
        except TelemetryError: raise
        except ValueError as exc:
            if "response too large" in str(exc):
                raise TelemetryError(str(exc)) from None
            raise TelemetryError("TrainTrackerPositions fetch failed (ValueError)") from None
        except Exception as exc:
            raise TelemetryError(f"TrainTrackerPositions fetch failed ({type(exc).__name__})") from None

    @staticmethod
    def _canonical(document):
        root=document.get("ctatt") if isinstance(document,dict) else None
        if not isinstance(root,dict):
            raise TelemetryError("malformed TrainTrackerPositions root")
        code=root.get("errCd")
        if str(code) != "0":
            safe_code=str(code) if re.fullmatch(r"[0-9]{1,8}",str(code)) else "unknown"
            raise TelemetryError(f"TrainTrackerPositions error code {safe_code}")
        feed_timestamp=_traintracker_epoch(root.get("tmst"))
        routes=root.get("route")
        if not isinstance(routes,list) or len(routes)>len(TRAIN_TRACKER_ROUTES):
            raise TelemetryError("malformed or oversized TrainTracker routes")
        seen_routes=set(); seen_trains=set(); entities=[]
        for route in routes:
            if not isinstance(route,dict):
                raise TelemetryError("malformed TrainTracker route")
            route_name=route.get("@name")
            if route_name not in TRAIN_TRACKER_ROUTE_IDS:
                raise TelemetryError("invalid TrainTracker route")
            if route_name in seen_routes:
                raise TelemetryError("duplicate TrainTracker route")
            seen_routes.add(route_name)
            trains=route.get("train")
            if isinstance(trains,dict):
                trains=[trains]
            if not isinstance(trains,list) or len(entities)+len(trains)>MAX_ENTITIES:
                raise TelemetryError("malformed or oversized TrainTracker trains")
            route_id=TRAIN_TRACKER_ROUTE_IDS[route_name]
            for train in trains:
                if not isinstance(train,dict):
                    raise TelemetryError("malformed TrainTracker train")
                run=_required_text(train.get("rn"),"train run",32,r"[A-Za-z0-9-]+")
                identity=f"{len(route_id)}:{route_id}{len(run)}:{run}"
                if identity in seen_trains:
                    raise TelemetryError("duplicate resolved train identity")
                seen_trains.add(identity)
                direction=_integer(train.get("trDr"),"direction",required=True)
                if direction not in (1, 5):
                    raise TelemetryError("invalid direction")
                is_delayed=_traintracker_flag(train.get("isDly"),"isDly")
                stop_id=_required_text(train.get("nextStpId"),"next stop",32,r"[A-Za-z0-9-]+")
                label=_required_text(train.get("destNm"),"destination label")
                latitude=_number(train.get("lat"),"latitude")
                longitude=_number(train.get("lon"),"longitude")
                if latitude is None or not -90<=latitude<=90:
                    raise TelemetryError("invalid latitude")
                if longitude is None:
                    raise TelemetryError("invalid longitude")
                vehicle_timestamp=_traintracker_epoch(train.get("prdt"))
                entities.append({"id":identity,"vehicle":{"trip":{"tripId":identity,
                    "routeId":route_id,"directionId":direction},"vehicle":{"id":identity,
                    "label":label},"position":{"latitude":latitude,"longitude":longitude},
                    "stopId":stop_id,"currentStatus":"IN_TRANSIT_TO","isDelayed":is_delayed,
                    "timestamp":vehicle_timestamp}})
        return canonical_feed("VehiclePositions",{"header":{"gtfsRealtimeVersion":"2.0",
            "timestamp":feed_timestamp},"entity":entities})


class EmptyTripUpdatesClient:
    """Deterministic companion for a successfully fetched Train Tracker cycle."""
    def __init__(self, positions_client): self.positions_client=positions_client
    def fetch(self):
        timestamp=self.positions_client.feed_timestamp
        if timestamp is None:
            raise TelemetryError("TrainTrackerPositions must be fetched before TripUpdates")
        return b"",{"header":{"gtfsRealtimeVersion":"2.0","timestamp":timestamp},"entity":[]}


def _iso_epoch(stamp):
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())


def _text(value, limit=256):
    return str(value)[:limit] if value is not None else None


def _vehicles(feed, observed_at):
    rows = []
    vehicle_ids = set()
    for entity in feed["entity"]:
        value = entity.get("vehicle")
        if entity.get("isDeleted") or not isinstance(value, dict): continue
        descriptor = value.get("vehicle") if isinstance(value.get("vehicle"), dict) else {}
        trip = value.get("trip") if isinstance(value.get("trip"), dict) else {}
        position = value.get("position") if isinstance(value.get("position"), dict) else {}
        vehicle_id = _text(descriptor.get("id") or entity["id"])
        if vehicle_id in vehicle_ids:
            raise TelemetryError("duplicate resolved vehicle identity")
        vehicle_ids.add(vehicle_id)
        lat = _number(position.get("latitude"), "latitude")
        lon = _number(position.get("longitude"), "longitude")
        if lat is not None and not -90 <= lat <= 90: raise TelemetryError("invalid latitude")
        rows.append({"vehicle_id":vehicle_id,"entity_id":_text(entity["id"]),
            "route_id":_text(trip.get("routeId")),"direction_id":_integer(trip.get("directionId"),"direction"),
            "trip_id":_text(trip.get("tripId")),"latitude":lat,"longitude":lon,
            "stop_id":_text(value.get("stopId")),"current_status":_text(value.get("currentStatus")),
            "is_delayed":_integer(value.get("isDelayed"),"isDelayed"),
            "vehicle_timestamp":_integer(value.get("timestamp"),"vehicle timestamp"),
            "feed_timestamp":feed["header"]["timestamp"],"observed_at":observed_at,
            "label":_text(descriptor.get("label"))})
    return rows


def _predictions(feed, observed_at):
    rows=[]
    prediction_keys=set()
    for entity in feed["entity"]:
        update=entity.get("tripUpdate")
        if entity.get("isDeleted") or not isinstance(update,dict): continue
        descriptor=update.get("trip") if isinstance(update.get("trip"),dict) else {}
        trip_id=_text(descriptor.get("tripId") or entity["id"])
        updates=update.get("stopTimeUpdate",[])
        if not isinstance(updates,list): raise TelemetryError("malformed stopTimeUpdate")
        if len(rows) + len(updates) > MAX_EXPANDED_PREDICTIONS:
            raise TelemetryError("oversized expanded predictions")
        for item in updates:
            if not isinstance(item,dict): raise TelemetryError("malformed stop prediction")
            arrival=item.get("arrival") if isinstance(item.get("arrival"),dict) else {}
            departure=item.get("departure") if isinstance(item.get("departure"),dict) else {}
            delay=arrival.get("delay",departure.get("delay"))
            stop_id=_text(item.get("stopId"))
            stop_sequence=_integer(item.get("stopSequence"),"stop sequence")
            key=(trip_id,stop_id,stop_sequence)
            if key in prediction_keys:
                raise TelemetryError("duplicate prediction identity")
            prediction_keys.add(key)
            rows.append({"trip_id":trip_id,"route_id":_text(descriptor.get("routeId")),
              "direction_id":_integer(descriptor.get("directionId"),"direction"),
              "stop_id":stop_id,"stop_sequence":stop_sequence,
              "arrival_time":_integer(arrival.get("time"),"arrival time"),
              "departure_time":_integer(departure.get("time"),"departure time"),
              "delay":_integer(delay,"delay"),"observed_at":observed_at})
    return rows


def _anomaly(kind, entity_key, text, context, severity="warning"):
    # A fingerprint identifies the continuing rule/entity condition, not volatile age values.
    stable=json.dumps({"kind":kind,"entity":entity_key},sort_keys=True,separators=(",",":"))
    return {"fingerprint":hashlib.sha256(stable.encode()).hexdigest(),"kind":kind,"entity_key":entity_key,
            "deterministic_text":text,"context":context,"severity":severity}


class TelemetryPipeline:
    def __init__(self, db, vehicle_client=None, trip_client=None, clock=None,
                 retention_hours=24, stale_seconds=120, delay_seconds=300,
                 stationary_seconds=600, gap_seconds=900, explainer=None, source=None):
        self.db=db
        if vehicle_client is None and trip_client is None:
            gtfs_key=os.getenv("CTA_GTFS_API_KEY","").strip()
            if gtfs_key:
                self.vehicle_client=GTFSRealtimeClient("VehiclePositions",gtfs_key)
                self.trip_client=GTFSRealtimeClient("TripUpdates",gtfs_key)
                self.source="gtfs-realtime"
            else:
                self.vehicle_client=TrainTrackerPositionsClient(os.getenv("CTA_API_KEY"))
                self.trip_client=EmptyTripUpdatesClient(self.vehicle_client)
                self.source="traintracker"
        else:
            if vehicle_client is None or trip_client is None:
                raise ValueError("both telemetry clients are required")
            self.vehicle_client=vehicle_client; self.trip_client=trip_client
            self.source=source or "gtfs-realtime"
        if self.source not in ("traintracker", "gtfs-realtime"):
            raise ValueError("unsupported telemetry source")
        self.clock=clock or (lambda: datetime.now(timezone.utc).isoformat().replace("+00:00","Z"))
        self.retention_hours=float(retention_hours); self.stale_seconds=int(stale_seconds)
        self.delay_seconds=int(delay_seconds); self.stationary_seconds=int(stationary_seconds)
        self.gap_seconds=int(gap_seconds)
        self.explainer=explainer if explainer is not None else (AnomalyExplainer() if os.getenv("CTA_LLM_ANOMALIES","").lower()=="true" else None)

    def ingest(self):
        stamp=self.clock()
        try:
            _, vf=self.vehicle_client.fetch(); _, tf=self.trip_client.fetch()
            vehicles=_vehicles(vf,stamp); predictions=_predictions(tf,stamp)
            anomalies=self._dedupe_anomalies(self._derive(vf,tf,vehicles,predictions,stamp))
            if len(anomalies) > MAX_ANOMALY_ROWS:
                raise TelemetryError("oversized derived anomalies")
            with self.db.connect() as con:
                existing={r[0] for r in con.execute("select fingerprint from anomalies")}
            for anomaly in anomalies:
                if anomaly["fingerprint"] in existing: continue
                explanation={"text":anomaly["deterministic_text"],"method":"deterministic","model":"rules-v1"}
                if self.explainer:
                    try: explanation=self.explainer.explain(anomaly)
                    except Exception: explanation={"text":anomaly["deterministic_text"],"method":"deterministic-fallback","model":"rules-v1"}
                if not (isinstance(explanation,dict) and isinstance(explanation.get("text"),str)
                        and isinstance(explanation.get("method"),str) and isinstance(explanation.get("model"),str)):
                    explanation={"text":anomaly["deterministic_text"],"method":"deterministic-fallback","model":"rules-v1"}
                anomaly["explanation"]={k:_text(v,2000) for k,v in explanation.items()}
            return self._persist(vf,tf,vehicles,predictions,anomalies,stamp)
        except Exception as exc:
            self._failure(stamp,exc); raise

    @staticmethod
    def _dedupe_anomalies(anomalies):
        chosen={}
        for anomaly in anomalies:
            key=anomaly["fingerprint"]
            rank=(json.dumps(anomaly["context"],sort_keys=True,separators=(",",":")),
                  anomaly["deterministic_text"])
            current=chosen.get(key)
            if current is None or rank > (json.dumps(current["context"],sort_keys=True,separators=(",",":")),current["deterministic_text"]):
                chosen[key]=anomaly
        return [chosen[key] for key in sorted(chosen)]

    def _derive(self,vf,tf,vehicles,predictions,stamp):
        now_epoch=_iso_epoch(stamp); result=[]
        for name,feed in (("vehicle_positions",vf),("trip_updates",tf)):
            age=max(0,now_epoch-feed["header"]["timestamp"])
            if age>=self.stale_seconds:
                bucket=age//max(self.stale_seconds,1)
                result.append(_anomaly("stale_feed",name,f"{name} feed timestamp is {age} seconds old.",
                                      {"feed":name,"age_seconds":age,"age_bucket":bucket}))
        for row in predictions:
            if row["delay"] is not None and row["delay"]>=self.delay_seconds:
                result.append(_anomaly("material_delay",f"{row['trip_id']}|{row['stop_id']}",
                    f"Trip {row['trip_id']} has a reported prediction delay of {row['delay']} seconds.",
                    {"trip_id":row["trip_id"],"stop_id":row["stop_id"],"delay_seconds":row["delay"]}))
        groups={}
        for row in predictions:
            if row["arrival_time"] is not None and row["route_id"] and row["stop_id"]:
                groups.setdefault((row["route_id"],row["direction_id"],row["stop_id"]),[]).append(row["arrival_time"])
        for key,times in groups.items():
            ordered=sorted(set(times))
            for left,right in zip(ordered,ordered[1:]):
                if right-left>=self.gap_seconds:
                    entity="|".join(map(str,key)); result.append(_anomaly("arrival_gap",entity,
                      f"Predicted arrivals for route {key[0]}, direction {key[1]}, stop {key[2]} have a {right-left} second gap.",
                      {"route_id":key[0],"direction_id":key[1],"stop_id":key[2],"gap_seconds":right-left,"from":left,"to":right}))
        with self.db.connect() as con:
            prior={r["vehicle_id"]:r for r in con.execute("select vehicle_id,entity_id,trip_id,latitude,longitude,vehicle_timestamp,stationary_since from vehicle_state")}
        for row in vehicles:
            old=prior.get(row["vehicle_id"]); current_time=row["vehicle_timestamp"] or now_epoch
            same=(old and old["entity_id"]==row["entity_id"] and old["trip_id"]==row["trip_id"]
                  and row["latitude"] is not None and row["longitude"] is not None
                  and old["latitude"] is not None and old["longitude"] is not None
                  and abs(row["latitude"]-old["latitude"])<0.0001 and abs(row["longitude"]-old["longitude"])<0.0001)
            elapsed=current_time-(old["stationary_since"] or old["vehicle_timestamp"] or current_time) if same else 0
            if same and elapsed>=self.stationary_seconds:
                result.append(_anomaly("stationary_vehicle",row["vehicle_id"],
                  f"Vehicle {row['vehicle_id']} has unchanged reported coordinates across {elapsed} seconds.",
                  {"vehicle_id":row["vehicle_id"],"duration_seconds":elapsed,"route_id":row["route_id"]}))
        return result

    def _persist(self,vf,tf,vehicles,predictions,anomalies,stamp):
        cutoff=(datetime.fromisoformat(stamp.replace("Z","+00:00"))-timedelta(hours=self.retention_hours)).isoformat().replace("+00:00","Z")
        created=0; new_anomalies=0
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            cur=con.execute("insert into telemetry_runs(started_at,status,source) values(?,?,?)",(stamp,"running",self.source)); run=cur.lastrowid
            for name,feed in (("vehicle_positions",vf),("trip_updates",tf)):
                encoded=json.dumps(feed,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode(); digest=hashlib.sha256(encoded).hexdigest()
                compressed=gzip.compress(encoded,compresslevel=9,mtime=0)
                old=con.execute("select id from telemetry_snapshots where feed_type=? and content_hash=?",(name,digest)).fetchone()
                con.execute("insert into telemetry_snapshots(run_id,feed_type,content_hash,canonical_json,feed_timestamp,created_at) values(?,?,?,?,?,?) on conflict(feed_type,content_hash) do update set run_id=excluded.run_id,canonical_json=excluded.canonical_json,feed_timestamp=excluded.feed_timestamp,created_at=excluded.created_at",(run,name,digest,compressed,feed["header"]["timestamp"],stamp))
                created += old is None
            present=[]
            for r in vehicles:
                present.append(r["vehicle_id"]); old=con.execute("select * from vehicle_state where vehicle_id=?",(r["vehicle_id"],)).fetchone()
                fields=("route_id","direction_id","latitude","longitude","stop_id","current_status","is_delayed","vehicle_timestamp")
                if old is None or any(old[k]!=r[k] for k in fields):
                    con.execute("insert into vehicle_observations(run_id,vehicle_id,route_id,direction_id,latitude,longitude,stop_id,current_status,is_delayed,vehicle_timestamp,observed_at) values(?,?,?,?,?,?,?,?,?,?,?)",(run,r["vehicle_id"],r["route_id"],r["direction_id"],r["latitude"],r["longitude"],r["stop_id"],r["current_status"],r["is_delayed"],r["vehicle_timestamp"],stamp))
                same=(old is not None and old["entity_id"]==r["entity_id"] and old["trip_id"]==r["trip_id"] and old["latitude"] is not None and old["longitude"] is not None and r["latitude"] is not None and r["longitude"] is not None and abs(old["latitude"]-r["latitude"])<0.0001 and abs(old["longitude"]-r["longitude"])<0.0001)
                stationary_since=(old["stationary_since"] or old["vehicle_timestamp"] or r["vehicle_timestamp"] or _iso_epoch(stamp)) if same else (r["vehicle_timestamp"] or _iso_epoch(stamp))
                con.execute("insert into vehicle_state(vehicle_id,entity_id,route_id,direction_id,trip_id,latitude,longitude,stop_id,current_status,is_delayed,vehicle_timestamp,feed_timestamp,observed_at,label,stationary_since) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) on conflict(vehicle_id) do update set entity_id=excluded.entity_id,route_id=excluded.route_id,direction_id=excluded.direction_id,trip_id=excluded.trip_id,latitude=excluded.latitude,longitude=excluded.longitude,stop_id=excluded.stop_id,current_status=excluded.current_status,is_delayed=excluded.is_delayed,vehicle_timestamp=excluded.vehicle_timestamp,feed_timestamp=excluded.feed_timestamp,observed_at=excluded.observed_at,label=excluded.label,stationary_since=excluded.stationary_since",(*r.values(),stationary_since))
            if present:
                con.execute(f"delete from vehicle_state where vehicle_id not in ({','.join('?' for _ in present)})",present)
            else: con.execute("delete from vehicle_state")
            for r in predictions:
                key=json.dumps([r["trip_id"],r["stop_id"],r["stop_sequence"]],separators=(",",":"))
                material={k:v for k,v in r.items() if k!="observed_at"}; signature=hashlib.sha256(json.dumps(material,sort_keys=True,separators=(",",":")).encode()).hexdigest()
                old=con.execute("select signature from prediction_state where prediction_key=?",(key,)).fetchone()
                if old is None or old[0]!=signature:
                    con.execute("insert into trip_prediction_observations(run_id,trip_id,route_id,direction_id,stop_id,stop_sequence,arrival_time,departure_time,delay,observed_at) values(?,?,?,?,?,?,?,?,?,?)",(run,*r.values()))
                con.execute("insert into prediction_state values(?,?) on conflict(prediction_key) do update set signature=excluded.signature",(key,signature))
            prediction_keys={json.dumps([r["trip_id"],r["stop_id"],r["stop_sequence"]],separators=(",",":")) for r in predictions}
            if prediction_keys:
                con.execute(f"delete from prediction_state where prediction_key not in ({','.join('?' for _ in prediction_keys)})",tuple(prediction_keys))
            else:
                con.execute("delete from prediction_state")
            con.execute("update anomalies set active=0")
            for a in anomalies:
                explanation=a.get("explanation")
                if explanation:
                    cur=con.execute("insert or ignore into anomalies(fingerprint,kind,severity,entity_key,deterministic_text,context_json,first_seen_at,last_seen_at,active,explanation_text,method,model) values(?,?,?,?,?,?,?,?,1,?,?,?)",(a["fingerprint"],a["kind"],a["severity"],a["entity_key"],a["deterministic_text"],json.dumps(a["context"],sort_keys=True),stamp,stamp,explanation["text"],explanation["method"],explanation["model"])); new_anomalies+=cur.rowcount
                old=con.execute("select context_json,deterministic_text from anomalies where fingerprint=?",(a["fingerprint"],)).fetchone()
                context=json.dumps(a["context"],sort_keys=True); changed=old is not None and (old[0]!=context or old[1]!=a["deterministic_text"])
                if changed:
                    con.execute("update anomalies set severity=?,deterministic_text=?,context_json=?,last_seen_at=?,active=1,explanation_text=?,method='deterministic-current',model='rules-v1' where fingerprint=?",(a["severity"],a["deterministic_text"],context,stamp,a["deterministic_text"],a["fingerprint"]))
                else:
                    con.execute("update anomalies set severity=?,deterministic_text=?,context_json=?,last_seen_at=?,active=1 where fingerprint=?",(a["severity"],a["deterministic_text"],context,stamp,a["fingerprint"]))
            con.execute("update telemetry_runs set finished_at=?,status='success',vehicle_feed_timestamp=?,trip_feed_timestamp=?,vehicles=?,predictions=? where id=?",(stamp,vf["header"]["timestamp"],tf["header"]["timestamp"],len(vehicles),len(predictions),run))
            con.execute("delete from telemetry_snapshots where created_at < ?",(cutoff,))
            con.execute("delete from telemetry_snapshots where id in (select id from telemetry_snapshots order by created_at desc,id desc limit -1 offset ?)",(MAX_SNAPSHOT_ROWS,))
            con.execute("delete from vehicle_observations where observed_at < ?",(cutoff,))
            con.execute("delete from trip_prediction_observations where observed_at < ?",(cutoff,))
            con.execute("delete from anomalies where active=0 and last_seen_at < ?",(cutoff,))
            con.execute("delete from vehicle_observations where id in (select id from vehicle_observations order by id desc limit -1 offset ?)",(MAX_VEHICLE_OBSERVATION_ROWS,))
            con.execute("delete from trip_prediction_observations where id in (select id from trip_prediction_observations order by id desc limit -1 offset ?)",(MAX_TRIP_OBSERVATION_ROWS,))
            con.execute("delete from anomalies where active=0 and id in (select id from anomalies where active=0 order by last_seen_at desc,id desc limit -1 offset max(0,?-(select count(*) from anomalies where active=1)))",(MAX_ANOMALY_ROWS,))
            con.execute("delete from telemetry_runs where finished_at < ? and id not in (select run_id from telemetry_snapshots)",(cutoff,))
            self._cap_runs(con)
        return {"run_id":run,"status":"success","source":self.source,"vehicles":len(vehicles),"predictions":len(predictions),"snapshots_created":created,"new_anomalies":new_anomalies,"vehicle_feed_timestamp":vf["header"]["timestamp"],"trip_feed_timestamp":tf["header"]["timestamp"]}

    def _failure(self,stamp,exc):
        try:
            detail=(str(exc) if isinstance(exc,TelemetryError) and
                    str(exc).startswith("TrainTrackerPositions error code ") else
                    f"{self.source} telemetry cycle failed")
            with self.db.connect() as con:
                con.execute("insert into telemetry_runs(started_at,finished_at,status,error,source) values(?,?,?,?,?)",(stamp,stamp,"failed",f"{type(exc).__name__}: {detail}"[:MAX_ERROR],self.source))
                self._cap_runs(con)
        except Exception: pass

    @staticmethod
    def _cap_runs(con):
        referenced=con.execute("select count(distinct run_id) from telemetry_snapshots").fetchone()[0]
        unreferenced_limit=max(0,MAX_RUN_ROWS-referenced)
        con.execute("delete from telemetry_runs where id in (select id from telemetry_runs where id not in (select run_id from telemetry_snapshots) order by id desc limit -1 offset ?)",(unreferenced_limit,))
