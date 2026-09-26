#!/usr/bin/env python3
"""Collect each existing model's remaining native horizon into an archive sidecar."""
import hashlib, json, os, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
import requests
import extend_model_horizon as ext
import noaa_weather_context as noaa
import availability_contract as availability
from full_horizon_contract import VERSION, MODELS, utc, maximum_hours, compatibility_hours, extension_leads, acquisition_policy, validate_archive, gefs_lead_contract
SNAP=Path(os.getenv("COLLECTOR_MODEL_FILE","work/model_snapshot.json"))

class PublicationUnavailable(RuntimeError):
    """Required provider product is not yet available for the bound cycle."""

def now(): return datetime.now(timezone.utc).isoformat()
def noaa_requests(model,run,lead):
 day,hh=run.strftime("%Y%m%d"),run.strftime("%H")
 contract=None
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
             # pgrb2b is used for provider-native weather context only. NOMADS
             # returns HTTP 500 when the historically attempted far-range GUST
             # flag is combined with otherwise valid DPT/CAPE/CIN selections.
             # Far-range gust therefore remains explicitly unavailable/optional.
             ("gefs_0p50b","filter_gefs_atmos_0p50b.pl",f"/gefs.{day}/{hh}/atmos/pgrb2bp5",f"gec00.t{hh}z.pgrb2b.0p50.f{lead:03d}",[],False)]
 out=[]
 for product,script,directory,filename,variables,required in products:
  pad=contract["subset_padding_degrees"] if contract is not None else 0.30
  q={"file":filename,"dir":directory,"subregion":"",
     "leftlon":f"{ext.LON-pad:.4f}","rightlon":f"{ext.LON+pad:.4f}",
     "toplat":f"{ext.LAT+pad:.4f}","bottomlat":f"{ext.LAT-pad:.4f}"}; q.update({"var_"+v:"on" for v in variables})
  # Wind-bearing products need their native wind/surface levels. The GEFS
  # secondary pgrb2b weather product does not support 10 m AGL; sending that
  # inherited flag makes NOMADS return HTTP 500 even though DPT/CAPE/CIN are
  # available. Its valid levels are added by the product-specific registry.
  if product!="gefs_0p50b":
   q.update({"lev_10_m_above_ground":"on","lev_surface":"on"})
  noaa.add_weather_flags(q,product)
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
 vals,evidence,optional_errors,point,weather_availability={},[],[],None,{}
 with requests.Session() as session,tempfile.TemporaryDirectory() as td:
  session.headers.update({"User-Agent":"mardorf-data-collector/full-horizon-v1"})
  for product,url,required in noaa_requests(model,run,lead):
   try:
    content=_download(session,url,required,model=="GEFS-control" and lead>240)
    path=Path(td)/(product+".grib2"); path.write_bytes(content)
    ext.assert_grib_valid_time(path,run,run+timedelta(hours=lead),f"{model} archive {lead}")
    source_sha=hashlib.sha256(content).hexdigest()
    product_values,product_point=noaa.extract_native_values(
     path,ext.LAT,ext.LON,source_sha256=source_sha,product=product)
    if point is not None and point!=product_point: raise ValueError("GEFS a/b extraction points differ")
    point=product_point
    for name,items in product_values.items(): vals.setdefault(name,[]).extend(items)
    weather_availability[product]=noaa.weather_availability(product,product_values)
    evidence.append({"product":product,"url":url,"sha256":source_sha,"response_bytes":len(content)})
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
 return [{"model":model,"run_time_utc":run.isoformat(),"forecast_lead_hours":lead,"valid_time_utc":(run+timedelta(hours=lead)).isoformat(),"retrieved_at_utc":now(),"provider_product":"+".join(x["product"] for x in evidence),"source":"NOAA/NCEP NOMADS GRIB2 full horizon","source_urls":[x["url"] for x in evidence],"grib_evidence":evidence,"optional_product_errors":optional_errors,"field_availability":{"wind_uv":True,"gust":gust is not None},"weather_context_availability":weather_availability,"forecast_coordinate_or_grid_point":point,"values":vals,"derived":ext.derived(u,v,gust)}]
