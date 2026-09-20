#!/usr/bin/env python3
"""Fetch ETNW METAR from AviationWeather.gov for public transfer."""
import json,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ICAO="ETNW"
URL="https://aviationweather.gov/api/data/metar"
UA="mardorf-data-collector/1.0 (+github-actions; ETNW METAR ingest)"
FRESH_MINUTES=120
FUTURE_TOLERANCE_MINUTES=15
OUT=Path("work/etnw_bundle.json")
WUNSTORF=Path("work/wunstorf_bundle.json")
S=requests.Session();S.headers.update({"User-Agent":UA,"Accept":"application/json"})
retry=Retry(total=4,connect=4,read=4,status=4,backoff_factor=1.0,status_forcelist=(429,500,502,503,504),allowed_methods=frozenset(["GET"]),raise_on_status=False)
S.mount("https://",HTTPAdapter(max_retries=retry))

def utcnow():return datetime.now(timezone.utc)
def parse_time(v):
    if v is None:return None
    if isinstance(v,(int,float)):
        if v>1e12:v=v/1000
        return datetime.fromtimestamp(v,timezone.utc)
    s=str(v).strip().replace("Z","+00:00");dt=datetime.fromisoformat(s)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
def num(v):
    try:return float(v)
    except Exception:return None
def circular_error(a,b):
    if a is None or b is None:return None
    d=abs((a-b)%360.0);return min(d,360.0-d)

def dwd_crosscheck(latest):
    if not WUNSTORF.exists():return {"status":"unavailable","reason":"current_public_wunstorf_bundle_missing"}
    try:
        d=json.loads(WUNSTORF.read_text(encoding="utf-8"));rows=d.get("observations") or []
        target=parse_time(latest.get("time_utc"));candidates=[]
        for x in rows:
            try:t=parse_time(x.get("time_utc"))
            except Exception:continue
            if t:candidates.append((abs((t-target).total_seconds()),t,x))
        if not candidates:return {"status":"unavailable","reason":"no_parseable_dwd_rows"}
        delta_s,t,x=min(candidates,key=lambda z:z[0])
        if delta_s>90*60:return {"status":"no_same_time_crosscheck","reason":"nearest_DWD_CDC_row_more_than_90_min_away","time_delta_minutes":round(delta_s/60,1),"dwd_source_quality":d.get("quality")}
        met_spd=latest.get("wind_speed_ms");dwd_spd=num(x.get("wind_speed_ms"))
        met_dir=latest.get("wind_direction_deg");dwd_dir=num(x.get("wind_direction_deg"))
        dd=circular_error(met_dir,dwd_dir)
        return {"status":"matched_nearest_time","metar_time_utc":target.isoformat(),"dwd_time_utc":t.isoformat(),
                "time_delta_minutes":round(delta_s/60,1),"metar_wind_speed_ms":met_spd,"dwd_wind_speed_ms":dwd_spd,
                "speed_difference_metar_minus_dwd_ms":round(met_spd-dwd_spd,3) if met_spd is not None and dwd_spd is not None else None,
                "metar_direction_deg":met_dir,"dwd_direction_deg":dwd_dir,
                "circular_direction_difference_deg":round(dd,1) if dd is not None else None,
                "dwd_source_quality":d.get("quality"),
                "interpretation":"Informational only. ETNW is current redundancy; DWD CDC is a historical reference and may publish later."}
    except Exception as e:return {"status":"error","error":f"{type(e).__name__}: {e}"}

