#!/usr/bin/env python3
"""Transfer one collector bundle into the private repository.

The public repository never stores the payload. The destination is a unique,
gzip-compressed file under data/inbox/public_collector/ in the private repo.
"""
from __future__ import annotations
import argparse,base64,gzip,hashlib,json,os,re,time
from datetime import datetime,timezone
from pathlib import Path
import requests

API="https://api.github.com"
DEFAULT_REPO="janwohlers78/mardorf-kitevorhersage"

def parse_time(value):
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z","+00:00")).astimezone(timezone.utc)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--kind",required=True,choices=("models","svg"))
    ap.add_argument("--file",required=True)
    args=ap.parse_args()
    token=os.getenv("PRIVATE_REPO_TOKEN")
    if not token:
        raise RuntimeError("PRIVATE_REPO_TOKEN is not configured")
    repo=os.getenv("PRIVATE_REPO",DEFAULT_REPO)
    raw=Path(args.file).read_bytes()
    obj=json.loads(raw)
    when=parse_time(obj.get("retrieved_at_utc") or obj.get("collector_retrieved_at_utc"))
    stamp=when.strftime("%Y%m%dT%H%M%SZ")
    dest=f"data/inbox/public_collector/{args.kind}/{when:%Y/%m/%d}/{args.kind}_{stamp}.json.gz"
    packed=gzip.compress(raw,compresslevel=9,mtime=0)
    digest=hashlib.sha256(raw).hexdigest()
    url=f"{API}/repos/{repo}/contents/{dest}"
    headers={
        "Authorization":f"Bearer {token}",
        "Accept":"application/vnd.github+json",
        "X-GitHub-Api-Version":"2022-11-28",
        "User-Agent":"mardorf-data-collector/1.0",
    }
    body={
        "message":f"collector: ingest {args.kind} {stamp}",
        "content":base64.b64encode(packed).decode("ascii"),
        "branch":"main",
    }
    last=None
    for attempt in range(4):
        r=requests.put(url,headers=headers,json=body,timeout=60)
        if r.status_code in (200,201):
            data=r.json()
            print(json.dumps({
                "destination":dest,
                "source_bytes":len(raw),
                "compressed_bytes":len(packed),
                "source_sha256":digest,
                "commit_sha":(data.get("commit") or {}).get("sha"),
            }))
            return
        # A timestamped path should normally be unique. Treat an already-existing
        # identical object as idempotent rather than creating another copy.
        if r.status_code==422 and "sha" in r.text.lower():
            g=requests.get(url,headers=headers,timeout=30)
            if g.ok:
                existing=g.json()
                blob=requests.get(existing["download_url"],timeout=30).content
                if gzip.decompress(blob)==raw:
                    print(json.dumps({"destination":dest,"idempotent":True,"source_sha256":digest}))
                    return
        last=f"{r.status_code}: {r.text[:500]}"
        if r.status_code in (429,500,502,503,504):
            time.sleep(2**attempt)
            continue
        break
    raise RuntimeError(f"private repository transfer failed: {last}")

if __name__=="__main__":
    main()
