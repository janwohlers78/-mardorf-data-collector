#!/usr/bin/env python3
"""Integrity audit for public Wunstorf and ETNW secondary observation payloads."""
import argparse,hashlib,json,math
from datetime import datetime,timezone
from pathlib import Path

POLICY=Path("config/integrity_policy.json")
UTC=timezone.utc

def dt(v):
    if v in (None,""):return None
    try:x=datetime.fromisoformat(str(v).replace("Z","+00:00"))
    except Exception:return None
    if x.tzinfo is None:return None
    return x.astimezone(UTC)

def finite(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)

def issue(code,severity,source,scope,impact,**details):
    return {"code":code,"severity":severity,"source":source,"scope":scope,"impact":impact,"details":details}

def report(kind,now,sources,issues,meta):
    errors=sum(x["severity"]=="ERROR" for x in issues)
    warnings=sum(x["severity"]=="WARN" for x in issues)
    infos=sum(x["severity"]=="INFO" for x in issues)
    return {
        "schema_version":1,
        "method_version":json.loads(POLICY.read_text(encoding="utf-8")).get("method_version","collector-integrity-v1.5"),
        "kind":kind,"generated_at_utc":now.isoformat(),
        "status":"FAIL" if errors else ("PASS_WITH_WARNINGS" if warnings else "PASS"),
        "error_count":errors,"warning_count":warnings,"info_count":infos,
        "bundle_ready_for_private_revalidation":errors==0,
        "sources":sources,"issues":issues,**meta,
    }

def read_payload(kind,path,now):
    source="DWD-05715" if kind=="wunstorf" else "ETNW"
    if not path.exists():
        return None,{},[issue(f"{kind.upper()}_BUNDLE_MISSING","ERROR",source,"bundle","Collector payload file is missing.",path=str(path))],{"input_file_present":False}
    raw=path.read_bytes()
    meta={"input_file_present":True,"input_payload_sha256":hashlib.sha256(raw).hexdigest(),"input_payload_bytes":len(raw)}
    try:return json.loads(raw.decode("utf-8")),meta,[],meta
    except Exception as e:
        return None,meta,[issue(f"{kind.upper()}_BUNDLE_JSON_INVALID","ERROR",source,"bundle","Collector payload is not valid JSON.",exception_type=type(e).__name__,exception_message=str(e),path=str(path))],meta

def audit_wunstorf(path,cfg,now):
    d,meta,issues,_=read_payload("wunstorf",path,now);sources={}
    if d is None:return report("wunstorf",now,sources,issues,meta)
    pcfg=cfg.get("wunstorf_policy") or {};source="DWD-05715"
    station=d.get("station") if isinstance(d.get("station"),dict) else {}
    if d.get("provider")!="DWD" or str(station.get("id"))!=str(pcfg.get("station_id","05715")):
        issues.append(issue("WUNSTORF_SOURCE_IDENTITY_MISMATCH","ERROR",source,"station_identity",
            "DWD Wunstorf payload identity does not match configured source.",
            provider=d.get("provider"),station=station,expected_station_id=pcfg.get("station_id","05715")))
    retrieved=dt(d.get("retrieved_at_utc"));future=float(pcfg.get("maximum_timestamp_future_tolerance_minutes",15))
    if retrieved is None:
        issues.append(issue("WUNSTORF_RETRIEVAL_TIMESTAMP_INVALID","ERROR",source,"timestamps","retrieved_at_utc is missing or invalid.",value=d.get("retrieved_at_utc")))
    elif (retrieved-now).total_seconds()/60>future:
        issues.append(issue("WUNSTORF_RETRIEVAL_TIMESTAMP_TOO_FAR_IN_FUTURE","ERROR",source,"timestamps","retrieval timestamp is implausibly in the future.",retrieved_at_utc=retrieved.isoformat(),checked_at_utc=now.isoformat()))
    rows=d.get("observations")
    parsed=[];bad=0
    if not isinstance(rows,list) or not rows:
        issues.append(issue("WUNSTORF_OBSERVATIONS_MISSING","ERROR",source,"observations","No Wunstorf observation rows are present."))
        rows=[]
    for i,row in enumerate(rows):
        if not isinstance(row,dict):bad+=1;continue
        t=dt(row.get("time_utc"));ws=row.get("wind_speed_ms");wd=row.get("wind_direction_deg")
        if t is None or (t-now).total_seconds()/60>future or not finite(ws) or float(ws)<0 or not finite(wd) or not 0<=float(wd)<=360:
            bad+=1;continue
        parsed.append(t)
    if bad:
        issues.append(issue("WUNSTORF_INVALID_OBSERVATION_ROWS","ERROR",source,"observations",
            "One or more Wunstorf rows have invalid timestamps/wind values.",invalid_row_count=bad,total_rows=len(rows)))
    if parsed and (parsed!=sorted(parsed) or len(parsed)!=len(set(parsed))):
        issues.append(issue("WUNSTORF_TIME_AXIS_INVALID","ERROR",source,"timestamps","Wunstorf timestamps are unordered or duplicated."))
    latest=dt(d.get("latest_observation_time_utc"));latest_obj=d.get("latest_observation") if isinstance(d.get("latest_observation"),dict) else {}
    latest_obj_t=dt(latest_obj.get("time_utc"))
    if not parsed or latest is None or latest_obj_t is None or latest!=parsed[-1] or latest_obj_t!=parsed[-1]:
        issues.append(issue("WUNSTORF_LATEST_POINTER_MISMATCH","ERROR",source,"timestamps",
            "Latest Wunstorf timestamps do not identify the newest observation row.",
            declared_latest=d.get("latest_observation_time_utc"),latest_object_time=latest_obj.get("time_utc"),
            newest_row_time=parsed[-1].isoformat() if parsed else None))
    age=(now-latest).total_seconds()/60 if latest else None
    if age is not None and age>float(pcfg.get("warning_archive_age_minutes",2880)):
        issues.append(issue("WUNSTORF_ARCHIVE_UNUSUALLY_DELAYED","WARN",source,"freshness",
            "DWD CDC historical reference is older than the configured plausible archive-lag window.",
            observation_age_minutes=round(age,2),warning_archive_age_minutes=pcfg.get("warning_archive_age_minutes",2880)))
    elif age is not None and age>float(pcfg.get("recent_reference_minutes",180)):
        issues.append(issue("WUNSTORF_ARCHIVE_PROVIDER_LAG","INFO",source,"freshness",
            "DWD CDC reference is outside the recent-reference target but within plausible archive lag.",
            observation_age_minutes=round(age,2),recent_reference_minutes=pcfg.get("recent_reference_minutes",180)))
    sources[source]={
        "provider":d.get("provider"),"station":station,"retrieved_at_utc":d.get("retrieved_at_utc"),
        "latest_observation_time_utc":d.get("latest_observation_time_utc"),
        "observation_age_minutes":round(age,2) if age is not None else None,
        "recent_reference_minutes":float(pcfg.get("recent_reference_minutes",180)),
        "warning_archive_age_minutes":float(pcfg.get("warning_archive_age_minutes",2880)),
        "record_count":len(rows),"quality":d.get("quality"),"request_diagnostics":d.get("request_diagnostics"),
        "role":d.get("source_role"),
    }
    return report("wunstorf",now,sources,issues,{**meta,"retrieved_at_utc":d.get("retrieved_at_utc")})

