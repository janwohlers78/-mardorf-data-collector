#!/usr/bin/env python3
"""Acquire recent SVG WeatherLink observations into one ephemeral transfer bundle.

Every attempted endpoint records exact HTTP/exception diagnostics. The bundle is
written even when acquisition fails so the integrity stage can report the failed
attempt precisely and transfer that failure report to the private repository.
"""
from __future__ import annotations
import json,os,time
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
S.headers.update({"User-Agent":"mardorf-data-collector/1.1","Accept":"application/json"})
retry=Retry(total=4,connect=4,read=4,status=4,backoff_factor=1.0,
            status_forcelist=(429,500,502,503,504),
            allowed_methods=frozenset(["GET"]),raise_on_status=False)
adapter=HTTPAdapter(max_retries=retry,pool_connections=4,pool_maxsize=4)
S.mount("https://",adapter)

def request(path,key,secret,params=None):
    diag={"request_path":path,"success":False,"http_status":None,"elapsed_seconds":None,
          "response_bytes":None,"exception_type":None,"exception_message":None}
    if not key or not secret:
        absent=[]
        if not key:absent.append("WEATHERLINK_API_KEY")
        if not secret:absent.append("WEATHERLINK_API_SECRET")
        diag["exception_type"]="ConfigurationError"
        diag["exception_message"]="Required GitHub Actions secret(s) not configured: "+", ".join(absent)
        return None,diag
    q={"api-key":key}
    if params:q.update(params)
    t0=time.monotonic()
    try:
        r=S.get(BASE+path,params=q,headers={"X-Api-Secret":secret},timeout=(10,45))
        diag["elapsed_seconds"]=round(time.monotonic()-t0,3)
        diag["http_status"]=r.status_code
        diag["response_bytes"]=len(r.content)
        r.raise_for_status()
        payload=r.json()
        diag["success"]=True
        return payload,diag
    except Exception as e:
        diag["elapsed_seconds"]=round(time.monotonic()-t0,3)
        diag["exception_type"]=type(e).__name__
        diag["exception_message"]=str(e)
        return None,diag

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
    if not rows:raise RuntimeError("current endpoint contains no sensor data record")
    r=max(rows,key=lambda x:x.get("ts",0));t=ts(r.get("ts"))
    if not t:raise RuntimeError("current sensor record has an invalid timestamp")
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
    now=datetime.now(timezone.utc);start=now-timedelta(hours=HISTORY_HOURS)
    current_raw,current_diag=request(f"/current/{STATION_ID}",key,secret)
    history_raw,history_diag=request(f"/historic/{STATION_ID}",key,secret,{
        "start-timestamp":int(start.timestamp()),"end-timestamp":int(now.timestamp())})
    stations_raw,stations_diag=request(f"/stations/{STATION_ID}",key,secret)

    normalization={}
    current=None;history=[];metadata={}
    if current_raw is not None:
        try:current=normalize_current(current_raw);normalization["current"]={"success":True}
        except Exception as e:normalization["current"]={"success":False,"exception_type":type(e).__name__,"exception_message":str(e)}
    else:
        normalization["current"]={"success":False,"exception_type":"SourceRequestUnavailable","exception_message":"Current endpoint did not return a payload."}
    if history_raw is not None:
        try:history=normalize_history(history_raw);normalization["historic"]={"success":True,"record_count":len(history)}
        except Exception as e:normalization["historic"]={"success":False,"exception_type":type(e).__name__,"exception_message":str(e),"record_count":0}
    else:
        normalization["historic"]={"success":False,"exception_type":"SourceRequestUnavailable","exception_message":"Historic endpoint did not return a payload.","record_count":0}
    if isinstance(stations_raw,dict):
        stations=stations_raw.get("stations") or []
        if stations and isinstance(stations[0],dict):metadata=stations[0]

    age=None
    if current and current.get("time_utc"):
        age=round((now-datetime.fromisoformat(current["time_utc"])).total_seconds()/60,2)
    bundle={
        "schema_version":2,"collector":"public-acquisition-only-v2",
        "retrieved_at_utc":now.isoformat(),"provider":"WeatherLink v2",
        "station":{"id":STATION_ID,"uuid":STATION_UUID,"name":"SVG"},
        "requested_history_window":{"start_utc":start.isoformat(),"end_utc":now.isoformat(),"hours":HISTORY_HOURS},
        "request_diagnostics":{"current":current_diag,"historic":history_diag,"stations":stations_diag},
        "normalization_diagnostics":normalization,
        "station_metadata":metadata,
        "latest_observation":current,
        "recent_historic_observations":history,
        "raw_source_payloads":{"current":current_raw,"historic":history_raw},
        "measurement_summary":{
            "current_observation_age_minutes":age,
            "historic_record_count":len(history),
            "current_fields_present":sorted(k for k,v in (current or {}).items() if v is not None),
        },
        "limitations":{"sensor_height_m":None,"sensor_height_status":"not_documented_in_machine_readable_metadata","site_role":"south_shore_reference"},
    }
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(bundle,separators=(",",":"),ensure_ascii=False)+"\n",encoding="utf-8")
    ok=bool(current_diag["success"] and history_diag["success"] and normalization["current"]["success"] and normalization["historic"]["success"])
    print(json.dumps({
        "retrieved_at_utc":bundle["retrieved_at_utc"],"current_request":current_diag,
        "historic_request":history_diag,"stations_request":stations_diag,
        "normalization":normalization,"current_observation_age_minutes":age,
        "historic_records":len(history),"output_bytes":OUT.stat().st_size,
    }))
    if not ok:raise SystemExit(2)

if __name__=="__main__":
    main()