PGRB2B_RETRY_DELAY_HOURS=3
PGRB2B_MAX_RETRIES=1

def pgrb2b_missing_leads(source):
 out=[]
 for record in source.get("records",[]):
  errors=record.get("optional_product_errors") if isinstance(record.get("optional_product_errors"),list) else []
  if any(isinstance(x,dict) and x.get("product")=="gefs_0p50b" for x in errors):
   try: out.append(int(record["forecast_lead_hours"]))
   except Exception: pass
 return sorted(set(out))

def retry_gefs_pgrb2b(record,run):
 lead=int(record["forecast_lead_hours"])
 candidates=[x for x in noaa_requests("GEFS-control",run,lead) if x[0]=="gefs_0p50b"]
 if len(candidates)!=1:
  raise RuntimeError(f"GEFS pgrb2b request identity missing at lead {lead}: {candidates}")
 product,url,_required=candidates[0]
 out=json.loads(json.dumps(record))
 with requests.Session() as session,tempfile.TemporaryDirectory() as td:
  session.headers.update({"User-Agent":"mardorf-data-collector/gefs-pgrb2b-supplement-v1"})
  content=_download(session,url,False,True)
  path=Path(td)/(product+".grib2");path.write_bytes(content)
  ext.assert_grib_valid_time(path,run,run+timedelta(hours=lead),f"GEFS supplemental pgrb2b {lead}")
  source_sha=hashlib.sha256(content).hexdigest()
  product_values,product_point=noaa.extract_native_values(
   path,ext.LAT,ext.LON,source_sha256=source_sha,product=product)
 existing_point=out.get("forecast_coordinate_or_grid_point")
 if isinstance(existing_point,dict) and existing_point!=product_point:
  raise ValueError("GEFS supplemental pgrb2b extraction point differs from archived 0.50a point")
 vals=out.setdefault("values",{})
 for name,items in product_values.items():
  vals[name]=items
 availability_map=out.setdefault("weather_context_availability",{})
 availability_map[product]=noaa.weather_availability(product,product_values)
 evidence=[x for x in (out.get("grib_evidence") or []) if not(isinstance(x,dict) and x.get("product")==product)]
 evidence.append({"product":product,"url":url,"sha256":source_sha,"response_bytes":len(content)})
 out["grib_evidence"]=evidence
 errors=[x for x in (out.get("optional_product_errors") or []) if not(isinstance(x,dict) and x.get("product")==product)]
 out["optional_product_errors"]=errors
 urls=[x for x in (out.get("source_urls") or []) if x!=url]
 urls.append(url);out["source_urls"]=urls
 out["provider_product"]="+".join(x["product"] for x in evidence if isinstance(x,dict) and x.get("product"))
 out["retrieved_at_utc"]=now()
 availability.stamp_rows([out],observed_at=out["retrieved_at_utc"],replace_row_time=True)
 return out

def update_pgrb2b_retry_state(source,action,attempt_results=None):
 missing=pgrb2b_missing_leads(source)
 previous=source.get("gefs_pgrb2b_supplemental_retry")
 previous=previous if isinstance(previous,dict) else {}
 attempts=int(previous.get("attempts",0) or 0)
 if action=="supplemental_retry":
  attempts+=1
 status="not_needed";next_retry=None
 if missing:
  if attempts<PGRB2B_MAX_RETRIES:
   status="pending"
   next_retry=(datetime.now(timezone.utc)+timedelta(hours=PGRB2B_RETRY_DELAY_HOURS)).isoformat()
  else:
   status="exhausted"
 elif action=="supplemental_retry":
  status="complete"
 source["gefs_pgrb2b_supplemental_retry"]={
  "method_version":"gefs-pgrb2b-bounded-supplement-v1",
  "status":status,
  "attempts":attempts,
  "maximum_attempts":PGRB2B_MAX_RETRIES,
  "retry_delay_hours":PGRB2B_RETRY_DELAY_HOURS,
  "next_retry_not_before_utc":next_retry,
  "missing_leads_hours":missing,
  "last_attempt_results":attempt_results or [],
  "updated_at_utc":now(),
 }

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
def cycle_gate_action(payload,model):
 plan=payload.get("provider_cycle_gate") if isinstance(payload.get("provider_cycle_gate"),dict) else {}
 entry=(plan.get("models") or {}).get(model) if isinstance(plan.get("models"),dict) else None
 return entry.get("action") if isinstance(entry,dict) else "fetch"

