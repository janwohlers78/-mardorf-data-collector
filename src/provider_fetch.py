#!/usr/bin/env python3
"""Fetch exactly one model provider/stage and persist progress immediately.

This wrapper isolates provider failures so one slow source cannot prevent later
sources from being attempted. External shell timeout handles truly hung clients.
"""
from __future__ import annotations
import argparse,json,os,time
from datetime import datetime,timezone
from pathlib import Path

import fetch_model_data as base
import fetch_extra_models as extra
import fetch_dwd_additional_models as dwd
import extend_model_horizon as ext

SNAP=Path(os.getenv("COLLECTOR_MODEL_FILE","work/model_snapshot.json"))

def now():
    return datetime.now(timezone.utc).isoformat()

def initial(test):
    return {
        "schema_version":2,
        "retrieved_at_utc":now(),
        "spot":{"lat":base.LAT,"lon":base.LON},
        "mode":"test" if test else "production",
        "leads_hours":[],
        "models":{},
        "quality":{"errors":[]},
        "provider_attempts":[],
    }

def load(test):
    if SNAP.exists():
        d=json.loads(SNAP.read_text(encoding="utf-8"))
        d.setdefault("models",{});d.setdefault("quality",{});d["quality"].setdefault("errors",[])
        d.setdefault("provider_attempts",[])
        return d
    d=initial(test);SNAP.parent.mkdir(parents=True,exist_ok=True)
    SNAP.write_text(json.dumps(d,separators=(",",":"))+"\n",encoding="utf-8")
    return d

def save(d):
    all_leads=sorted({int(r["forecast_lead_hours"]) for rows in d.get("models",{}).values() for r in rows if r.get("forecast_lead_hours") is not None})
    d["leads_hours"]=all_leads
    d["retrieved_at_utc"]=now()
    SNAP.parent.mkdir(parents=True,exist_ok=True)
    SNAP.write_text(json.dumps(d,separators=(",",":"))+"\n",encoding="utf-8")

def base_leads(model,test):
    if test:
        return [0,12,24,30,36,42,48] if model in ("ICON-D2","GFS") else [0,12,24,36,48]
    return list(range(0,49,3))

def quality_one(d,model,expected):
    rows=d.get("models",{}).get(model,[])
    got={int(r["forecast_lead_hours"]) for r in rows if r.get("derived") and r.get("forecast_lead_hours") is not None}
    complete=all(x in got for x in expected) and len(got)>=len(expected)
    d["quality"][model]={
        "records":len(rows),"derived_records":sum(bool(r.get("derived")) for r in rows),
        "success":complete,"complete_requested_horizon":complete,
        "expected_lead_count":len(expected),"max_derived_lead_hours":max(got) if got else None,
    }
    success=[n for n,q in d["quality"].items() if isinstance(q,dict) and q.get("success")]
    d["quality"]["successful_models"]=success
    families={("DWD-ICON" if n.startswith("ICON-") else "GFS" if n in ("GFS","GEFS-control") else n) for n in success}
    d["quality"]["minimum_two_independent_models_met"]=len(families)>=2

def fetch_base(d,model,test):
    leads=base_leads(model,test)
    if model=="ICON-D2":
        rows=base.derive(base.fetch_icon(leads))
    elif model=="GFS":
        rows=base.derive(base.fetch_gfs(leads))
    elif model=="ECMWF-IFS":
        rows=extra.fetch_ifs(leads)
    elif model=="GEFS-control":
        rows=extra.fetch_gefs(leads)
    elif model=="ICON-EU":
        rows=dwd.fetch_icon_eu(leads,required_cycle_lead=None if test else 120)
    elif model=="ICON-D2-EPS":
        rows=dwd.fetch_icon_d2_eps(leads)
    else:
        raise ValueError(model)
    d["models"][model]=rows
    quality_one(d,model,leads)

def fetch_extension(d,model):
    if model=="GFS":
        new=ext.fetch_noaa(d,"GFS",False)
    elif model=="GEFS-control":
        new=ext.fetch_noaa(d,"GEFS-control",True)
    elif model=="ECMWF-IFS":
        new=ext.fetch_ifs(d)
    elif model=="ICON-EU":
        new=ext.fetch_icon_eu(d)
    else:
        raise ValueError(f"extension unsupported for {model}")
    old=[r for r in d["models"].get(model,[]) if int(r.get("forecast_lead_hours",9999))<=48]
    d["models"][model]=old+new
    ext.quality(d)
    d["horizon_extension_retrieved_at_utc"]=now()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--model",required=True,choices=("ICON-D2","GFS","ECMWF-IFS","GEFS-control","ICON-EU","ICON-D2-EPS"))
    ap.add_argument("--stage",required=True,choices=("base","extension"))
    ap.add_argument("--test",action="store_true")
    args=ap.parse_args()
    d=load(args.test)
    started=datetime.now(timezone.utc);source=os.getenv("ECMWF_OPEN_DATA_SOURCE") if args.model=="ECMWF-IFS" else None
    attempt={"model":args.model,"stage":args.stage,"status":"started","started_at_utc":started.isoformat()}
    if source:attempt["mirror"]=source
    d["provider_attempts"].append(attempt);save(d)
    try:
        if args.stage=="base":
            fetch_base(d,args.model,args.test)
        else:
            if args.test: raise RuntimeError("extension stage is not used in test mode")
            fetch_extension(d,args.model)
        ended=datetime.now(timezone.utc)
        attempt.update(status="success",completed_at_utc=ended.isoformat(),duration_seconds=round((ended-started).total_seconds(),3))
        save(d)
        print(json.dumps({"model":args.model,"stage":args.stage,"status":"success","mirror":source,"duration_seconds":attempt["duration_seconds"],"records":len(d["models"].get(args.model,[]))}))
    except Exception as e:
        ended=datetime.now(timezone.utc)
        attempt.update(status="failed",completed_at_utc=ended.isoformat(),duration_seconds=round((ended-started).total_seconds(),3),
                       exception_type=type(e).__name__,exception_message=str(e))
        save(d)
        print(json.dumps({"model":args.model,"stage":args.stage,"status":"failed","mirror":source,
                          "exception_type":type(e).__name__,"exception_message":str(e)},ensure_ascii=False))
        raise

if __name__=="__main__":
    main()