def audit_etnw(path,cfg,now):
    d,meta,issues,_=read_payload("etnw",path,now);sources={}
    if d is None:return report("etnw",now,sources,issues,meta)
    pcfg=cfg.get("etnw_policy") or {};source="ETNW"
    station=d.get("station") if isinstance(d.get("station"),dict) else {}
    if d.get("provider")!="AviationWeather.gov" or station.get("icao")!=pcfg.get("station_icao","ETNW"):
        issues.append(issue("ETNW_SOURCE_IDENTITY_MISMATCH","ERROR",source,"station_identity",
            "ETNW payload identity does not match configured METAR source.",
            provider=d.get("provider"),station=station,expected_icao=pcfg.get("station_icao","ETNW")))
    retrieved=dt(d.get("retrieved_at_utc"));future=float(pcfg.get("maximum_timestamp_future_tolerance_minutes",15))
    if retrieved is None:
        issues.append(issue("ETNW_RETRIEVAL_TIMESTAMP_INVALID","ERROR",source,"timestamps","retrieved_at_utc is missing or invalid.",value=d.get("retrieved_at_utc")))
    elif (retrieved-now).total_seconds()/60>future:
        issues.append(issue("ETNW_RETRIEVAL_TIMESTAMP_TOO_FAR_IN_FUTURE","ERROR",source,"timestamps","retrieval timestamp is implausibly in the future.",retrieved_at_utc=retrieved.isoformat(),checked_at_utc=now.isoformat()))
    rows=d.get("observations")
    parsed=[];bad=0
    if not isinstance(rows,list) or not rows:
        issues.append(issue("ETNW_OBSERVATIONS_MISSING","ERROR",source,"observations","No ETNW METAR rows are present."))
        rows=[]
    for row in rows:
        if not isinstance(row,dict):bad+=1;continue
        t=dt(row.get("time_utc"));ws=row.get("wind_speed_ms");wd=row.get("wind_direction_deg");var=row.get("wind_direction_variable") is True;gust=row.get("wind_gust_ms")
        valid_dir=(finite(wd) and 0<=float(wd)<=360) or var
        if t is None or (t-now).total_seconds()/60>future or not finite(ws) or float(ws)<0 or not valid_dir or (gust is not None and (not finite(gust) or float(gust)<0)):
            bad+=1;continue
        parsed.append(t)
    if bad:
        issues.append(issue("ETNW_INVALID_OBSERVATION_ROWS","ERROR",source,"observations",
            "One or more ETNW rows have invalid timestamps/wind values.",invalid_row_count=bad,total_rows=len(rows)))
    if parsed and (parsed!=sorted(parsed) or len(parsed)!=len(set(parsed))):
        issues.append(issue("ETNW_TIME_AXIS_INVALID","ERROR",source,"timestamps","ETNW timestamps are unordered or duplicated."))
    latest=dt(d.get("latest_observation_time_utc"));latest_obj=d.get("latest_observation") if isinstance(d.get("latest_observation"),dict) else {};latest_obj_t=dt(latest_obj.get("time_utc"))
    if not parsed or latest is None or latest_obj_t is None or latest!=parsed[-1] or latest_obj_t!=parsed[-1]:
        issues.append(issue("ETNW_LATEST_POINTER_MISMATCH","ERROR",source,"timestamps",
            "Latest ETNW timestamps do not identify the newest observation row.",
            declared_latest=d.get("latest_observation_time_utc"),latest_object_time=latest_obj.get("time_utc"),
            newest_row_time=parsed[-1].isoformat() if parsed else None))
    age=(now-latest).total_seconds()/60 if latest else None
    fresh=float(pcfg.get("fresh_target_minutes",120));maximum=float(pcfg.get("maximum_current_age_minutes",360))
    if age is not None and age>maximum:
        issues.append(issue("ETNW_OBSERVATION_TOO_OLD","ERROR",source,"freshness",
            "ETNW is too old to promote as the current Wunstorf redundancy.",
            observation_age_minutes=round(age,2),maximum_current_age_minutes=maximum))
    elif age is not None and age>fresh:
        issues.append(issue("ETNW_OBSERVATION_EXCEEDS_FRESH_TARGET","WARN",source,"freshness",
            "ETNW is usable but older than the preferred current-state target.",
            observation_age_minutes=round(age,2),fresh_target_minutes=fresh))
    sources[source]={
        "provider":d.get("provider"),"station":station,"retrieved_at_utc":d.get("retrieved_at_utc"),
        "latest_observation_time_utc":d.get("latest_observation_time_utc"),
        "observation_age_minutes":round(age,2) if age is not None else None,
        "fresh_target_minutes":fresh,"maximum_current_age_minutes":maximum,
        "record_count":len(rows),"quality":d.get("quality"),"request_diagnostics":d.get("request_diagnostics"),
        "dwd_05715_crosscheck":d.get("dwd_05715_crosscheck"),"role":d.get("source_role"),
    }
    return report("etnw",now,sources,issues,{**meta,"retrieved_at_utc":d.get("retrieved_at_utc")})

