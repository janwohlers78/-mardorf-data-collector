#!/usr/bin/env python3
"""Fetch DWD station 05715 (Wunstorf) hourly wind archive for public transfer."""
import csv,io,json,time,zipfile
from datetime import datetime,timedelta,timezone
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

STATION_ID="05715"
STATION_NAME="Wunstorf"
SOURCE_URL=f"https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/wind_synop/recent/stundenwerte_F_{STATION_ID}_akt.zip"
UA="mardorf-data-collector/1.0 (+github-actions; DWD Wunstorf reference)"
RECENT_REFERENCE_MINUTES=180
PLAUSIBLE_ARCHIVE_LAG_MINUTES=48*60
FUTURE_TOLERANCE_MINUTES=15
OUT=Path("work/wunstorf_bundle.json")
S=requests.Session();S.headers.update({"User-Agent":UA})
retry=Retry(total=4,connect=4,read=4,status=4,backoff_factor=1.0,status_forcelist=(429,500,502,503,504),allowed_methods=frozenset(["GET"]),raise_on_status=False)
S.mount("https://",HTTPAdapter(max_retries=retry))

def utcnow():return datetime.now(timezone.utc)
def parse_dt(s):
    s=str(s).strip()
    for fmt in ("%Y%m%d%H","%Y%m%d%H%M"):
        try:return datetime.strptime(s,fmt).replace(tzinfo=timezone.utc)
        except ValueError:pass
    raise ValueError(s)
def fnum(v):
    try:
        x=float(str(v).strip().replace(",","."))
        return None if x<=-900 else x
    except Exception:return None
def pick(row,*names):
    norm={str(k).strip().upper():v for k,v in row.items()}
    for n in names:
        if n.upper() in norm:return norm[n.upper()]
    return None
def classify_archive_age(now,latest):
    age=(now-latest).total_seconds()/60
    if age < -FUTURE_TOLERANCE_MINUTES:return age,"timestamp_validation_failure",False
    age=max(0.0,age)
    if age<=RECENT_REFERENCE_MINUTES:return age,"recent_historical_reference",True
    if age<=PLAUSIBLE_ARCHIVE_LAG_MINUTES:return age,"provider_archive_lag_plausible",True
    return age,"provider_archive_unusually_delayed",True

def main():
    now=utcnow();started=time.monotonic()
    r=S.get(SOURCE_URL,timeout=(10,60))
    elapsed=round(time.monotonic()-started,3);r.raise_for_status()
    z=zipfile.ZipFile(io.BytesIO(r.content))
    candidates=[n for n in z.namelist() if n.lower().endswith(".txt") and ("produkt_" in n.lower() or "product_" in n.lower())]
    if not candidates:candidates=[n for n in z.namelist() if n.lower().endswith(".txt")]
    rows=[]
    for name in candidates:
        text=z.read(name).decode("latin-1",errors="replace")
        tmp=[]
        for row in csv.DictReader(io.StringIO(text),delimiter=";"):
            ts=pick(row,"MESS_DATUM","MESS_DATUM_WOZ")
            if not ts:continue
            try:t=parse_dt(ts)
            except Exception:continue
            speed=fnum(pick(row,"F","FF"));direction=fnum(pick(row,"D","DD"))
            if speed is None or direction is None:continue
            tmp.append({"time_utc":t.isoformat(),"wind_speed_ms":speed,"wind_direction_deg":direction})
        if len(tmp)>len(rows):rows=tmp
    if not rows:raise RuntimeError("No usable Wunstorf wind rows parsed from DWD archive")
    rows.sort(key=lambda x:x["time_utc"])
    cutoff=now-timedelta(days=21)
    rows=[x for x in rows if datetime.fromisoformat(x["time_utc"])>=cutoff]
    if not rows:raise RuntimeError("DWD archive parsed but no observations within last 21 days")
    latest=rows[-1];latest_dt=datetime.fromisoformat(latest["time_utc"]).astimezone(timezone.utc)
    age,status,valid=classify_archive_age(now,latest_dt)
    payload={
      "schema_version":2,"retrieved_at_utc":now.isoformat(),"provider":"DWD","dataset":"hourly_wind_synop_recent",
      "source_role":"historical_land_reference_not_operational_realtime_source",
      "station":{"id":STATION_ID,"name":STATION_NAME},"source_url":SOURCE_URL,
      "request_diagnostics":{"success":True,"http_status":r.status_code,"elapsed_seconds":elapsed,
                             "response_bytes":len(r.content),"archive_member_count":len(z.namelist())},
      "latest_observation_time_utc":latest["time_utc"],"latest_observation":latest,"observations":rows,
      "quality":{"records":len(rows),"success":bool(valid),"observation_age_minutes":round(max(0.0,age),1),
                 "availability_status":status,"timestamp_validation":"ok" if valid else "source_timestamp_in_future",
                 "interpretation":"ETNW METAR is the current Wunstorf redundancy; this CDC feed is retained for historical verification and may publish with substantial lag."}
    }
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(payload,indent=2,ensure_ascii=False,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({"latest_observation_time_utc":latest["time_utc"],"quality":payload["quality"]},indent=2))

if __name__=="__main__":main()