def collect(payload,path,workers=4):
 previous=payload.get("full_horizon_archive") if isinstance(payload.get("full_horizon_archive"),dict) else {}
 previous_sources=previous.get("sources") if isinstance(previous.get("sources"),dict) else {}
 sources,jobs={},[]
 for model in MODELS:
  core=payload.get("models",{}).get(model,[])
  if not core:
   source={"records":[],"errors":[{"reason":"parent_cycle_missing"}],"lead_status":{}}
   sources[model]=source
   continue
  run=ext.cycle_from_existing(payload,model)
  action=cycle_gate_action(payload,model)
  if action=="carry_forward":
   prior=previous_sources.get(model)
   if not isinstance(prior,dict) or prior.get("run_time_utc")!=run.isoformat():
    raise RuntimeError(
     f"{model} cycle gate requested carry-forward without matching prior full-horizon source")
   source=json.loads(json.dumps(prior))
   source["cycle_gate_action"]="carry_forward"
   source["cycle_gate_checked_at_utc"]=(payload.get("provider_cycle_gate") or {}).get("checked_at_utc")
   sources[model]=source
   continue
  source={"records":[],"errors":[],"lead_status":{}}
  sources[model]=source
  leads=extension_leads(model,run);expected_max=maximum_hours(model,run)
  policy=acquisition_policy(model,run)
  source.update(
   run_time_utc=run.isoformat(),
   cycle_gate_action=action,
   target_max_hours=expected_max,
   expected_max_lead_for_cycle=expected_max,
   provider_native_horizon_hours=policy["provider_native_horizon_hours"],
   compatibility_horizon_hours=policy["compatibility_horizon_hours"],
   acquisition_grid_version=policy["acquisition_grid_version"],
   retention_grid_version=policy["retention_grid_version"],
   acquisition_max_lead_hours=policy["acquisition_max_lead_hours"],
   requested_extension_leads=leads,
  )
  for h in leads:source["lead_status"][str(h)]={"status":"pending"}
  jobs.extend((model,leads[i:i+6]) for i in range(0,len(leads),6)) if model=="ECMWF-IFS" else jobs.extend((model,[h]) for h in leads)
 payload["full_horizon_archive"]={"method_version":VERSION,"started_at_utc":now(),"sources":sources};checkpoint(payload,path)
 with ThreadPoolExecutor(max_workers=workers) as pool:
  fs={pool.submit(fetch_ifs,payload,l) if m=="ECMWF-IFS" else pool.submit(fetch_noaa,m,utc(sources[m]["run_time_utc"]),l[0]):(m,l) for m,l in jobs}
  for f in as_completed(fs):
   m,l=fs[f]
   try:
    rr=f.result()
    availability.stamp_rows(rr, observed_at=now(), replace_row_time=False)
    prior=list(sources[m]["records"]);sources[m]["records"].extend(rr)
    for h in l:sources[m]["lead_status"][str(h)]={"status":"published","checked_at_utc":now()}
    candidate_core=[int(r["forecast_lead_hours"]) for r in payload.get("models",{}).get(m,[]) if r.get("derived") and r.get("forecast_lead_hours") is not None]
    candidate_tail=[int(r["forecast_lead_hours"]) for r in sources[m]["records"] if r.get("derived")]
    sources[m]["actual_max_lead"]=max(candidate_core+candidate_tail) if candidate_core or candidate_tail else None
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
def archive_exit_code(coverage):
 return 0 if coverage.get("horizon_status")=="complete" else 1

def main():
 p=json.loads(SNAP.read_text())
 if p.get("mode")=="test":raise RuntimeError("full-horizon collection is production-only")
 r=collect(p,SNAP);print(json.dumps(r,sort_keys=True))
 code=archive_exit_code(r)
 if code:raise SystemExit(code)
if __name__=="__main__":main()
