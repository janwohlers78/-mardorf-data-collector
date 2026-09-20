#!/usr/bin/env python3
"""Best-effort optional SKM/MeteoMap station 898 acquisition.

This source is legacy/diagnostic only. Failure must never block the primary SVG
observation path or model evaluation. MeteoMap chart timestamps are naive UTC.
"""
from __future__ import annotations
import json,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
import requests

STATION_ID=898
STATION_NAME="Segel-Klub Minden - Steinhuder Meer"
BASE="https://www.meteomap.cloud"
KINDS=("wind","gust","temperature","humidity","rain","pressure")
REQUIRED=("wind","gust")
FUTURE_TOLERANCE_MINUTES=15
OUT=Path("work/skm_bundle.json")
S=requests.Session()
S.headers.update({
    "User-Agent":"mardorf-data-collector/1.4 (+github-actions; optional SKM health probe)",
    "X-Requested-With":"XMLHttpRequest",
    "Accept":"application/json,text/plain,*/*",
})

def parse_source_time(value):
    try:
        return datetime.strptime(str(value),"%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None

def series(payload):
    x=(payload or {}).get("data",{}).get("data",[])
    return x if isinstance(x,list) else []

def request_chart(kind,day):
    url=f"{BASE}/{STATION_ID}/get/chart/{kind}"
    params={"tf":"current","datex":json.dumps({"from":day,"to":None},separators=(",",":"))}
    started=time.monotonic()
    diag={"kind":kind,"source_day_utc":day,"request_url":url}
    try:
        r=S.get(url,params=params,timeout=(10,35))
        diag.update(http_status=r.status_code,elapsed_seconds=round(time.monotonic()-started,3),
                    response_bytes=len(r.content),final_url=r.url)
        r.raise_for_status()
        payload=r.json()
        if not isinstance(payload,dict):
            raise RuntimeError("payload is not an object")
        if payload.get("error") not in (0,"0",False,None):
            raise RuntimeError(str(payload.get("message") or payload.get("error")))
        diag.update(success=True,exception_type=None,exception_message=None)
        return diag,payload
    except Exception as e:
        diag.update(success=False,elapsed_seconds=round(time.monotonic()-started,3),
                    exception_type=type(e).__name__,exception_message=str(e))
        return diag,None

def nonfuture_rows(payload,now):
    cutoff=now+timedelta(minutes=FUTURE_TOLERANCE_MINUTES)
    out=[];future=[]
    for row in series(payload):
        if not isinstance(row,dict):continue
        t=parse_source_time(row.get("x"))
        if t is None:continue
        if t>cutoff:
            future.append({"time_utc":t.isoformat(),"value":row.get("y")})
            continue
        out.append({"time_utc":t.isoformat(),"value":row.get("y")})
    return out,future

def main():
    now=datetime.now(timezone.utc)
    current=now.strftime("%Y-%m-%d")
    previous=(now.date()-timedelta(days=1)).isoformat()
    bundle={
        "schema_version":1,
        "retrieved_at_utc":now.isoformat(),
        "provider":"MeteoMap.cloud",
        "station":{"id":STATION_ID,"name":STATION_NAME,"sensor_height_m":3.0,
                   "latitude":52.48972001,"longitude":9.31819998},
        "source_timestamp_timezone":"UTC",
        "source_day_window_timezone":"UTC",
        "role":"optional_legacy_north_shore_diagnostic_never_primary",
        "requests":[],
        "windows":{},
        "selected_day_by_endpoint":{},
        "endpoint_summary":{},
    }

    # Always try current UTC day for every useful field.
    bundle["windows"][current]={}
    for kind in KINDS:
        diag,payload=request_chart(kind,current)
        bundle["requests"].append(diag)
        if payload is not None:
            bundle["windows"][current][kind]=payload

    # If either required endpoint has no plausible current-day rows, retry only
    # the required pair for previous UTC day. This is bounded and non-blocking.
    current_required={}
    for kind in REQUIRED:
        rows,_=nonfuture_rows(bundle["windows"][current].get(kind),now)
        current_required[kind]=rows
    if any(not current_required[k] for k in REQUIRED):
        bundle["windows"][previous]={}
        for kind in REQUIRED:
            diag,payload=request_chart(kind,previous)
            bundle["requests"].append(diag)
            if payload is not None:
                bundle["windows"][previous][kind]=payload

    for kind in KINDS:
        candidates=[]
        for day in (current,previous):
            payload=(bundle["windows"].get(day) or {}).get(kind)
            rows,future=nonfuture_rows(payload,now)
            if rows:
                candidates.append((day,rows,future))
        selected=max(candidates,key=lambda x:x[1][-1]["time_utc"]) if candidates else None
        if selected:
            day,rows,future=selected
            latest=rows[-1]
            t=datetime.fromisoformat(latest["time_utc"])
            bundle["selected_day_by_endpoint"][kind]=day
            bundle["endpoint_summary"][kind]={
                "success":True,"source_day_utc":day,"record_count_nonfuture":len(rows),
                "first_time_utc":rows[0]["time_utc"],"last_time_utc":latest["time_utc"],
                "latest_value":latest.get("value"),
                "observation_age_minutes":round((now-t).total_seconds()/60,2),
                "future_points_ignored":len(future),
            }
        else:
            req=[x for x in bundle["requests"] if x.get("kind")==kind]
            bundle["endpoint_summary"][kind]={
                "success":False,"record_count_nonfuture":0,
                "request_attempts":req,
            }

    wind=bundle["endpoint_summary"].get("wind") or {}
    gust=bundle["endpoint_summary"].get("gust") or {}
    bundle["latest_required_observation"]={
        "wind_time_utc":wind.get("last_time_utc"),"wind_value_kmh":wind.get("latest_value"),
        "gust_time_utc":gust.get("last_time_utc"),"gust_value_kmh":gust.get("latest_value"),
    }
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(bundle,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps({
        "station":STATION_ID,
        "wind":bundle["endpoint_summary"].get("wind"),
        "gust":bundle["endpoint_summary"].get("gust"),
        "request_failures":[x for x in bundle["requests"] if not x.get("success")],
        "output":str(OUT),
    },indent=2,ensure_ascii=False))

if __name__=="__main__":
    main()