def main():
    now=utcnow();started=time.monotonic()
    r=S.get(URL,params={"ids":ICAO,"format":"json","hours":"24"},timeout=(10,35))
    elapsed=round(time.monotonic()-started,3)
    if r.status_code==204:raise RuntimeError("AviationWeather returned 204: no ETNW METAR data")
    r.raise_for_status();rows=r.json() or []
    if not isinstance(rows,list):raise RuntimeError("AviationWeather METAR payload is not a list")
    normalized_by_time={};rejected=0;future_rejected=0;future_cutoff=now+timedelta(minutes=FUTURE_TOLERANCE_MINUTES)
    for x in rows:
        if not isinstance(x,dict):rejected+=1;continue
        if x.get("icaoId") not in (None,ICAO):rejected+=1;continue
        t=None
        for k in ("obsTime","reportTime","receiptTime"):
            try:
                if x.get(k) is not None:t=parse_time(x.get(k));break
            except Exception:pass
        if not t:rejected+=1;continue
        if t>future_cutoff:future_rejected+=1;continue
        wspd=num(x.get("wspd"));wgst=num(x.get("wgst"));wdir=num(x.get("wdir"));raw=x.get("rawOb") or ""
        variable=(" VRB" in f" {raw} ") or (x.get("wdir") is not None and wdir is None)
        rec={"time_utc":t.isoformat(),"wind_direction_deg":wdir,"wind_direction_variable":bool(variable),
             "wind_speed_kt":wspd,"wind_speed_ms":round(wspd*0.514444,3) if wspd is not None else None,
             "wind_gust_kt":wgst,"wind_gust_ms":round(wgst*0.514444,3) if wgst is not None else None,
             "raw_metar":x.get("rawOb"),
             "source_fields":{k:x.get(k) for k in ("icaoId","obsTime","reportTime","wdir","wspd","wgst") if k in x}}
        normalized_by_time[rec["time_utc"]]=rec
    normalized=[normalized_by_time[k] for k in sorted(normalized_by_time)]
    if not normalized:raise RuntimeError("ETNW API response contained no plausible nonfuture observations")
    latest=normalized[-1];latest_dt=parse_time(latest["time_utc"])
    age=max(0.0,round((now-latest_dt).total_seconds()/60,1));gaps=[]
    for a,b in zip(normalized,normalized[1:]):
        gaps.append((parse_time(b["time_utc"])-parse_time(a["time_utc"])).total_seconds()/60)
    speed_present=sum(x.get("wind_speed_ms") is not None for x in normalized)
    direction_present=sum((x.get("wind_direction_deg") is not None) or x.get("wind_direction_variable") for x in normalized)
    availability="fresh" if age<=FRESH_MINUTES else ("delayed" if age<=360 else "stale")
    quality={"records":len(normalized),"source_rows":len(rows),"rejected_rows":rejected,
             "future_timestamp_rows_rejected":future_rejected,"success":True,"age_minutes":age,
             "availability_status":availability,"fresh_within_120_min":age<=FRESH_MINUTES,
             "timestamp_validation":"ok","wind_speed_availability_pct":round(100*speed_present/len(normalized),1),
             "direction_or_variable_availability_pct":round(100*direction_present/len(normalized),1),
             "max_inter_report_gap_minutes":round(max(gaps),1) if gaps else None,
             "gust_note":"Gust is optional in METAR and absence is not treated as a data-quality failure."}
    payload={"schema_version":3,"retrieved_at_utc":now.isoformat(),"provider":"AviationWeather.gov",
             "source_role":"current_operational_Wunstorf_redundancy","station":{"icao":ICAO,"name":"Fliegerhorst Wunstorf"},
             "source_url":r.url,"request_diagnostics":{"success":True,"http_status":r.status_code,
             "elapsed_seconds":elapsed,"response_bytes":len(r.content)},
             "units_note":"AviationWeather JSON wspd/wgst are knots; m/s values are explicit conversions using 1 kt = 0.514444 m/s.",
             "latest_observation_time_utc":latest["time_utc"],"latest_observation":latest,"observations":normalized,
             "quality":quality,"dwd_05715_crosscheck":dwd_crosscheck(latest)}
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(payload,indent=2,ensure_ascii=False,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({"latest":latest,"quality":quality,"dwd_05715_crosscheck":payload["dwd_05715_crosscheck"]},indent=2))

if __name__=="__main__":main()
