#!/usr/bin/env python3
"""Atomically transfer one collector attempt into the private repository.

One collector run creates at most one private commit containing the optional
gzip payload, immutable JSON/Markdown integrity reports and latest pointers.
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

def hdr(token):
    return {
        "Authorization":f"Bearer {token}",
        "Accept":"application/vnd.github+json",
        "X-GitHub-Api-Version":"2022-11-28",
        "User-Agent":"mardorf-data-collector/1.2",
    }

def req(method,url,h,**kwargs):
    r=requests.request(method,url,headers=h,timeout=60,**kwargs)
    if not r.ok:
        raise RuntimeError(f"{method} {url} -> HTTP {r.status_code}: {r.text[:800]}")
    return r.json() if r.content else {}

def content_meta(repo,path,h):
    r=requests.get(f"{API}/repos/{repo}/contents/{path}",headers=h,timeout=30)
    if r.status_code==404:return None
    if not r.ok:raise RuntimeError(f"GET content {path} -> HTTP {r.status_code}: {r.text[:500]}")
    return r.json()

def same_existing(repo,path,content,h,gz=False):
    meta=content_meta(repo,path,h)
    if not meta:return False
    blob=requests.get(meta["download_url"],timeout=30).content
    try:existing=gzip.decompress(blob) if gz else blob
    except Exception:return False
    target=gzip.decompress(content) if gz else content
    if existing!=target:
        raise RuntimeError(f"immutable destination already exists with different content: {path}")
    return True

def blob(repo,content,h):
    d=req("POST",f"{API}/repos/{repo}/git/blobs",h,json={
        "content":base64.b64encode(content).decode("ascii"),"encoding":"base64"})
    return d["sha"]

def atomic_commit(repo,files,message,h):
    # Immutable entries may already exist after an earlier successful retry.
    pending=[]
    for item in files:
        if item.get("immutable") and same_existing(repo,item["path"],item["content"],h,item.get("gzip",False)):
            continue
        pending.append(item)
    if not pending:return {"idempotent":True,"commit_sha":None,"paths":[x["path"] for x in files]}

    blob_shas={x["path"]:blob(repo,x["content"],h) for x in pending}
    last=None
    for attempt in range(4):
        try:
            ref=req("GET",f"{API}/repos/{repo}/git/ref/heads/main",h)
            parent=ref["object"]["sha"]
            commit=req("GET",f"{API}/repos/{repo}/git/commits/{parent}",h)
            tree_entries=[{"path":x["path"],"mode":"100644","type":"blob","sha":blob_shas[x["path"]]} for x in pending]
            tree=req("POST",f"{API}/repos/{repo}/git/trees",h,json={
                "base_tree":commit["tree"]["sha"],"tree":tree_entries})
            new_commit=req("POST",f"{API}/repos/{repo}/git/commits",h,json={
                "message":message,"tree":tree["sha"],"parents":[parent]})
            r=requests.patch(f"{API}/repos/{repo}/git/refs/heads/main",headers=h,timeout=60,
                             json={"sha":new_commit["sha"],"force":False})
            if r.ok:
                return {"idempotent":False,"commit_sha":new_commit["sha"],"paths":[x["path"] for x in pending]}
            last=f"PATCH ref -> HTTP {r.status_code}: {r.text[:800]}"
        except Exception as e:
            last=f"{type(e).__name__}: {e}"
        time.sleep(2**attempt)
    raise RuntimeError(f"atomic private transfer failed after retries: {last}")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--kind",required=True,choices=("models","svg"))
    ap.add_argument("--file")
    ap.add_argument("--integrity-json",required=True)
    ap.add_argument("--integrity-md",required=True)
    args=ap.parse_args()

    token=os.getenv("PRIVATE_REPO_TOKEN")
    if not token:raise RuntimeError("PRIVATE_REPO_TOKEN is not configured")
    repo=os.getenv("PRIVATE_REPO",DEFAULT_REPO);h=hdr(token)

    report=json.loads(Path(args.integrity_json).read_text(encoding="utf-8"))
    when=parse_time(report.get("generated_at_utc"));stamp=when.strftime("%Y%m%dT%H%M%SZ");day=f"{when:%Y/%m/%d}"
    md_raw=Path(args.integrity_md).read_bytes()
    files=[]

    if args.file and Path(args.file).exists():
        raw=Path(args.file).read_bytes();packed=gzip.compress(raw,compresslevel=9,mtime=0)
        dest=f"data/inbox/public_collector/{args.kind}/{day}/{args.kind}_{stamp}.json.gz"
        report["private_payload"]={
            "destination":dest,"source_sha256":hashlib.sha256(raw).hexdigest(),
            "source_bytes":len(raw),"compressed_bytes":len(packed)}
        files.append({"path":dest,"content":packed,"immutable":True,"gzip":True})

    report_raw=(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+"\n").encode()
    files += [
        {"path":f"data/inbox/public_collector/integrity/{args.kind}/{day}/integrity_{stamp}.json","content":report_raw,"immutable":True},
        {"path":f"reports/collector-health/{args.kind}/{day}/integrity_{stamp}.md","content":md_raw,"immutable":True},
        {"path":f"data/inbox/public_collector/integrity/{args.kind}/latest.json","content":report_raw,"immutable":False},
        {"path":f"reports/collector-health/{args.kind}/latest.md","content":md_raw,"immutable":False},
    ]
    result=atomic_commit(repo,files,f"collector: ingest {args.kind} attempt {stamp}",h)
    result.update({"kind":args.kind,"stamp":stamp})
    print(json.dumps(result,indent=2))

if __name__=="__main__":
    main()
