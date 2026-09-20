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

def decoded_json_content(meta):
    if not meta or not meta.get("content"): return None
    try:
        raw=base64.b64decode(meta["content"].replace("\n",""))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None

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

def verify_unpublished_commit(repo,commit_sha,pending,blob_shas,h):
    commit=req("GET",f"{API}/repos/{repo}/git/commits/{commit_sha}",h)
    tree=req("GET",f"{API}/repos/{repo}/git/trees/{commit['tree']['sha']}?recursive=1",h)
    entries={x.get("path"):x for x in tree.get("tree",[]) if x.get("type")=="blob"}
    receipts=[]
    for item in pending:
        path=item["path"];expected_blob=blob_shas[path]
        entry=entries.get(path)
        if not entry:
            raise RuntimeError(f"readback tree path missing before publish: {path}")
        if entry.get("sha")!=expected_blob:
            raise RuntimeError(f"readback tree/blob SHA mismatch for {path}: expected {expected_blob} got {entry.get('sha')}")
        b=req("GET",f"{API}/repos/{repo}/git/blobs/{expected_blob}",h)
        if b.get("encoding")!="base64":
            raise RuntimeError(f"readback blob encoding unexpected for {path}: {b.get('encoding')}")
        raw=base64.b64decode((b.get("content") or "").replace("\n",""))
        if raw!=item["content"]:
            raise RuntimeError(
                f"readback content mismatch for {path}: expected_sha256={hashlib.sha256(item['content']).hexdigest()} "
                f"got_sha256={hashlib.sha256(raw).hexdigest()} expected_bytes={len(item['content'])} got_bytes={len(raw)}")
        receipt={"path":path,"blob_sha":expected_blob,"bytes":len(raw),
                 "sha256":hashlib.sha256(raw).hexdigest(),"exact_bytes_match":True}
        if item.get("gzip"):
            unpacked=gzip.decompress(raw)
            receipt["decompressed_bytes"]=len(unpacked)
            receipt["decompressed_sha256"]=hashlib.sha256(unpacked).hexdigest()
            if item.get("source_sha256") and receipt["decompressed_sha256"]!=item["source_sha256"]:
                raise RuntimeError(
                    f"readback decompressed SHA mismatch for {path}: expected {item['source_sha256']} "
                    f"got {receipt['decompressed_sha256']}")
        receipts.append(receipt)
    return receipts

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
            receipts=verify_unpublished_commit(repo,new_commit["sha"],pending,blob_shas,h)
            r=requests.patch(f"{API}/repos/{repo}/git/refs/heads/main",headers=h,timeout=60,
                             json={"sha":new_commit["sha"],"force":False})
            if r.ok:
                published=req("GET",f"{API}/repos/{repo}/git/ref/heads/main",h)
                if published.get("object",{}).get("sha")!=new_commit["sha"]:
                    raise RuntimeError(
                        f"published main ref mismatch: expected {new_commit['sha']} "
                        f"got {published.get('object',{}).get('sha')}")
                return {"idempotent":False,"commit_sha":new_commit["sha"],
                        "paths":[x["path"] for x in pending],
                        "readback_verified":True,
                        "readback_protocol":"unpublished_commit_tree_and_blob_exact_byte_readback_then_main_ref_update",
                        "readback":receipts}
            last=f"PATCH ref -> HTTP {r.status_code}: {r.text[:800]}"
        except Exception as e:
            last=f"{type(e).__name__}: {e}"
        time.sleep(2**attempt)
    raise RuntimeError(f"atomic private transfer failed after retries: {last}")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--kind",required=True,choices=("models","svg","skm"))
    ap.add_argument("--file")
    ap.add_argument("--integrity-json",required=True)
    ap.add_argument("--integrity-md",required=True)
    args=ap.parse_args()

    token=os.getenv("PRIVATE_REPO_TOKEN")
    if not token:raise RuntimeError("PRIVATE_REPO_TOKEN is not configured")
    repo=os.getenv("PRIVATE_REPO",DEFAULT_REPO);h=hdr(token)

    report=json.loads(Path(args.integrity_json).read_text(encoding="utf-8"))
    when=parse_time(report.get("generated_at_utc"));stamp=when.strftime("%Y%m%dT%H%M%SZ");day=f"{when:%Y/%m/%d}"

    latest_path=f"data/inbox/public_collector/integrity/{args.kind}/latest.json"
    previous=decoded_json_content(content_meta(repo,latest_path,h))
    nominal_minutes=180 if args.kind=="models" else 60
    continuity={
        "nominal_target_interval_minutes":nominal_minutes,
        "previous_attempt_generated_at_utc":None,
        "interval_since_previous_attempt_minutes":None,
        "interval_exceeds_1_5x_nominal":None,
        "estimated_whole_nominal_intervals_without_attempt":None,
    }
    if previous and previous.get("generated_at_utc"):
        prev_time=parse_time(previous["generated_at_utc"])
        gap=max(0.0,(when-prev_time).total_seconds()/60)
        continuity.update(
            previous_attempt_generated_at_utc=prev_time.isoformat(),
            interval_since_previous_attempt_minutes=round(gap,2),
            interval_exceeds_1_5x_nominal=gap>nominal_minutes*1.5,
            estimated_whole_nominal_intervals_without_attempt=max(0,int(gap//nominal_minutes)-1),
        )
    report["invocation_continuity"]=continuity
    report["private_transfer_protocol"]={
        "method_version":"private-transfer-readback-v1",
        "publish_gate":"unpublished commit tree + blob exact-byte readback before main ref update",
        "gzip_payload_check":"compressed bytes exact; decompressed SHA-256 must equal source_sha256",
        "main_ref_check":"main must resolve to the verified commit immediately after update"
    }

    md_text=Path(args.integrity_md).read_text(encoding="utf-8")
    md_text += (
        "\n## Collector invocation continuity\n\n"
        f"- Nominal target interval: {nominal_minutes} min.\n"
        f"- Previous transferred attempt: {continuity['previous_attempt_generated_at_utc']}.\n"
        f"- Interval since previous attempt: {continuity['interval_since_previous_attempt_minutes']} min.\n"
        f"- Interval >1.5× nominal: {continuity['interval_exceeds_1_5x_nominal']}.\n"
        f"- Estimated complete nominal slots without an attempt: {continuity['estimated_whole_nominal_intervals_without_attempt']}.\n"
    )
    md_raw=md_text.encode("utf-8")
    files=[]

    if args.file and Path(args.file).exists():
        raw=Path(args.file).read_bytes();packed=gzip.compress(raw,compresslevel=9,mtime=0)
        dest=f"data/inbox/public_collector/{args.kind}/{day}/{args.kind}_{stamp}.json.gz"
        report["private_payload"]={
            "destination":dest,"source_sha256":hashlib.sha256(raw).hexdigest(),
            "source_bytes":len(raw),"compressed_bytes":len(packed)}
        files.append({"path":dest,"content":packed,"immutable":True,"gzip":True,
                      "source_sha256":report["private_payload"]["source_sha256"]})

    report_raw=(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+"\n").encode()
    files += [
        {"path":f"data/inbox/public_collector/integrity/{args.kind}/{day}/integrity_{stamp}.json","content":report_raw,"immutable":True},
        {"path":f"reports/collector-health/{args.kind}/{day}/integrity_{stamp}.md","content":md_raw,"immutable":True},
        {"path":f"data/inbox/public_collector/integrity/{args.kind}/latest.json","content":report_raw,"immutable":False},
        {"path":f"reports/collector-health/{args.kind}/latest.md","content":md_raw,"immutable":False},
    ]
    if report.get("bundle_ready_for_private_revalidation"):
        files += [
            {"path":f"data/inbox/public_collector/integrity/{args.kind}/latest_success.json","content":report_raw,"immutable":False},
            {"path":f"reports/collector-health/{args.kind}/latest_success.md","content":md_raw,"immutable":False},
        ]
    result=atomic_commit(repo,files,f"collector: ingest {args.kind} attempt {stamp}",h)
    result.update({"kind":args.kind,"stamp":stamp})

    # Persist the verification result separately. The payload/report commit cannot
    # contain its own post-build readback result without circularly changing the
    # bytes that were just verified.
    if result.get("commit_sha") and result.get("readback_verified"):
        verified_at=datetime.now(timezone.utc)
        receipt={
            "schema_version":1,
            "method_version":"private-transfer-readback-v1",
            "kind":args.kind,
            "stamp":stamp,
            "verified_at_utc":verified_at.isoformat(),
            "verified_data_commit_sha":result["commit_sha"],
            "readback_verified":True,
            "readback_protocol":result.get("readback_protocol"),
            "readback":result.get("readback") or [],
            "payload_source_sha256":(report.get("private_payload") or {}).get("source_sha256"),
            "payload_destination":(report.get("private_payload") or {}).get("destination"),
            "publication_semantics":"The verified data commit was read back before publication; this receipt is a child commit recording that completed verification.",
        }
        receipt_raw=(json.dumps(receipt,indent=2,ensure_ascii=False,allow_nan=False)+"\n").encode()
        receipt_path=f"data/inbox/public_collector/transfer_receipts/{args.kind}/{day}/receipt_{stamp}.json"
        receipt_files=[
            {"path":receipt_path,"content":receipt_raw,"immutable":True},
            {"path":f"data/inbox/public_collector/transfer_receipts/{args.kind}/latest.json","content":receipt_raw,"immutable":False},
        ]
        rr=atomic_commit(repo,receipt_files,f"collector: record verified {args.kind} transfer {stamp}",h)
        result["transfer_receipt_path"]=receipt_path
        result["transfer_receipt_commit_sha"]=rr.get("commit_sha")
        result["transfer_receipt_commit_readback_verified"]=rr.get("readback_verified",False)
    print(json.dumps(result,indent=2))

if __name__=="__main__":
    main()
