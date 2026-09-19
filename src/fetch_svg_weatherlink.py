#!/usr/bin/env python3
"""Acquire recent SVG WeatherLink observations into one ephemeral transfer bundle."""
from __future__ import annotations
import json,os
from datetime import datetime,timedelta,timezone
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE="https://api.weatherlink.com/v2"
STATION_ID=42374
STATION_UUID="aebf6f56-9f93-4e34-9c7f-b9e9be51f0f5"
SENSOR_TYPE=48
CURRENT_STRUCTURE=2
HISTORIC_STRUCTURE=4
MPH_TO_MS=0.44704
HISTORY_HOURS=8
OUT=Path("work/svg_bundle.json")

S=requests.Session()
S.headers.update({"User-Agent":"mardorf-data-collector/1.0","Accept":"application/json"})
retry=Retry(total=4,connect=4,read=4,status=4,backoff_factor=1.0,
            status_forcelist=(429,500,502,503,504),
            allowed_methods=frozenset(["GET"]),raise_on_status=False)
adapter=HTTPAdapter(max_retries=retry,pool_connections=4,pool_maxsize=4)
S.mount("https://",adapter)

def get(path,key,secret,params=None):
    q={"api-key":key}
    if params:q.update(params)
    r=S.get(BASE+path,params=q,headers={"X-Api-Secret":secret},timeout=(10,45))
    r.raise_for_status()
    return r.json()

def f(v):
    try:return float(v)
    except (TypeError,ValueError):return None

def ts(v):
    try:return datetime.fromtimestamp(float(v),timezone.utc)
    except (TypeError,ValueError,OverflowError):return None

def mph(v):
    x=f(v);return round(x*MPH_TO_MS,4) if x is not None else None

def sector(v):
    x=f(v)
    return (x*22.5)%360 if x is not None and 0<=x<=15 else None

def sensor(payload,structure):
    for x in payload.get("sensors",[]) if isinstance(payload,dict) else []:
        if isinstance(x,dict) and x.get("sensor_type")==SENSOR_TYPE and x.get("data_structure_type")==structure:
            return x
    raise RuntimeError(f"sensor type {SENSOR_TYPE} / structure {structure} missing")

def normalize_current(payload):
    rows=sensor(payload,CURRENT_STRUCTURE).get("data") or []
    rows=[x for x in rows if isinstance(x,dict)]
    if not rows:raise RuntimeError("no current observation")
    r=max(rows,key=lambda x:x.get("ts",0));t=ts(r.get("ts"))
    if not t:raise RuntimeError("invalid current timestamp")
    return {
        "time_utc":t.isoformat(),
        "wind_speed_ms":mph(r.get("wind_speed_10_min_avg")),
        "wind_speed_instant_ms":mph(r.get("wind_speed")),
        "wind_gust_ms":mph(r.get("wind_gust_10_min")),
        "wind_direction_deg":f(r.get("wind_dir")),
        "source_semantics":{
            "wind_speed":"10_min_average","wind_speed_instant":"current_speed",
            "wind_gust":"10_min_gust","wind_direction":"degrees_of_compass",
        },
    }

def normalize_history(payload):
    out=[]
    for r in sensor(payload,HISTORIC_STRUCTURE).get("data") or []:
        if not isinstance(r,dict):continue
        t=ts(r.get("ts"))
        if not t:continue
        out.append({
            "time_utc":t.isoformat(),
            "wind_speed_ms":mph(r.get("wind_speed_avg")),
            "wind_gust_ms":mph(r.get("wind_speed_hi")),
            "wind_direction_deg":sector(r.get("wind_dir_of_prevail")),
            "wind_gust_direction_deg":sector(r.get("wind_dir_of_hi")),
            "wind_samples":int(r["wind_num_samples"]) if r.get("wind_num_samples") is not None else None,
            "source_semantics":{
                "wind_speed":"archive_interval_average","wind_gust":"archive_interval_high",
                "wind_direction":"16_sector_prevailing_direction",
                "wind_gust_direction":"16_sector_direction_of_high",
            },
        })
    return sorted(out,key=lambda x:x["time_utc"])

def main():
    key=os.getenv("WEATHERLINK_API_KEY");secret=os.getenv("WEATHERLINK_API_SECRET")
    if not key or not secret:raise RuntimeError("WeatherLink credentials are not configured")
    now=datetime.now(timezone.utc)
    current_raw=get(f"/current/{STATION_ID}",key,secret)
    start=now-timedelta(hours=HISTORY_HOURS)
    history_raw=get(f"/historic/{STATION_ID}",key,secret,{
        "start-timestamp":int(start.timestamp()),"end-timestamp":int(now.timestamp())})
    try:
        stations=get(f"/stations/{STATION_ID}",key,secret).get("stations") or []
        metadata=stations[0] if stations and isinstance(stations[0],dict) else {}
    except Exception as e:
        metadata={"metadata_error":f"{type(e).__name__}: {e}"}
    current=normalize_current(current_raw);history=normalize_history(history_raw)
    age=(now-datetime.fromisoformat(current["time_utc"])).total_seconds()/60
    bundle={
        "schema_version":1,
        "collector":"public-acquisition-only-v1",
        "retrieved_at_utc":now.isoformat(),
        "provider":"WeatherLink v2",
        "station":{"id":STATION_ID,"uuid":STATION_UUID,"name":"SVG"},
        "station_metadata":metadata,
        "latest_observation":current,
        "recent_historic_observations":history,
        "raw_source_payloads":{"current":current_raw,"historic":history_raw},
        "quality":{
            "observation_age_minutes":round(age,1),
            "quality_status":"fresh" if age<=15 else "delayed" if age<=60 else "stale",
            "historic_records":len(history),
        },
        "limitations":{
            "sensor_height_m":None,
            "sensor_height_status":"not_documented_in_machine_readable_metadata",
            "site_role":"south_shore_reference",
        },
    }
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(bundle,separators=(",",":"),ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps({
        "retrieved_at_utc":bundle["retrieved_at_utc"],
        "latest_observation_time_utc":current["time_utc"],
        "observation_age_minutes":round(age,1),
        "historic_records":len(history),
        "output_bytes":OUT.stat().st_size,
    }))

if __name__=="__main__":
    main()
