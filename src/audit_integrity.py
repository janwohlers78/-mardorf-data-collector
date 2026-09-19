#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,math
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path

POLICY=Path("config/integrity_policy.json")
FAMILY={
    "ICON-D2":"DWD-ICON","ICON-D2-EPS":"DWD-ICON","ICON-EU":"DWD-ICON",
    "ECMWF-IFS":"ECMWF","GFS":"GFS","GEFS-control":"GFS",
}
POLICY_VERSION="collector-integrity-v1.1"

def dt(v):
    if not v:return None
    try:
        x=datetime.fromisoformat(str(v).replace("Z","+00:00"))
        return x.astimezone(timezone.utc) if x.tzinfo else x.replace(tzinfo=timezone.utc)
    except Exception:return None

def finite(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)

def issue(code,severity,source,scope,impact,**details):
    return {"code":code,"severity":severity,"source":source,"scope":scope,"impact":impact,"details":details}

def provider_max(model,run_hour,cfg):
    p=cfg["model_policy"]["provider_horizon_by_cycle"][model]
    if "default_max_hours" in p:return int(p["default_max_hours"])
    return int(p["main_max_hours"] if run_hour in p["main_cycle_hours"] else p["other_max_hours"])

def desired_leads(model,cfg):
    return [int(x) for x in cfg["model_policy"]["project_desired_leads"][model]]

def make_report(kind,now,sources,issues,usable,extra):
    counts=Counter(x["severity"] for x in issues)
    status="PASS" if counts["ERROR"]==0 and counts["WARN"]==0 else "PASS_WITH_WARNINGS" if counts["ERROR"]==0 else "FAIL"
    return {
        "schema_version":1,"method_version":str(POLICY_VERSION),"kind":kind,
        "generated_at_utc":now.isoformat(),"status":status,
        "error_count":counts["ERROR"],"warning_count":counts["WARN"],"info_count":counts["INFO"],
        "bundle_ready_for_private_revalidation":bool(usable and counts["ERROR"]==0),
        "sources":sources,"issues":issues,**extra
    }

