#!/usr/bin/env python3
"""Transfer collector payload and detailed integrity reports into the private repo.

No collected payload or report is persisted in the public repository. Timestamped
objects are immutable; latest pointers are explicit overwrite-only convenience
copies for private technical reporting.
"""
from __future__ import annotations
import argparse,base64,gzip,hashlib,json,os,time
from datetime import datetime,timezone
from pathlib import Path
import requests

API="https://api.github.com"
DEFAULT_REPO="janwohlers78/mardorf-kitevorhersage"

def parse_time(value):
    if not value:return datetime.now(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z","+00:00")).astimezone(timezone.utc)

def headers(token):
    return {
        "Authorization":f"Bearer {token}",
        "Accept":"application/vnd.github+json",
        "X-GitHub-Api-Version":"2022-11-28",
        "User-Agent":"mardorf-data-collector/1.1",
    }

def existing(repo,path,h):
    r=requests.get(f"{API}/repos/{repo}/contents/{path}",headers=h,timeout=30)
    if r.status_code==404:return None
    r.raise_for_status()
    return r.json()

def put(repo,path,content,message,h,overwrite=False,idempotent_compare=None):
    meta=existing(repo,path,h)
    if meta and not overwrite:
        if idempotent_compare is not None:
            blob=requests.get(meta["download_url"],timeout=30).content
            if idempotent_compare(blob):
                return {"path":path,"idempotent":True,"commit_sha":None}
        raise RuntimeError(f"immutable destination already exists with different content: {path}")
    body={
        "message":message,
        "content":base64.b64encode(content).decode("ascii"),
        "branch":"main",
    }
    if meta:body["sha"]=meta["sha"]
    last=None
    for attempt in range(4):
        r=requests.put(f"{API}/repos/{repo}/contents/{path}",headers=h,json=body,timeout=60)
        if r.status_code in (200,201):
            data=r.json()
            return {"path":path,"idempotent":False,"commit_sha":(data.get("commit") or {}).get("sha")}
        last=f"{r.status_code}: {r.text[:800]}"
        if r.status_code in (409,429,500,502,503,504):
            time.sleep(2**attempt)
            meta=existing(repo,path,h)
            if meta and overwrite:body["sha"]=meta["sha"]
            continue
        break
    raise RuntimeError(f"private repository write failed for {path}: {last}")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--kind",required=True,choices=("models","svg"))
    ap.add_argument("--file")
    ap.add_argument("--integrity-json",required=True)
    ap.add_argument("--integrity-md",required=True)
    args=ap.parse_args()

    token=os.getenv("PRIVATE_REPO_TOKEN")
    if not token:raise RuntimeError("PRIVATE_REPO_TOKEN is not configured")
    repo=os.getenv("PRIVATE_REPO",DEFAULT_REPO);h=headers(token)

    report_raw=Path(args.integrity_json).read_bytes()
    report=json.loads(report_raw)
    when=parse_time(report.get("generated_at_utc"))
    stamp=when.strftime("%Y%m%dT%H%M%SZ")
    day=f"{when:%Y/%m/%d}"
    md_raw=Path(args.integrity_md).read_bytes()
    writes=[]

    if args.file and Path(args.file).exists():
        raw=Path(args.file).read_bytes()
        packed=gzip.compress(raw,compresslevel=9,mtime=0)
        bundle_dest=f"data/inbox/public_collector/{args.kind}/{day}/{args.kind}_{stamp}.json.gz"
        writes.append(put(
            repo,bundle_dest,packed,f"collector: ingest {args.kind} payload {stamp}",h,False,
            lambda blob:gzip.decompress(blob)==raw
        ))
        report["private_payload"]={
            "destination":bundle_dest,
            "source_sha256":hashlib.sha256(raw).hexdigest(),
            "source_bytes":len(raw),
            "compressed_bytes":len(packed),
        }
        report_raw=(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+"\n").encode()

    json_immutable=f"data/inbox/public_collector/integrity/{args.kind}/{day}/integrity_{stamp}.json"
    md_immutable=f"reports/collector-health/{args.kind}/{day}/integrity_{stamp}.md"
    json_latest=f"data/inbox/public_collector/integrity/{args.kind}/latest.json"
    md_latest=f"reports/collector-health/{args.kind}/latest.md"

    writes.append(put(repo,json_immutable,report_raw,f"collector: record {args.kind} integrity {stamp}",h,False,lambda b:b==report_raw))
    writes.append(put(repo,md_immutable,md_raw,f"collector: record {args.kind} health report {stamp}",h,False,lambda b:b==md_raw))
    writes.append(put(repo,json_latest,report_raw,f"collector: update {args.kind} integrity latest",h,True))
    writes.append(put(repo,md_latest,md_raw,f"collector: update {args.kind} health latest",h,True))
    print(json.dumps({"kind":args.kind,"stamp":stamp,"writes":writes},indent=2))

if __name__=="__main__":
    main()
