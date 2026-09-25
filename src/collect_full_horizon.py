#!/usr/bin/env python3
"""Collect each existing model's remaining native horizon into an archive sidecar."""
import hashlib, json, os, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
import requests
import extend_model_horizon as ext
from full_horizon_contract import VERSION, MODELS, utc, maximum_hours, extension_leads, validate_archive, gefs_lead_contract
SNAP=Path(os.getenv("COLLECTOR_MODEL_FILE","work/model_snapshot.json"))

class PublicationUnavailable(RuntimeError):
    """Required provider product is not yet available for the bound cycle."""

def now(): return datetime.now(timezone.utc).isoformat()
def noaa_requests(model,run,lead):
 day,hh=run.strftime("%Y%m%d"),run.strftime("%H")
 if model=="GFS":
  products=[("gfs_0p25","filter_gfs_0p25.pl",f"/gfs.{day}/{hh}/atmos",f"gfs.t{hh}z.pgrb2.0p25.f{lead:03d}",["UGRD","VGRD","GUST","APCP"],True)]
 else:
  contract=gefs_lead_contract(run,lead)
  if not contract["expected"]:
   raise ValueError(f"GEFS lead {lead} is not expected for {run:%HZ}; cycle maximum is {contract['expected_max_hours']} h")
  if contract["wind_product"]=="gefs_0p25s":
   products=[("gefs_0p25s","filter_gefs_atmos_0p25s.pl",f"/gefs.{day}/{hh}/atmos/pgrb2sp25",f"gec00.t{hh}z.pgrb2s.0p25.f{lead:03d}",["UGRD","VGRD","GUST","APCP"],True)]
  else:
   products=[("gefs_0p50a","filter_gefs_atmos_0p50a.pl",f"/gefs.{day}/{hh}/atmos/pgrb2ap5",f"gec00.t{hh}z.pgrb2a.0p50.f{lead:03d}",["UGRD","VGRD","APCP"],True),
             ("gefs_0p50b","filter_gefs_atmos_0p50b.pl",f"/gefs.{day}/{hh}/atmos/pgrb2bp5",f"gec00.t{hh}z.pgrb2b.0p50.f{lead:03d}",["GUST"],contract["gust_required"])]
 out=[]
 for product,script,directory,filename,variables,required in products:
  q={"file":filename,"dir":directory,"lev_10_m_above_ground":"on","lev_surface":"on","subregion":"","leftlon":"9.0418","rightlon":"9.6418","toplat":"52.7942","bottomlat":"52.1942"}; q.update({"var_"+v:"on" for v in variables})
  out.append((product,"https://nomads.ncep.noaa.gov/cgi-bin/"+script+"?"+urlencode(q),required))
 return out
def _download(session,url,required,far_gefs):
 # Same-cycle publication-aware retry. Never silently mix model cycles.
 attempts=2 if (required and far_gefs) else 1
 last=None
 for i in range(attempts):
  try:
   r=session.get(url,timeout=(10,45)); r.raise_for_status()
   if not r.content.startswith(b"GRIB"): raise ValueError("NOMADS response is not GRIB")
   return r.content
  except Exception as exc:
   last=exc
   if i+1<attempts: time.sleep(5*(i+1))
 raise PublicationUnavailable(str(last)) from last
def fetch_noaa(model,run,lead):
 vals,evidence,optional_errors,point={},[],[],None
 with requests.Session() as session,tempfile.TemporaryDirectory() as td:
  session.headers.update({"User-Agent":"mardorf-data-collector/full-horizon-v1"})
  for product,url,required in noaa_requests(model,run,lead):
   try:
    content=_download(session,url,required,model=="GEFS-control" and lead>240)
    path=Path(td)/(product+".grib2"); path.write_bytes(content)
    ext.assert_grib_valid_time(path,run,run+timedelta(hours=lead),f"{model} archive {lead}")
    rows=ext.nearest(path)
    if point is not None and point!=rows.point: raise ValueError("GEFS a/b extraction points differ")
    point=rows.point
    for name,step,value in rows: vals.setdefault(name,[]).append({"stepRange":step,"value":value})
    evidence.append({"product":product,"url":url,"sha256":hashlib.sha256(content).hexdigest()})
   except Exception as exc:
    if required:
     message=f"required product {product} unavailable for same cycle {run.isoformat()} lead {lead}: {type(exc).__name__}: {exc}"
     if isinstance(exc,PublicationUnavailable): raise PublicationUnavailable(message) from exc
     raise RuntimeError(message) from exc
    optional_errors.append({"product":product,"url":url,"type":type(exc).__name__,"reason":str(exc)[:400]})
 def one(*names):
  for n in names:
   if vals.get(n): return vals[n][0]["value"]
 u,v,gust=one("10u","u"),one("10v","v"),one("gust","10fg")
 if u is None or v is None: raise ValueError("required U/V absent from returned product")
 if gust is None and not(model=="GEFS-control" and lead>240): raise ValueError("required gust absent from returned product")
 return [{"model":model,"run_time_utc":run.isoformat(),"forecast_lead_hours":lead,"valid_time_utc":(run+timedelta(hours=lead)).isoformat(),"retrieved_at_utc":now(),"provider_product":"+".join(x["product"] for x in evidence),"source":"NOAA/NCEP NOMADS GRIB2 full horizon","source_urls":[x["url"] for x in evidence],"grib_evidence":evidence,"optional_product_errors":optional_errors,"field_availability":{"wind_uv":True,"gust":gust is not None},"forecast_coordinate_or_grid_point":point,"values":vals,"derived":ext.derived(u,v,gust)}]