def audit_models(path,cfg,now):
    issues=[];sources={}
    if not path.exists():
        issues.append(issue("MODEL_BUNDLE_FILE_NOT_CREATED","ERROR","collector","bundle",
            "No model payload exists to transfer or ingest.",path=str(path)))
        return make_report("models",now,sources,issues,False,{"input_file_present":False})
    try:d=json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        issues.append(issue("MODEL_BUNDLE_JSON_INVALID","ERROR","collector","bundle",
            "The model payload cannot be parsed.",exception_type=type(e).__name__,exception_message=str(e),path=str(path)))
        return make_report("models",now,sources,issues,False,{"input_file_present":True})

    mode=d.get("mode","unknown")
    retrieval=dt(d.get("retrieved_at_utc") or d.get("horizon_extension_retrieved_at_utc") or d.get("dwd_additional_retrieved_at_utc") or d.get("extended_retrieved_at_utc"))
    if retrieval is None:
        issues.append(issue("RETRIEVAL_TIMESTAMP_INVALID","ERROR","collector","bundle",
            "Bundle age and chronology cannot be verified.",value=d.get("retrieved_at_utc")))
    else:
        future_min=(retrieval-now).total_seconds()/60
        tol=float(cfg["model_policy"]["retrieval_timestamp_future_tolerance_minutes"])
        if future_min>tol:
            issues.append(issue("RETRIEVAL_TIMESTAMP_IN_FUTURE","ERROR","collector","bundle",
                "Bundle chronology is implausible.",retrieved_at_utc=retrieval.isoformat(),
                checked_at_utc=now.isoformat(),future_offset_minutes=round(future_min,2),allowed_future_minutes=tol))

    qerrors=list((d.get("quality") or {}).get("errors") or [])
    for model in cfg["model_policy"]["project_desired_leads"]:
        recs=[r for r in (d.get("models") or {}).get(model,[]) if isinstance(r,dict)]
        lead_rows={};duplicates=[];invalid_lead_rows=[]
        for r in recs:
            try:lead=int(r.get("forecast_lead_hours"))
            except Exception:
                invalid_lead_rows.append({"valid_time_utc":r.get("valid_time_utc"),"value":r.get("forecast_lead_hours")});continue
            if lead in lead_rows:duplicates.append(lead)
            else:lead_rows[lead]=r

        run_values=sorted({str(r.get("run_time_utc")) for r in recs if r.get("run_time_utc")})
        run=dt(run_values[0]) if len(run_values)==1 else None
        run_hour=run.hour if run else None
        pmax=provider_max(model,run_hour,cfg) if run_hour is not None else None
        if mode=="test":
            expected=[0,12,24,30,36,42,48] if model in ("ICON-D2","GFS") else [0,12,24,36,48]
            project_gap=[]
        else:
            expected=[x for x in desired_leads(model,cfg) if pmax is not None and x<=pmax]
            project_gap=[x for x in desired_leads(model,cfg) if pmax is not None and x>pmax]
        got=sorted(lead_rows);missing=sorted(set(expected)-set(got));extra=sorted(set(got)-set(expected))
        field_failures=[];timestamp_failures=[];critical_source_errors=[];optional_source_warnings=[];outside_horizon_records=[]
        expected_set=set(expected)
        optional_fields=set(cfg["model_policy"].get("optional_weather_context_fields",[]))
        for lead,r in sorted(lead_rows.items()):
            in_provider_scope=lead in expected_set
            if not in_provider_scope:
                outside_horizon_records.append({"lead_hours":lead,"reason":"outside_selected_provider_cycle_horizon"})
            der=r.get("derived") if isinstance(r.get("derived"),dict) else {}
            req=list(cfg["model_policy"]["required_derived_fields"])
            if model in cfg["model_policy"]["required_gust_models"]:req.append("gust_ms")
            absent=[f for f in req if not finite(der.get(f))]
            if in_provider_scope and absent:field_failures.append({"lead_hours":lead,"fields":absent})
            rt=dt(r.get("run_time_utc"));vt=dt(r.get("valid_time_utc"))
            if rt is None or vt is None:
                timestamp_failures.append({"lead_hours":lead,"run_time_utc":r.get("run_time_utc"),"valid_time_utc":r.get("valid_time_utc"),"reason":"unparseable_timestamp"})
            else:
                actual=(vt-rt).total_seconds()/3600
                if abs(actual-lead)>0.06:
                    timestamp_failures.append({"lead_hours":lead,"run_time_utc":rt.isoformat(),"valid_time_utc":vt.isoformat(),"actual_lead_hours":round(actual,3),"reason":"declared_lead_differs_from_run_to_valid_interval"})
            def record_problem(item,field=None):
                if not in_provider_scope:
                    item["classification"]="outside_provider_cycle_horizon"
                    outside_horizon_records.append(item)
                elif field in optional_fields:
                    item["classification"]="optional_weather_context"
                    optional_source_warnings.append(item)
                else:
                    item["classification"]="wind_or_record_critical"
                    critical_source_errors.append(item)
            if r.get("error"):
                record_problem({"lead_hours":lead,"location":"record","message":str(r["error"])})
            if r.get("error_type") or r.get("error_message"):
                record_problem({"lead_hours":lead,"location":"record","exception_type":r.get("error_type"),"message":r.get("error_message")})
            if r.get("derive_error"):
                record_problem({"lead_hours":lead,"location":"derive","message":str(r["derive_error"])})
            if r.get("derive_error_type") or r.get("derive_error_message"):
                record_problem({"lead_hours":lead,"location":"derive","exception_type":r.get("derive_error_type"),"message":r.get("derive_error_message")})
            vals=r.get("values")
            if isinstance(vals,dict):
                for key,val in vals.items():
                    if isinstance(val,dict) and val.get("error"):
                        record_problem({"lead_hours":lead,"location":"values."+key,"message":str(val["error"])},key)
                    if isinstance(val,dict) and (val.get("error_type") or val.get("error_message")):
                        record_problem({"lead_hours":lead,"location":"values."+key,"exception_type":val.get("error_type"),"message":val.get("error_message"),"source_url":val.get("source_url")},key)

        model_qerrors=[str(x) for x in qerrors if str(x).startswith(model+":")]
        if len(run_values)==0:
            issues.append(issue("MODEL_RUN_TIME_NOT_PRESENT","ERROR",model,"run_identity",
                "No model cycle can be identified for this source.",record_count=len(recs)))
        elif len(run_values)>1:
            issues.append(issue("MULTIPLE_MODEL_RUNS_IN_ONE_SOURCE_BUNDLE","ERROR",model,"run_identity",
                "One source bundle contains records from more than one cycle.",run_time_values=run_values))
        if invalid_lead_rows:
            issues.append(issue("FORECAST_LEAD_VALUE_INVALID","ERROR",model,"coverage",
                "Some forecast records cannot be assigned to a lead time.",rows=invalid_lead_rows))
        if duplicates:
            issues.append(issue("DUPLICATE_FORECAST_LEADS","ERROR",model,"coverage",
                "More than one record exists for the same model lead.",duplicate_leads_hours=sorted(set(duplicates))))
        if missing:
            issues.append(issue("EXPECTED_PROVIDER_LEADS_NOT_RECEIVED","ERROR",model,"coverage",
                "The selected model cycle does not contain all leads that should be collected from that cycle.",
                expected_leads_hours=expected,received_leads_hours=got,missing_leads_hours=missing,
                selected_cycle_hour_utc=run_hour,provider_expected_max_horizon_hours=pmax))
        if project_gap:
            issues.append(issue("PROJECT_LONG_RANGE_LEADS_NOT_PROVIDED_BY_SELECTED_CYCLE","INFO",model,"coverage",
                "These project-desired long-range leads are outside the documented horizon of the selected provider cycle; this is not a download failure.",
                selected_cycle_hour_utc=run_hour,provider_expected_max_horizon_hours=pmax,
                project_desired_but_cycle_unavailable_leads_hours=project_gap))
        if extra:
            issues.append(issue("RECORDS_OUTSIDE_SELECTED_PROVIDER_CYCLE_HORIZON","INFO",model,"coverage",
                "Records or placeholders outside the selected cycle's documented horizon are excluded from completeness and field-failure gates.",
                provider_expected_max_horizon_hours=pmax,extra_received_leads_hours=extra))
        if field_failures:
            issues.append(issue("REQUIRED_DERIVED_FIELDS_UNAVAILABLE","ERROR",model,"fields",
                "Wind data required by the private integrity gate could not be derived for specific leads.",
                affected_leads=field_failures))
        if timestamp_failures:
            issues.append(issue("FORECAST_TIMESTAMP_OR_LEAD_INCONSISTENCY","ERROR",model,"timestamps",
                "Declared run/valid/lead metadata are internally inconsistent for specific records.",
                affected_records=timestamp_failures))
        if critical_source_errors or model_qerrors:
            issues.append(issue("SOURCE_FETCH_OR_DECODE_ERRORS_RECORDED","ERROR",model,"provider_fetch",
                "Wind-critical provider requests, GRIB extraction or field derivation recorded explicit errors.",
                record_errors=critical_source_errors,quality_errors=model_qerrors))
        if optional_source_warnings:
            issues.append(issue("OPTIONAL_WEATHER_CONTEXT_FIELDS_UNAVAILABLE","WARN",model,"optional_weather_context",
                "Optional precipitation/CAPE context is incomplete at specific leads; wind-core completeness is unaffected.",
                affected_fields=optional_source_warnings))
        run_age=None;age_limit=float(cfg["model_policy"]["maximum_run_age_hours"][model])
        if run:
            run_age=(now-run).total_seconds()/3600
            future_tol=float(cfg["model_policy"]["run_timestamp_future_tolerance_minutes"])/60
            if run_age < -future_tol:
                issues.append(issue("MODEL_RUN_TIME_IMPLAUSIBLY_FUTURE","ERROR",model,"currentness",
                    "The selected model cycle is timestamped too far in the future.",
                    run_time_utc=run.isoformat(),checked_at_utc=now.isoformat(),run_age_hours=round(run_age,3),
                    allowed_future_hours=round(future_tol,3)))
            elif run_age>age_limit:
                issues.append(issue("MODEL_RUN_OLDER_THAN_CURRENTNESS_POLICY","ERROR",model,"currentness",
                    "A newer provider cycle should normally be available; the exact excess age is recorded.",
                    run_time_utc=run.isoformat(),checked_at_utc=now.isoformat(),run_age_hours=round(run_age,3),
                    maximum_run_age_hours=age_limit,excess_age_hours=round(run_age-age_limit,3)))

        sources[model]={
            "family":FAMILY[model],"record_count":len(recs),
            "selected_run_time_utc":run.isoformat() if run else None,"selected_cycle_hour_utc":run_hour,
            "run_age_hours":round(run_age,3) if run_age is not None else None,"maximum_run_age_hours":age_limit,
            "provider_expected_max_horizon_hours":pmax,"project_desired_max_horizon_hours":max(desired_leads(model,cfg)),
            "expected_collection_leads_hours":expected,"received_leads_hours":got,"missing_expected_leads_hours":missing,
            "extra_received_leads_hours":extra,"project_desired_but_cycle_unavailable_leads_hours":project_gap,
            "duplicate_leads_hours":sorted(set(duplicates)),"required_field_failures":field_failures,
            "timestamp_failures":timestamp_failures,"provider_or_decode_errors":critical_source_errors,
            "optional_weather_context_warnings":optional_source_warnings,
            "out_of_horizon_records":outside_horizon_records,
            "quality_error_messages":model_qerrors,
            "provider_cycle_complete":not any([missing,duplicates,invalid_lead_rows,field_failures,timestamp_failures,critical_source_errors,model_qerrors]) and run is not None,
            "currentness_policy_pass":run_age is not None and run_age<=age_limit and run_age>=-float(cfg["model_policy"]["run_timestamp_future_tolerance_minutes"])/60,
        }

    complete_current_families=sorted({v["family"] for v in sources.values() if v["provider_cycle_complete"] and v["currentness_policy_pass"]})
    usable=len(complete_current_families)>=2
    if not usable:
        issues.append(issue("FEWER_THAN_TWO_COMPLETE_CURRENT_INDEPENDENT_MODEL_FAMILIES","ERROR","collector","family_gate",
            "The private forecast must not treat this acquisition as sufficient multi-family evidence.",
            complete_current_families=complete_current_families,required_count=2,observed_count=len(complete_current_families)))
    return make_report("models",now,sources,issues,usable,{
        "input_file_present":True,"collector_mode":mode,
        "retrieved_at_utc":retrieval.isoformat() if retrieval else None,
        "complete_current_independent_families":complete_current_families,
        "minimum_two_complete_current_independent_families_met":usable,
    })

