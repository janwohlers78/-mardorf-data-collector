#!/usr/bin/env python3
"""Strict GRIB model-reference-time and validity-time identity checks."""
from datetime import datetime,timezone
import re
import subprocess

def _grib_datetime(date_value,time_value,label,path):
    ds=str(date_value).strip();ts=str(time_value).strip()
    if not ds.isdigit() or not ts.isdigit():
        raise RuntimeError(f"unparseable GRIB {label} in {path}: date={ds!r} time={ts!r}")
    return datetime.strptime(ds+ts.zfill(4),"%Y%m%d%H%M").replace(tzinfo=timezone.utc)

def grib_message_identities(path):
    p=subprocess.run(
        ["grib_get","-p","shortName,dataDate,dataTime,stepRange,validityDate,validityTime",str(path)],
        capture_output=True,text=True,check=True)
    rows=[]
    for line in p.stdout.splitlines():
        parts=line.strip().split()
        if len(parts)<6:
            continue
        short_name,data_date,data_time,step_range,validity_date,validity_time=parts[:6]
        try:
            run=_grib_datetime(data_date,data_time,"reference time",path)
            valid=_grib_datetime(validity_date,validity_time,"validity time",path)
        except Exception:
            continue
        nums=re.findall(r"\d+",str(step_range))
        step_end=int(nums[-1]) if nums else None
        rows.append({
            "short_name":short_name,
            "run_time_utc":run,
            "valid_time_utc":valid,
            "step_range":step_range,
            "step_end_hours":step_end,
        })
    if not rows:
        raise RuntimeError(f"no parseable GRIB message identities in {path}: {p.stdout[:700]!r}")
    return rows

def grib_run_times(path):
    return sorted({x["run_time_utc"] for x in grib_message_identities(path)})

def assert_grib_run_time(path,expected,context):
    expected=expected.astimezone(timezone.utc)
    observed=grib_run_times(path)
    if observed != [expected]:
        raise RuntimeError(
            f"{context}: GRIB run identity mismatch expected={expected.isoformat()} "
            f"observed={[x.isoformat() for x in observed]}")
    return observed[0]

def assert_grib_valid_time(path,expected_run,expected_valid,context):
    expected_run=expected_run.astimezone(timezone.utc)
    expected_valid=expected_valid.astimezone(timezone.utc)
    expected_lead=(expected_valid-expected_run).total_seconds()/3600
    if abs(expected_lead-round(expected_lead))>1e-9 or expected_lead<0:
        raise RuntimeError(f"{context}: expected lead is not a non-negative whole hour: {expected_lead}")
    expected_lead=int(round(expected_lead))
    rows=grib_message_identities(path)
    failures=[]
    for row in rows:
        if row["run_time_utc"]!=expected_run:
            failures.append({"short_name":row["short_name"],"reason":"run_time_mismatch",
                             "observed":row["run_time_utc"].isoformat()})
        if row["valid_time_utc"]!=expected_valid:
            failures.append({"short_name":row["short_name"],"reason":"valid_time_mismatch",
                             "observed":row["valid_time_utc"].isoformat()})
        if row["step_end_hours"]!=expected_lead:
            failures.append({"short_name":row["short_name"],"reason":"step_end_mismatch",
                             "step_range":row["step_range"],"step_end_hours":row["step_end_hours"]})
    if failures:
        raise RuntimeError(
            f"{context}: GRIB temporal identity mismatch expected_run={expected_run.isoformat()} "
            f"expected_valid={expected_valid.isoformat()} expected_lead={expected_lead}; failures={failures[:20]}")
    return expected_valid

def assert_grib_batch_leads(path,expected_run,expected_leads,context):
    expected_run=expected_run.astimezone(timezone.utc)
    expected={int(x) for x in expected_leads}
    rows=grib_message_identities(path)
    observed=set();failures=[]
    for row in rows:
        if row["run_time_utc"]!=expected_run:
            failures.append({"short_name":row["short_name"],"reason":"run_time_mismatch",
                             "observed":row["run_time_utc"].isoformat()})
            continue
        lead=(row["valid_time_utc"]-row["run_time_utc"]).total_seconds()/3600
        if abs(lead-round(lead))>1e-9 or lead<0:
            failures.append({"short_name":row["short_name"],"reason":"non_integral_validity_lead",
                             "valid_time_utc":row["valid_time_utc"].isoformat(),"lead_hours":lead})
            continue
        lead=int(round(lead));observed.add(lead)
        if row["step_end_hours"]!=lead:
            failures.append({"short_name":row["short_name"],"reason":"step_validity_disagreement",
                             "step_range":row["step_range"],"step_end_hours":row["step_end_hours"],
                             "validity_lead_hours":lead})
        if lead not in expected:
            failures.append({"short_name":row["short_name"],"reason":"unexpected_lead",
                             "lead_hours":lead,"expected_leads":sorted(expected)})
    missing=sorted(expected-observed)
    if missing:
        failures.append({"reason":"expected_leads_missing_from_grib_batch","missing_leads":missing,
                         "observed_leads":sorted(observed)})
    if failures:
        raise RuntimeError(f"{context}: GRIB batch temporal identity mismatch: {failures[:30]}")
    return sorted(observed)