def markdown(r):
    lines=[f"# Collector integrity — {r['kind']}","",f"- Generated UTC: {r['generated_at_utc']}",
           f"- Status: **{r['status']}**",f"- Errors: {r['error_count']}; warnings: {r['warning_count']}; info: {r['info_count']}",
           f"- Bundle ready for private revalidation: {r['bundle_ready_for_private_revalidation']}",""]
    for name,x in r.get("sources",{}).items():
        lines += [f"## {name}","",f"- Latest observation UTC: {x.get('latest_observation_time_utc')}",
                  f"- Observation age: {x.get('observation_age_minutes')} min.",f"- Records: {x.get('record_count')}",""]
    lines += ["## Exact diagnostics",""]
    if not r.get("issues"):lines.append("- No integrity deviations recorded.")
    else:
        for i,x in enumerate(r["issues"],1):
            lines += [f"### {i}. {x['severity']} — {x['code']}",f"- Source: {x['source']}; scope: {x['scope']}",
                      f"- Impact: {x['impact']}","- Details:"]
            lines.extend("    "+line for line in json.dumps(x["details"],indent=2,ensure_ascii=False,sort_keys=True).splitlines())
    return "\n".join(lines)+"\n"

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--kind",required=True,choices=("wunstorf","etnw"))
    ap.add_argument("--input",required=True);ap.add_argument("--json-out",required=True);ap.add_argument("--md-out",required=True)
    args=ap.parse_args();cfg=json.loads(POLICY.read_text(encoding="utf-8"));now=datetime.now(UTC)
    r=audit_wunstorf(Path(args.input),cfg,now) if args.kind=="wunstorf" else audit_etnw(Path(args.input),cfg,now)
    jp=Path(args.json_out);mp=Path(args.md_out);jp.parent.mkdir(parents=True,exist_ok=True);mp.parent.mkdir(parents=True,exist_ok=True)
    jp.write_text(json.dumps(r,indent=2,ensure_ascii=False,allow_nan=False)+"\n",encoding="utf-8")
    mp.write_text(markdown(r),encoding="utf-8")
    print(json.dumps({"status":r["status"],"errors":r["error_count"],"warnings":r["warning_count"],"ready":r["bundle_ready_for_private_revalidation"]},ensure_ascii=False))

if __name__=="__main__":main()