def audit_svg(path,cfg,now):
    issues=[];sources={}
    if not path.exists():
        issues.append(issue("SVG_BUNDLE_FILE_NOT_CREATED","ERROR","SVG-42374","bundle",
            "No SVG payload exists to transfer or ingest.",path=str(path)))
        return make_report("svg",now,sources,issues,False,{"input_file_present":False})
    try:d=json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        issues.append(issue("SVG_BUNDLE_JSON_INVALID","ERROR","SVG-42374","bundle",
            "The SVG payload cannot be parsed.",exception_type=type(e).__name__,exception_message=str(e),path=str(path)))
        return make_report("svg",now,sources,issues,False,{"input_file_present":True})

    reqs=d.get("request_diagnostics") or {}
    for endpoint in ("current","historic","stations"):
        x=reqs.get(endpoint)
        sev="ERROR" if endpoint!="stations" else "WARN"
        if not isinstance(x,dict):
            issues.append(issue("SVG_REQUEST_DIAGNOSTIC_NOT_RECORDED",sev,"SVG-42374",endpoint,
                "The collector cannot prove how this WeatherLink request completed.",endpoint=endpoint))
        elif not x.get("success"):
            issues.append(issue("WEATHERLINK_REQUEST_FAILED",sev,"SVG-42374",endpoint,
                "A WeatherLink request failed; exact HTTP/exception information is attached.",
                endpoint=endpoint,http_status=x.get("http_status"),exception_type=x.get("exception_type"),
                exception_message=x.get("exception_message"),request_path=x.get("request_path")))

    obs=d.get("latest_observation") if isinstance(d.get("latest_observation"),dict) else None
    ot=dt(obs.get("time_utc")) if obs else None;age=None
    if ot:
        age=(now-ot).total_seconds()/60
        future_tol=float(cfg["svg_policy"]["maximum_timestamp_future_tolerance_minutes"])
        fresh=float(cfg["svg_policy"]["fresh_target_minutes"]);maxage=float(cfg["svg_policy"]["maximum_current_state_age_minutes"])
        if age < -future_tol:
            issues.append(issue("SVG_OBSERVATION_TIMESTAMP_TOO_FAR_IN_FUTURE","ERROR","SVG-42374","current_observation",
                "Current observation chronology is implausible.",observation_time_utc=ot.isoformat(),
                checked_at_utc=now.isoformat(),future_offset_minutes=round(-age,2),allowed_future_minutes=future_tol))
        elif age>maxage:
            issues.append(issue("SVG_CURRENT_OBSERVATION_EXCEEDS_MAXIMUM_AGE","ERROR","SVG-42374","current_observation",
                "The current observation is too old for current-state use.",observation_time_utc=ot.isoformat(),
                checked_at_utc=now.isoformat(),observation_age_minutes=round(age,2),
                maximum_current_state_age_minutes=maxage,excess_age_minutes=round(age-maxage,2)))
        elif age>fresh:
            issues.append(issue("SVG_CURRENT_OBSERVATION_EXCEEDS_FRESH_TARGET","WARN","SVG-42374","current_observation",
                "The observation remains within the maximum current-state age but is older than the preferred freshness target.",
                observation_time_utc=ot.isoformat(),checked_at_utc=now.isoformat(),observation_age_minutes=round(age,2),
                fresh_target_minutes=fresh,excess_over_fresh_target_minutes=round(age-fresh,2)))
    else:
        issues.append(issue("SVG_CURRENT_OBSERVATION_TIMESTAMP_UNAVAILABLE","ERROR","SVG-42374","current_observation",
            "No parseable current observation timestamp is present.",value=(obs or {}).get("time_utc")))

    rows=[x for x in (d.get("recent_historic_observations") or []) if isinstance(x,dict)]
    parsed=[dt(x.get("time_utc")) for x in rows];times=sorted({x for x in parsed if x is not None})
    duplicates=len(rows)-len(times);cadence=float(cfg["svg_policy"]["archive_expected_cadence_minutes"]);gaps=[]
    for a,b in zip(times,times[1:]):
        diff=(b-a).total_seconds()/60
        if diff>cadence+0.1:
            gaps.append({"after_utc":a.isoformat(),"before_utc":b.isoformat(),"gap_minutes":round(diff,2),
                         "estimated_missing_five_minute_intervals":max(0,int(round(diff/cadence))-1)})

    requested=d.get("requested_history_window") or {}
    requested_start=dt(requested.get("start_utc"));requested_end=dt(requested.get("end_utc"))
    expected_slots=None;start_gap=None;end_gap=None;coverage_ratio=None
    if requested_start and requested_end and requested_end>requested_start:
        expected_slots=max(1,int((requested_end-requested_start).total_seconds()//(cadence*60)))
        if times:
            start_gap=max(0.0,(times[0]-requested_start).total_seconds()/60)
            end_gap=max(0.0,(requested_end-times[-1]).total_seconds()/60)
            coverage_ratio=min(1.0,len(times)/expected_slots) if expected_slots else None

    if not rows:
        issues.append(issue("SVG_HISTORIC_WINDOW_RETURNED_ZERO_RECORDS","ERROR","SVG-42374","historic_window",
            "The routine WeatherLink history request returned no normalized archive records.",
            requested_history_hours=cfg["svg_policy"]["routine_history_hours"]))
    if duplicates:
        issues.append(issue("SVG_HISTORIC_DUPLICATE_TIMESTAMPS","WARN","SVG-42374","historic_window",
            "Repeated archive timestamps were present in the normalized history.",duplicate_record_count=duplicates))
    if gaps:
        issues.append(issue("SVG_HISTORIC_CADENCE_GAPS","WARN","SVG-42374","historic_window",
            "The recent archive has gaps larger than the expected five-minute cadence; affected intervals are explicit.",
            expected_cadence_minutes=cadence,gaps=gaps,
            total_estimated_missing_intervals=sum(x["estimated_missing_five_minute_intervals"] for x in gaps)))
    edge_tol=float(cfg["svg_policy"]["archive_edge_tolerance_minutes"])
    if start_gap is not None and start_gap>edge_tol:
        issues.append(issue("SVG_HISTORIC_WINDOW_START_NOT_COVERED","WARN","SVG-42374","historic_window",
            "The first returned archive record begins later than the requested history window.",
            requested_start_utc=requested_start.isoformat(),first_record_utc=times[0].isoformat(),
            uncovered_start_minutes=round(start_gap,2),allowed_edge_tolerance_minutes=edge_tol))
    if end_gap is not None and end_gap>edge_tol:
        issues.append(issue("SVG_HISTORIC_WINDOW_END_NOT_COVERED","WARN","SVG-42374","historic_window",
            "The last returned archive record ends too far before the requested history-window end.",
            requested_end_utc=requested_end.isoformat(),last_record_utc=times[-1].isoformat(),
            uncovered_end_minutes=round(end_gap,2),allowed_edge_tolerance_minutes=edge_tol))

    current_req_ok=bool((reqs.get("current") or {}).get("success"))
    historic_req_ok=bool((reqs.get("historic") or {}).get("success"))
    within_age=age is not None and age<=float(cfg["svg_policy"]["maximum_current_state_age_minutes"]) and age>=-float(cfg["svg_policy"]["maximum_timestamp_future_tolerance_minutes"])
    usable=current_req_ok and historic_req_ok and within_age and ot is not None
    sources["SVG-42374"]={
        "current_request":reqs.get("current"),"historic_request":reqs.get("historic"),"metadata_request":reqs.get("stations"),
        "current_observation_time_utc":ot.isoformat() if ot else None,
        "current_observation_age_minutes":round(age,2) if age is not None else None,
        "fresh_target_minutes":cfg["svg_policy"]["fresh_target_minutes"],
        "maximum_current_state_age_minutes":cfg["svg_policy"]["maximum_current_state_age_minutes"],
        "current_state_age_policy_pass":within_age,"historic_record_count":len(rows),
        "historic_unique_timestamp_count":len(times),"historic_first_time_utc":times[0].isoformat() if times else None,
        "historic_last_time_utc":times[-1].isoformat() if times else None,
        "expected_archive_cadence_minutes":cadence,"historic_gap_count":len(gaps),"historic_gaps":gaps,
        "duplicate_historic_record_count":duplicates,
        "requested_history_start_utc":requested_start.isoformat() if requested_start else None,
        "requested_history_end_utc":requested_end.isoformat() if requested_end else None,
        "expected_five_minute_intervals":expected_slots,
        "observed_unique_intervals":len(times),
        "coverage_ratio_of_requested_interval_count":round(coverage_ratio,4) if coverage_ratio is not None else None,
        "uncovered_start_minutes":round(start_gap,2) if start_gap is not None else None,
        "uncovered_end_minutes":round(end_gap,2) if end_gap is not None else None,
    }
    return make_report("svg",now,sources,issues,usable,{
        "input_file_present":True,"retrieved_at_utc":d.get("retrieved_at_utc")
    })

def markdown(report):
    lines=[
        "# Collector integrity — "+report["kind"],"",
        "- Generated UTC: "+str(report["generated_at_utc"]),
        "- Status: **"+str(report["status"])+"**",
        "- Errors: %s; warnings: %s; info: %s"%(report["error_count"],report["warning_count"],report["info_count"]),
        "- Bundle ready for private revalidation: "+str(report["bundle_ready_for_private_revalidation"]),""
    ]
    if report["kind"]=="models":
        lines += ["| Source | Run UTC | Age h / limit | received / expected leads | cycle horizon | complete | currentness |",
                  "|---|---|---:|---:|---:|---|---|"]
        for name,x in report["sources"].items():
            lines.append("| %s | %s | %s / %s | %s / %s | %s h | %s | %s |"%(
                name,x["selected_run_time_utc"] or "—",x["run_age_hours"],x["maximum_run_age_hours"],
                len(x["received_leads_hours"]),len(x["expected_collection_leads_hours"]),
                x["provider_expected_max_horizon_hours"],x["provider_cycle_complete"],x["currentness_policy_pass"]))
    else:
        x=report["sources"].get("SVG-42374",{})
        lines += [
            "- Current observation UTC: "+str(x.get("current_observation_time_utc")),
            "- Observation age: %s min; fresh target %s min; maximum current-state age %s min."%(
                x.get("current_observation_age_minutes"),x.get("fresh_target_minutes"),x.get("maximum_current_state_age_minutes")),
            "- Historic records: %s; unique timestamps: %s; expected five-minute intervals: %s; coverage ratio: %s."%(
                x.get("historic_record_count"),x.get("historic_unique_timestamp_count"),x.get("expected_five_minute_intervals"),x.get("coverage_ratio_of_requested_interval_count")),
            "- Window edges not covered: start %s min; end %s min; internal cadence gaps: %s."%(
                x.get("uncovered_start_minutes"),x.get("uncovered_end_minutes"),x.get("historic_gap_count")),""
        ]
    lines += ["## Exact diagnostics",""]
    if not report["issues"]:lines.append("- No integrity deviations recorded.")
    else:
        for i,x in enumerate(report["issues"],1):
            lines += [
                "### %d. %s — %s"%(i,x["severity"],x["code"]),
                "- Source: %s; scope: %s"%(x["source"],x["scope"]),
                "- Impact: "+x["impact"],"- Details:"
            ]
            for line in json.dumps(x["details"],indent=2,ensure_ascii=False,sort_keys=True).splitlines():
                lines.append("    "+line)
    return "\n".join(lines)+"\n"

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--kind",required=True,choices=("models","svg"))
    ap.add_argument("--input",required=True);ap.add_argument("--json-out",required=True);ap.add_argument("--md-out",required=True)
    args=ap.parse_args();cfg=json.loads(POLICY.read_text(encoding="utf-8"));now=datetime.now(timezone.utc)
    report=audit_models(Path(args.input),cfg,now) if args.kind=="models" else audit_svg(Path(args.input),cfg,now)
    jp=Path(args.json_out);mp=Path(args.md_out);jp.parent.mkdir(parents=True,exist_ok=True);mp.parent.mkdir(parents=True,exist_ok=True)
    jp.write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+"\n",encoding="utf-8")
    mp.write_text(markdown(report),encoding="utf-8")
    print(json.dumps({
        "status":report["status"],"errors":report["error_count"],"warnings":report["warning_count"],
        "ready":report["bundle_ready_for_private_revalidation"],
        "issues":[{"severity":x["severity"],"code":x["code"],"source":x["source"],"scope":x["scope"],"details":x["details"]} for x in report["issues"]]
    },ensure_ascii=False))

if __name__=="__main__":
    main()
