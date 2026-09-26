#!/usr/bin/env python3
"""Plan provider-specific model work against private archived cycle evidence.

Phase 2E-2 keeps the existing bundle-level freshness gate as a cheap watchdog,
then performs provider-cycle discovery before any expensive full download.
A provider is carried forward only when:
  * the newest selected provider cycle matches the last verified public payload,
  * that exact cycle has private Archive-v4 cycle evidence, and
  * the prior payload still contains records for that cycle.

The script is deliberately fail-open. Any probe/private-state ambiguity marks
that provider for fetch rather than risking a missed cycle.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fetch_model_data as base
import fetch_extra_models as extra
import fetch_dwd_additional_models as dwd
from full_horizon_contract import maximum_hours

API="https://api.github.com"
DEFAULT_REPO="janwohlers78/mardorf-kitevorhersage"
MODELS=("ICON-D2","GFS","GEFS-control","ICON-EU","ICON-D2-EPS","ECMWF-IFS")
OUTPUT_KEYS={
    "ICON-D2":"icon_d2",
    "GFS":"gfs",
    "GEFS-control":"gefs_control",
    "ICON-EU":"icon_eu",
    "ICON-D2-EPS":"icon_d2_eps",
    "ECMWF-IFS":"ecmwf_ifs",
}
METHOD_VERSION="provider-cycle-gate-v1"


def utc(value):
    x=datetime.fromisoformat(str(value).replace("Z","+00:00"))
    return x.astimezone(timezone.utc) if x.tzinfo else x.replace(tzinfo=timezone.utc)


def output(name,value):
    path=os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path,"a",encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    print(f"{name}={value}")


def _request(repo,path,token):
    url=f"{API}/repos/{repo}/contents/{path}?ref=main"
    req=urllib.request.Request(url,headers={
        "Authorization":f"Bearer {token}",
        "Accept":"application/vnd.github+json",
        "X-GitHub-Api-Version":"2022-11-28",
        "User-Agent":"mardorf-data-collector-provider-cycle-gate/1.0",
    })
    with urllib.request.urlopen(req,timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def private_bytes(repo,path,token):
    meta=_request(repo,path,token)
    raw=meta.get("content")
    if not raw:
        raise RuntimeError(f"private contents API returned no inline content for {path}")
    return base64.b64decode(raw.replace("\n",""))


def private_json(repo,path,token):
    return json.loads(private_bytes(repo,path,token).decode("utf-8"))


def private_json_optional(repo,path,token):
    try:
        return private_json(repo,path,token)
    except urllib.error.HTTPError as exc:
        if exc.code==404:
            return None
        raise


def evidence_path(model,run):
    safe=model.lower().replace("_","-")
    return (
        f"data/weather_archive/cycle_evidence/{safe}/"
        f"year={run:%Y}/month={run:%m}/day={run:%d}/run={run:%Y%m%dT%H%M%SZ}.json"
    )


def exact_archived_cycle(repo,token,model,run):
    item=private_json_optional(repo,evidence_path(model,run),token)
    if not isinstance(item,dict):
        return None
    try:
        recorded=utc(item.get("run_time_utc"))
    except Exception:
        return None
    if recorded!=run or item.get("model")!=model:
        return None
    return item


def _ecmwf_probe(client,target,run=None,step=48):
    kwargs={}
    if run is not None:
        kwargs.update(date=run.strftime("%Y%m%d"),time=run.hour)
    client.retrieve(
        stream="oper",type="fc",step=[int(step)],param=["10u"],
        target=str(target),**kwargs)
    actual=extra.grib_run_time(target)
    if run is not None and actual!=run:
        raise RuntimeError(
            f"ECMWF probe returned wrong cycle: requested={run.isoformat()} actual={actual.isoformat()}")
    return actual


def _ecmwf_cycle(full_validation=True):
    """Probe the newest ECMWF cycle mature enough for this collection mode."""
    source=os.getenv("ECMWF_OPEN_DATA_SOURCE","azure")
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)
        client=extra.Client(source=source,model="ifs",resol="0p25",maximum_retries=2,retry_after=5)
        latest=_ecmwf_probe(client,root/"latest_f048.grib2",step=48)
        if not full_validation:
            return latest
        candidate=latest
        failures=[]
        for idx in range(8):
            terminal=maximum_hours("ECMWF-IFS",candidate)
            try:
                _ecmwf_probe(
                    client,root/f"candidate_{idx}_{terminal}.grib2",
                    run=candidate,step=terminal)
                return candidate
            except Exception as exc:
                failures.append({
                    "run_time_utc":candidate.isoformat(),
                    "terminal_lead_hours":terminal,
                    "exception_type":type(exc).__name__,
                    "exception_message":str(exc)[:300],
                })
                candidate=candidate-timedelta(hours=6)
        raise RuntimeError(f"No mature ECMWF cycle found from latest={latest.isoformat()}; failures={failures}")


def discover(model,full_validation=True):
    if model=="ICON-D2":
        return utc(datetime.strptime(base.latest_dwd_icon_d2_cycle(48),"%Y%m%d%H").replace(tzinfo=timezone.utc))
    if model=="GFS":
        lead=384 if full_validation else 48
        return utc(datetime.strptime(base.discover_gfs_cycle(lead),"%Y%m%d%H").replace(tzinfo=timezone.utc))
    if model=="GEFS-control":
        cycle,_evidence=extra.discover_gefs(
            48,require_far_horizon=bool(full_validation),return_evidence=True)
        return utc(datetime.strptime(cycle,"%Y%m%d%H").replace(tzinfo=timezone.utc))
    if model=="ICON-EU":
        try:
            cycle=dwd.discover_cycle("icon-eu",120)
        except Exception:
            cycle=dwd.discover_cycle("icon-eu",48)
        return utc(datetime.strptime(cycle,"%Y%m%d%H").replace(tzinfo=timezone.utc))
    if model=="ICON-D2-EPS":
        meta=dwd.fetch_eps_metadata()
        run=utc(meta["last_run_initialisation_time_utc"])
        available=utc(meta["last_run_availability_time_utc"])
        age=(datetime.now(timezone.utc)-available).total_seconds()
        if age < dwd.EPS_SETTLING_SECONDS:
            raise RuntimeError(
                f"ICON-D2-EPS newest run is not settled: run={run.isoformat()} age_seconds={age:.1f}")
        dwd.find_dwd_file("icon-d2-eps",run.strftime("%Y%m%d%H"),48,"u_10m")
        return run
    if model=="ECMWF-IFS":
        return _ecmwf_cycle(full_validation)
    raise ValueError(model)


def gefs_supplemental_due(payload,run,checked_at):
    archive=payload.get("full_horizon_archive") if isinstance(payload.get("full_horizon_archive"),dict) else {}
    sources=archive.get("sources") if isinstance(archive.get("sources"),dict) else {}
    source=sources.get("GEFS-control")
    if not isinstance(source,dict) or source.get("run_time_utc")!=run.isoformat():
        return False,None
    state=source.get("gefs_pgrb2b_supplemental_retry")
    if not isinstance(state,dict) or state.get("status")!="pending":
        return False,state
    try:
        attempts=int(state.get("attempts",0))
        maximum=int(state.get("maximum_attempts",1))
        due=utc(state.get("next_retry_not_before_utc"))
    except Exception:
        return False,state
    return attempts<maximum and checked_at>=due,state


def rows_match_cycle(payload,model,run):
    rows=[x for x in (payload.get("models") or {}).get(model,[]) if isinstance(x,dict)]
    if not rows:
        return False
    runs=set()
    for row in rows:
        try:
            runs.add(utc(row.get("run_time_utc")))
        except Exception:
            return False
    return runs=={run}


def source_matches_cycle(latest,model,run):
    source=(latest.get("sources") or {}).get(model)
    if not isinstance(source,dict):
        return False
    try:
        selected=utc(source.get("selected_run_time_utc"))
    except Exception:
        return False
    return (
        selected==run
        and source.get("provider_cycle_complete") is True
    )


def load_seed(repo,token):
    latest=private_json(repo,"data/inbox/public_collector/integrity/models/latest_success.json",token)
    private=latest.get("private_payload") if isinstance(latest.get("private_payload"),dict) else {}
    dest=private.get("destination")
    if not dest:
        raise RuntimeError("private latest_success has no model payload destination")
    packed=private_bytes(repo,dest,token)
    raw=gzip.decompress(packed)
    payload=json.loads(raw.decode("utf-8"))
    expected=private.get("source_sha256")
    if not isinstance(payload,dict) or not expected:
        raise RuntimeError("private seed payload identity is incomplete")
    import hashlib
    actual=hashlib.sha256(raw).hexdigest()
    if actual!=expected:
        raise RuntimeError(f"private seed payload SHA mismatch expected={expected} actual={actual}")
    return latest,payload,dest,actual


def build_plan(repo,token,full_validation=True,discover_fn=discover):
    checked=datetime.now(timezone.utc)
    latest,seed,seed_path,seed_sha=load_seed(repo,token)
    models={}
    for model in MODELS:
        entry={"action":"fetch","reason":"probe_not_run_fail_open"}
        try:
            run=discover_fn(model,full_validation)
            archived=exact_archived_cycle(repo,token,model,run)
            source_ok=source_matches_cycle(latest,model,run)
            rows_ok=rows_match_cycle(seed,model,run)
            entry.update(
                selected_run_time_utc=run.isoformat(),
                archive_cycle_evidence_path=evidence_path(model,run),
                archive_cycle_evidence_present=bool(archived),
                latest_success_source_matches=bool(source_ok),
                seed_payload_rows_match=bool(rows_ok),
            )
            if archived and source_ok and rows_ok:
                supplement_due,supplement_state=(
                    gefs_supplemental_due(seed,run,checked)
                    if model=="GEFS-control" else (False,None)
                )
                if supplement_due:
                    entry.update(
                        action="supplemental_retry",
                        reason="archived_cycle_has_due_bounded_pgrb2b_retry",
                        supplemental_retry_state=supplement_state,
                    )
                else:
                    entry.update(action="carry_forward",reason="selected_cycle_already_archived")
            else:
                missing=[]
                if not archived: missing.append("private_cycle_evidence")
                if not source_ok: missing.append("latest_success_source")
                if not rows_ok: missing.append("seed_payload_rows")
                entry["reason"]="fetch_fail_open_missing_"+"_".join(missing)
        except Exception as exc:
            entry.update(
                action="fetch",
                reason="provider_cycle_probe_failed_fail_open",
                probe_exception_type=type(exc).__name__,
                probe_exception_message=str(exc)[:700],
            )
        models[model]=entry

    any_work=any(x["action"]!="carry_forward" for x in models.values())
    plan={
        "schema_version":1,
        "method_version":METHOD_VERSION,
        "checked_at_utc":checked.isoformat(),
        "private_repo":repo,
        "full_validation":bool(full_validation),
        "seed_payload_path":seed_path,
        "seed_payload_sha256":seed_sha,
        "seed_integrity_generated_at_utc":latest.get("generated_at_utc"),
        "models":models,
        "any_work":any_work,
        "delta_prediction":"nonzero" if any_work else "zero",
        "no_op_transfer_suppressed":not any_work,
    }
    return plan,seed


def prepare_seed(seed,plan,path):
    out=json.loads(json.dumps(seed))
    out["retrieved_at_utc"]=plan["checked_at_utc"]
    out["provider_attempts"]=[]
    out["provider_cycle_gate"]=plan
    out.setdefault("quality",{}).setdefault("errors",[])
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    Path(path).write_text(json.dumps(out,separators=(",",":"),allow_nan=False)+"\n",encoding="utf-8")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--output",default=os.getenv("COLLECTOR_MODEL_FILE","work/model_snapshot.json"))
    ap.add_argument("--plan-out",default="work/provider_cycle_plan.json")
    args=ap.parse_args()
    token=os.getenv("PRIVATE_REPO_TOKEN","")
    repo=os.getenv("PRIVATE_REPO",DEFAULT_REPO)
    if not token:
        raise RuntimeError("PRIVATE_REPO_TOKEN is not configured for provider cycle gate")
    full=os.getenv("FULL_VALIDATION","").lower()=="true"

    try:
        plan,seed=build_plan(repo,token,full_validation=full)
    except Exception as exc:
        # If the shared private seed cannot be proven, fetch every provider.
        now=datetime.now(timezone.utc).isoformat()
        plan={
            "schema_version":1,
            "method_version":METHOD_VERSION,
            "checked_at_utc":now,
            "private_repo":repo,
            "full_validation":full,
            "models":{m:{
                "action":"fetch",
                "reason":"global_private_seed_unavailable_fail_open",
                "probe_exception_type":type(exc).__name__,
                "probe_exception_message":str(exc)[:700],
            } for m in MODELS},
            "any_work":True,
            "delta_prediction":"nonzero",
            "no_op_transfer_suppressed":False,
        }
        seed={
            "schema_version":2,
            "retrieved_at_utc":now,
            "spot":{"lat":base.LAT,"lon":base.LON},
            "mode":"production",
            "leads_hours":[],
            "models":{},
            "quality":{"errors":[]},
            "provider_attempts":[],
        }

    Path(args.plan_out).parent.mkdir(parents=True,exist_ok=True)
    Path(args.plan_out).write_text(json.dumps(plan,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    if plan["any_work"]:
        prepare_seed(seed,plan,args.output)

    output("any_work","true" if plan["any_work"] else "false")
    output("delta_prediction",plan["delta_prediction"])
    for model,key in OUTPUT_KEYS.items():
        output(key,plan["models"][model]["action"])
    print(json.dumps(plan,indent=2,sort_keys=True))


if __name__=="__main__":
    main()