def fetch_ifs(payload,leads):
 rows=ext.fetch_ifs(payload,requested_leads=leads)
 for r in rows:r.update(retrieved_at_utc=now(),provider_product="ifs_oper_fc_0p25")
 return rows
def checkpoint(payload,path):
 a=payload["full_horizon_archive"]
 for model,s in a["sources"].items():
  s["records"].sort(key=lambda r:r["forecast_lead_hours"])
  core_leads=[int(r["forecast_lead_hours"]) for r in payload.get("models",{}).get(model,[]) if r.get("derived") and r.get("forecast_lead_hours") is not None]
  archive_leads=[int(r["forecast_lead_hours"]) for r in s["records"] if r.get("derived")]
  s["actual_max_lead"]=max(core_leads+archive_leads) if core_leads or archive_leads else None
 a["coverage"]=validate_archive(payload);a["updated_at_utc"]=now();tmp=path.with_suffix(".tmp");tmp.write_text(json.dumps(payload,separators=(",",":"),allow_nan=False)+"\n");tmp.replace(path)
def collect(payload,path,workers=4):
 sources,jobs={},[]
 for model in MODELS:
  core=payload.get("models",{}).get(model,[]);source={"records":[],"errors":[],"lead_status":{}};sources[model]=source
  if not core:source["errors"].append({"reason":"parent_cycle_missing"});continue
  run=ext.cycle_from_existing(payload,model);leads=extension_leads(model,run);expected_max=maximum_hours(model,run)
  source.update(run_time_utc=run.isoformat(),target_max_hours=expected_max,expected_max_lead_for_cycle=expected_max,requested_extension_leads=leads)
  for h in leads:source["lead_status"][str(h)]={"status":"pending"}
  if model=="GEFS-control":
   for h in range(246,841,6):
    if h>expected_max:source["lead_status"][str(h)]={"status":"not_expected_for_cycle"}
  jobs.extend((model,leads[i:i+6]) for i in range(0,len(leads),6)) if model=="ECMWF-IFS" else jobs.extend((model,[h]) for h in leads)
 payload["full_horizon_archive"]={"method_version":VERSION,"started_at_utc":now(),"sources":sources};checkpoint(payload,path)
 with ThreadPoolExecutor(max_workers=workers) as pool:
  fs={pool.submit(fetch_ifs,payload,l) if m=="ECMWF-IFS" else pool.submit(fetch_noaa,m,utc(sources[m]["run_time_utc"]),l[0]):(m,l) for m,l in jobs}
  for f in as_completed(fs):
   m,l=fs[f]
   try:
    rr=f.result();prior=list(sources[m]["records"]);sources[m]["records"].extend(rr)
    for h in l:sources[m]["lead_status"][str(h)]={"status":"published","checked_at_utc":now()}
    try:validate_archive(payload)
    except Exception:
     sources[m]["records"]=prior
     for h in l:sources[m]["lead_status"][str(h)]={"status":"fetch_error","checked_at_utc":now(),"reason":"archive_validation_rejected_result"}
     raise
   except Exception as exc:
    status="not_yet_published" if isinstance(exc,PublicationUnavailable) else "fetch_error"
    for h in l:sources[m]["lead_status"][str(h)]={"status":status,"checked_at_utc":now(),"reason":str(exc)[:400]}
    sources[m]["errors"].append({"leads":l,"type":type(exc).__name__,"reason":str(exc)[:600],"retrieval_status":status})
   checkpoint(payload,path)
 return payload["full_horizon_archive"]["coverage"]
def main():
 p=json.loads(SNAP.read_text())
 if p.get("mode")=="test":raise RuntimeError("full-horizon collection is production-only")
 r=collect(p,SNAP);print(json.dumps(r,sort_keys=True))
 if r["status"]!="complete":raise SystemExit(1)
if __name__=="__main__":main()
