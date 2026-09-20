#!/usr/bin/env python3
"""Decide whether a scheduled collector run is due from private latest_success state.

The check is intentionally fail-open: if private state cannot be read, acquisition
runs and the reason is emitted explicitly instead of silently skipping data.
"""
from __future__ import annotations
import argparse,base64,json,os,urllib.error,urllib.request
from datetime import datetime,timezone

API="https://api.github.com"
DEFAULT_REPO="janwohlers78/mardorf-kitevorhersage"
FUTURE_TOLERANCE_MINUTES=15

def parse_time(value):
    x=datetime.fromisoformat(str(value).replace("Z","+00:00"))
    return x.astimezone(timezone.utc) if x.tzinfo else x.replace(tzinfo=timezone.utc)

def output(name,value):
    p=os.getenv("GITHUB_OUTPUT")
    if p:
        with open(p,"a",encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    print(f"{name}={value}")

def evaluate_latest_success(stamp,now,max_age_minutes,future_tolerance_minutes=FUTURE_TOLERANCE_MINUTES):
    t=parse_time(stamp)
    age=(now-t).total_seconds()/60
    if age < -float(future_tolerance_minutes):
        return True,"latest_success_timestamp_future_fail_open",age
    age=max(0.0,age)
    due=age>=float(max_age_minutes)
    return due,("last_success_age_exceeds_threshold" if due else "last_success_within_threshold"),age

def fetch_latest(repo,kind,token):
    path=f"data/inbox/public_collector/integrity/{kind}/latest_success.json"
    url=f"{API}/repos/{repo}/contents/{path}?ref=main"
    req=urllib.request.Request(url,headers={
        "Authorization":f"Bearer {token}",
        "Accept":"application/vnd.github+json",
        "X-GitHub-Api-Version":"2022-11-28",
        "User-Agent":"mardorf-data-collector-due-check/1.0",
    })
    with urllib.request.urlopen(req,timeout=20) as r:
        meta=json.loads(r.read().decode("utf-8"))
    raw=base64.b64decode(meta["content"].replace("\n",""))
    return json.loads(raw.decode("utf-8"))

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--kind",required=True,choices=("svg","models","wunstorf","etnw"))
    ap.add_argument("--max-age-minutes",required=True,type=float)
    args=ap.parse_args()
    token=os.getenv("PRIVATE_REPO_TOKEN","")
    repo=os.getenv("PRIVATE_REPO",DEFAULT_REPO)
    now=datetime.now(timezone.utc)

    due=True;reason="unknown";age=None;stamp=None
    if not token:
        reason="private_repo_token_not_configured_fail_open"
    else:
        try:
            latest=fetch_latest(repo,args.kind,token)
            stamp=latest.get("generated_at_utc")
            if not stamp:
                reason="latest_success_has_no_generated_at_fail_open"
            else:
                due,reason,age=evaluate_latest_success(stamp,now,args.max_age_minutes)
        except urllib.error.HTTPError as e:
            reason=f"private_latest_success_http_{e.code}_fail_open"
        except Exception as e:
            reason=f"private_latest_success_read_{type(e).__name__}_fail_open"

    output("due","true" if due else "false")
    output("reason",reason)
    output("last_success_generated_at_utc",stamp or "none")
    output("age_minutes",f"{age:.2f}" if age is not None else "unknown")
    output("threshold_minutes",f"{args.max_age_minutes:.2f}")

if __name__=="__main__":
    main()
