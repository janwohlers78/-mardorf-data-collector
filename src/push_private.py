#!/usr/bin/env python3
"""Atomically transfer one collector attempt into the private repository.

Immutable payload/integrity history is published only after exact-byte readback.
Mutable latest pointers are monotonic by source generation time. A successful
latest_success pointer is finalized only in the same verified commit as the
transfer receipt, so scheduling can never treat an unreceipted attempt as success.
"""
from __future__ import annotations
import argparse,base64,gzip,hashlib,json,os,time
from datetime import datetime,timezone
from pathlib import Path
import requests

API="https://api.github.com"
DEFAULT_REPO="janwohlers78/mardorf-kitevorhersage"

def parse_time(value):
    if not value:
        raise ValueError("required timestamp is missing")
    x=datetime.fromisoformat(str(value).replace("Z","+00:00"))
    if x.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {value!r}")
    return x.astimezone(timezone.utc)

def workflow_provenance():
    """Return GitHub Actions invocation identity for persistent transfer audit."""
    return {
        "run_id":os.getenv("GITHUB_RUN_ID") or None,
        "run_attempt":os.getenv("GITHUB_RUN_ATTEMPT") or None,
        "workflow":os.getenv("GITHUB_WORKFLOW") or None,
        "job":os.getenv("GITHUB_JOB") or None,
        "event_name":os.getenv("GITHUB_EVENT_NAME") or None,
        "ref":os.getenv("GITHUB_REF") or None,
        "sha":os.getenv("GITHUB_SHA") or None,
        "repository":os.getenv("GITHUB_REPOSITORY") or None,
    }

def hdr(token):
    return {
        "Authorization":f"Bearer {token}",
        "Accept":"application/vnd.github+json",
        "X-GitHub-Api-Version":"2022-11-28",
        "User-Agent":"mardorf-data-collector/1.3",
    }

def req(method,url,h,**kwargs):
    r=requests.request(method,url,headers=h,timeout=60,**kwargs)
    if not r.ok:
        raise RuntimeError(f"{method} {url} -> HTTP {r.status_code}: {r.text[:800]}")
    return r.json() if r.content else {}

def content_meta(repo,path,h,ref=None):
    params={"ref":ref} if ref else None
    r=requests.get(f"{API}/repos/{repo}/contents/{path}",headers=h,timeout=30,params=params)
    if r.status_code==404:return None
    if not r.ok:raise RuntimeError(f"GET content {path} -> HTTP {r.status_code}: {r.text[:500]}")
    return r.json()

def decoded_bytes(meta):
    if not meta or not meta.get("content"):return None
    try:return base64.b64decode(meta["content"].replace("\n",""))
    except Exception:return None

def decoded_json_content(meta):
    raw=decoded_bytes(meta)
    if raw is None:return None
    try:return json.loads(raw.decode("utf-8"))
    except Exception:return None

def same_existing(repo,path,content,h,gz=False,ref=None):
    """Return the existing blob SHA only when immutable bytes match exactly."""
    meta=content_meta(repo,path,h,ref=ref)
    if not meta:return False
    sha=meta.get("sha")
    if not sha:
        raise RuntimeError(f"immutable destination has no blob SHA: {path}")
    b=req("GET",f"{API}/repos/{repo}/git/blobs/{sha}",h)
    if b.get("encoding")!="base64":
        raise RuntimeError(f"immutable destination has unexpected blob encoding: {path}")
    existing=base64.b64decode((b.get("content") or "").replace("\n",""))
    if existing!=content:
        detail=""
        if gz:
            try:
                detail=(
                    f"; existing_decompressed_sha256={hashlib.sha256(gzip.decompress(existing)).hexdigest()}"
                    f" target_decompressed_sha256={hashlib.sha256(gzip.decompress(content)).hexdigest()}")
            except Exception:
                detail="; gzip_decompression_failed_while_comparing"
        raise RuntimeError(
            f"immutable destination already exists with different exact bytes: {path}"
            f"; existing_sha256={hashlib.sha256(existing).hexdigest()}"
            f" target_sha256={hashlib.sha256(content).hexdigest()}{detail}")
    return sha

def blob(repo,content,h):
    d=req("POST",f"{API}/repos/{repo}/git/blobs",h,json={
        "content":base64.b64encode(content).decode("ascii"),"encoding":"base64"})
    return d["sha"]

def timestamp_not_older(current_value,incoming_value):
    return parse_time(incoming_value)>=parse_time(current_value)

def monotonic_allows(repo,item,h,ref=None):
    guard=item.get("monotonic_guard")
    if not isinstance(guard,dict):return True
    current=decoded_json_content(content_meta(repo,guard["path"],h,ref=ref))
    if not current:return True
    current_value=current.get(guard["field"])
    if not current_value:return True
    return timestamp_not_older(current_value,guard["incoming_time"])

def mutable_already_exact(repo,item,h,ref=None):
    if item.get("immutable"):return False
    meta=content_meta(repo,item["path"],h,ref=ref)
    raw=decoded_bytes(meta)
    return raw==item["content"] if raw is not None else False

def descendant_preserves(repo,new_commit_sha,current_sha,pending,h):
    """Accept a later main descendant only if it preserves this publication."""
    if current_sha==new_commit_sha:
        return True
    comparison=req("GET",f"{API}/repos/{repo}/compare/{new_commit_sha}...{current_sha}",h)
    if comparison.get("status") not in ("ahead","identical"):
        return False
    for item in pending:
        if item.get("immutable"):
            if not same_existing(
                repo,item["path"],item["content"],h,item.get("gzip",False),ref=current_sha
            ):
                return False
            continue
        guard=item.get("monotonic_guard")
        if isinstance(guard,dict):
            current=decoded_json_content(content_meta(repo,guard["path"],h,ref=current_sha))
            if not current:
                return False
            current_value=current.get(guard["field"])
            if not current_value or parse_time(current_value)<parse_time(guard["incoming_time"]):
                return False
            continue
        if not mutable_already_exact(repo,item,h,ref=current_sha):
            return False
    return True

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

REF_UPDATE_MAX_ATTEMPTS=8
REF_UPDATE_BACKOFF_SECONDS=(1,2,4,8,12,16,20,30)

def ref_retry_delay(attempt):
    if attempt < 0:
        raise ValueError("attempt must be nonnegative")
    return REF_UPDATE_BACKOFF_SECONDS[min(attempt,len(REF_UPDATE_BACKOFF_SECONDS)-1)]

def atomic_commit(repo,files,message,h):
    """Commit with exact-byte readback and parent-bound monotonic checks.

    Each retry reads main first. All monotonic guards, immutable collision checks,
    idempotence checks and the base tree are then resolved against that exact
    parent SHA. A failed non-force ref update restarts the complete check/build
    sequence from the newly observed parent.
    """
    all_items=list(files)
    # Blob creation is content-addressed and independent of the eventual parent.
    blob_shas={item["path"]:blob(repo,item["content"],h) for item in all_items}

    last=None
    for attempt in range(REF_UPDATE_MAX_ATTEMPTS):
        try:
            ref=req("GET",f"{API}/repos/{repo}/git/ref/heads/main",h)
            parent=ref["object"]["sha"]

            pending=[]
            skipped_stale=[]
            skipped_exact=[]
            for item in all_items:
                if item.get("immutable"):
                    existing=same_existing(
                        repo,item["path"],item["content"],h,item.get("gzip",False),ref=parent
                    )
                    if existing:
                        skipped_exact.append(item["path"])
                        continue
                if not monotonic_allows(repo,item,h,ref=parent):
                    skipped_stale.append(item["path"]);continue
                if mutable_already_exact(repo,item,h,ref=parent):
                    skipped_exact.append(item["path"]);continue
                pending.append(item)
            if not pending:
                return {"idempotent":True,"commit_sha":None,"paths":[],
                        "parent_sha":parent,
                        "skipped_stale_paths":skipped_stale,"skipped_exact_paths":skipped_exact,
                        "readback_verified":True,"readback":[]}

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
                current_sha=published.get("object",{}).get("sha")
                if not current_sha or not descendant_preserves(
                    repo,new_commit["sha"],current_sha,pending,h
                ):
                    raise RuntimeError(
                        f"published commit is not preserved by current main: "
                        f"published={new_commit['sha']} current={current_sha}")
                return {"idempotent":False,"commit_sha":new_commit["sha"],
                        "parent_sha":parent,"validated_main_sha":current_sha,
                        "paths":[x["path"] for x in pending],
                        "skipped_stale_paths":skipped_stale,"skipped_exact_paths":skipped_exact,
                        "readback_verified":True,
                        "readback_protocol":"parent-bound guards + unpublished exact-byte readback + non-force main update + descendant preservation check",
                        "readback":receipts}
            last=f"PATCH ref -> HTTP {r.status_code}: {r.text[:800]}"
        except Exception as e:
            last=f"{type(e).__name__}: {e}"
        if attempt < REF_UPDATE_MAX_ATTEMPTS-1:
            time.sleep(ref_retry_delay(attempt))
    raise RuntimeError(f"atomic private transfer failed after retries: {last}")

def pointer_item(path,content,guard_path,field,incoming_time):
    return {"path":path,"content":content,"immutable":False,
            "monotonic_guard":{"path":guard_path,"field":field,"incoming_time":incoming_time}}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--kind",required=True,choices=("models","svg","skm","wunstorf","etnw"))
    ap.add_argument("--file")
    ap.add_argument("--integrity-json",required=True)
    ap.add_argument("--integrity-md",required=True)
    args=ap.parse_args()

    token=os.getenv("PRIVATE_REPO_TOKEN")
    if not token:raise RuntimeError("PRIVATE_REPO_TOKEN is not configured")
    repo=os.getenv("PRIVATE_REPO",DEFAULT_REPO);h=hdr(token)

    report=json.loads(Path(args.integrity_json).read_text(encoding="utf-8"))
    if report.get("kind")!=args.kind:
        raise RuntimeError(f"integrity report kind mismatch: report={report.get('kind')!r} argument={args.kind!r}")
    when=parse_time(report.get("generated_at_utc"))
    stamp=when.strftime("%Y%m%dT%H%M%S")+f"{when.microsecond:06d}Z"
    day=f"{when:%Y/%m/%d}"

    latest_path=f"data/inbox/public_collector/integrity/{args.kind}/latest.json"
    latest_success_path=f"data/inbox/public_collector/integrity/{args.kind}/latest_success.json"
    receipt_latest_path=f"data/inbox/public_collector/transfer_receipts/{args.kind}/latest.json"
    previous=decoded_json_content(content_meta(repo,latest_path,h))
    nominal_minutes={"models":180,"svg":60,"skm":60,"wunstorf":300,"etnw":300}[args.kind]
    continuity={
        "nominal_target_interval_minutes":nominal_minutes,
        "previous_attempt_generated_at_utc":None,
        "interval_since_previous_attempt_minutes":None,
        "interval_exceeds_1_5x_nominal":None,
        "estimated_whole_nominal_intervals_without_attempt":None,
        "incoming_attempt_older_than_current_latest":False,
    }
    if previous and previous.get("generated_at_utc"):
        if (previous.get("generated_at_utc")==report.get("generated_at_utc")
                and isinstance(previous.get("invocation_continuity"),dict)):
            continuity=dict(previous["invocation_continuity"])
        else:
            prev_time=parse_time(previous["generated_at_utc"])
            raw_gap=(when-prev_time).total_seconds()/60
            gap=max(0.0,raw_gap)
            continuity.update(
                previous_attempt_generated_at_utc=prev_time.isoformat(),
                interval_since_previous_attempt_minutes=round(raw_gap,2),
                interval_exceeds_1_5x_nominal=raw_gap>nominal_minutes*1.5,
                estimated_whole_nominal_intervals_without_attempt=max(0,int(gap//nominal_minutes)-1),
                incoming_attempt_older_than_current_latest=raw_gap<0,
            )
    report["invocation_continuity"]=continuity
    invocation=workflow_provenance()
    report["public_workflow_invocation"]=invocation
    report["private_transfer_protocol"]={
        "method_version":"private-transfer-readback-v2",
        "publish_gate":"unpublished commit tree + blob exact-byte readback before main ref update",
        "gzip_payload_check":"compressed bytes exact; decompressed SHA-256 must equal audit input SHA-256",
        "latest_pointer_rule":"generated_at_utc is monotonic across concurrent/retried writers",
        "success_pointer_rule":"latest_success is published only with a verified transfer receipt",
        "main_ref_check":"main must equal the verified commit or be a later descendant that preserves immutable evidence and monotonic pointers",
        "main_ref_race_policy":"up to 8 CAS-style retries; each retry reads the parent first, pins every guard/collision check to that SHA, and rebuilds the tree",
    }

    md_text=Path(args.integrity_md).read_text(encoding="utf-8")
    md_text += (
        "\n## Collector invocation continuity\n\n"
        f"- Nominal target interval: {nominal_minutes} min.\n"
        f"- Previous transferred attempt: {continuity['previous_attempt_generated_at_utc']}.\n"
        f"- Interval since previous attempt: {continuity['interval_since_previous_attempt_minutes']} min.\n"
        f"- Incoming attempt older than current latest: {continuity['incoming_attempt_older_than_current_latest']}.\n"
        f"- Interval >1.5× nominal: {continuity['interval_exceeds_1_5x_nominal']}.\n"
        f"- Estimated complete nominal slots without an attempt: {continuity['estimated_whole_nominal_intervals_without_attempt']}.\n"
        f"- Public workflow run: {invocation['run_id']} attempt {invocation['run_attempt']}; "
        f"workflow={invocation['workflow']}; job={invocation['job']}; "
        f"event={invocation['event_name']}; sha={invocation['sha']}.\n"
    )
    md_raw=md_text.encode("utf-8")
    immutable_files=[]

    audit_sha=report.get("input_payload_sha256")
    audit_bytes=report.get("input_payload_bytes")
    input_present=bool(report.get("input_file_present"))
    if input_present and (not args.file or not Path(args.file).exists()):
        raise RuntimeError("integrity report says input payload exists but transfer payload file is missing")
    if args.file and Path(args.file).exists():
        raw=Path(args.file).read_bytes()
        source_sha=hashlib.sha256(raw).hexdigest()
        if not audit_sha or source_sha!=audit_sha:
            raise RuntimeError(f"audited payload SHA mismatch: audit={audit_sha} transfer={source_sha}")
        if audit_bytes is None or int(audit_bytes)!=len(raw):
            raise RuntimeError(f"audited payload byte-count mismatch: audit={audit_bytes} transfer={len(raw)}")
        packed=gzip.compress(raw,compresslevel=9,mtime=0)
        dest=f"data/inbox/public_collector/{args.kind}/{day}/{args.kind}_{stamp}.json.gz"
        report["private_payload"]={
            "destination":dest,"source_sha256":source_sha,
            "source_bytes":len(raw),"compressed_bytes":len(packed)}
        immutable_files.append({"path":dest,"content":packed,"immutable":True,"gzip":True,
                                "source_sha256":source_sha})
    elif audit_sha or audit_bytes:
        raise RuntimeError("integrity report contains payload identity but no transfer payload file was supplied")

    report_raw=(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+"\n").encode()
    immutable_files += [
        {"path":f"data/inbox/public_collector/integrity/{args.kind}/{day}/integrity_{stamp}.json",
         "content":report_raw,"immutable":True},
        {"path":f"reports/collector-health/{args.kind}/{day}/integrity_{stamp}.md",
         "content":md_raw,"immutable":True},
    ]
    attempt_pointer_files=[
        pointer_item(latest_path,report_raw,latest_path,"generated_at_utc",when.isoformat()),
        pointer_item(f"reports/collector-health/{args.kind}/latest.md",md_raw,
                     latest_path,"generated_at_utc",when.isoformat()),
    ]
    receipt_path=f"data/inbox/public_collector/transfer_receipts/{args.kind}/{day}/receipt_{stamp}.json"

    # Fully completed reruns validate immutable evidence and then repair/finalize
    # mutable pointers if a previous invocation stopped after the receipt commit.
    existing_receipt=decoded_json_content(content_meta(repo,receipt_path,h))
    if existing_receipt is not None:
        expected_payload_sha=(report.get("private_payload") or {}).get("source_sha256")
        if (existing_receipt.get("kind")!=args.kind
                or existing_receipt.get("stamp")!=stamp
                or existing_receipt.get("source_generated_at_utc")!=when.isoformat()
                or existing_receipt.get("readback_verified") is not True
                or existing_receipt.get("payload_source_sha256")!=expected_payload_sha
                or existing_receipt.get("audit_input_payload_sha256")!=audit_sha):
            raise RuntimeError(f"existing transfer receipt conflicts with this attempt: {receipt_path}")
        rb={x.get("path"):x for x in (existing_receipt.get("readback") or []) if isinstance(x,dict)}
        for item in immutable_files:
            existing_sha=same_existing(repo,item["path"],item["content"],h,item.get("gzip",False))
            if not existing_sha:
                raise RuntimeError(f"receipt exists but immutable transfer path is missing: {item['path']}")
            proof=rb.get(item["path"])
            if not proof or proof.get("exact_bytes_match") is not True:
                raise RuntimeError(f"receipt lacks exact-byte proof for immutable path: {item['path']}")
            if proof.get("sha256")!=hashlib.sha256(item["content"]).hexdigest():
                raise RuntimeError(f"receipt SHA-256 does not match immutable bytes: {item['path']}")
            if item.get("gzip") and item.get("source_sha256") and proof.get("decompressed_sha256")!=item["source_sha256"]:
                raise RuntimeError(f"receipt decompressed SHA-256 mismatch for immutable payload: {item['path']}")

        receipt_raw=(json.dumps(existing_receipt,indent=2,ensure_ascii=False,allow_nan=False)+"\n").encode()
        finalize=[
            pointer_item(receipt_latest_path,receipt_raw,receipt_latest_path,
                         "source_generated_at_utc",when.isoformat())
        ]
        if report.get("bundle_ready_for_private_revalidation"):
            finalize += [
                pointer_item(latest_success_path,report_raw,latest_success_path,
                             "generated_at_utc",when.isoformat()),
                pointer_item(f"reports/collector-health/{args.kind}/latest_success.md",md_raw,
                             latest_success_path,"generated_at_utc",when.isoformat()),
            ]
        repaired=atomic_commit(repo,finalize,f"collector: finalize verified {args.kind} transfer {stamp}",h)
        print(json.dumps({
            "idempotent":True,"kind":args.kind,"stamp":stamp,
            "transfer_receipt_path":receipt_path,
            "verified_data_commit_sha":existing_receipt.get("verified_data_commit_sha"),
            "readback_verified":True,
            "finalization":repaired,
            "reason":"existing_receipt_and_immutable_bytes_reverified",
        },indent=2))
        return

    data_files=immutable_files+attempt_pointer_files
    result=atomic_commit(repo,data_files,f"collector: ingest {args.kind} attempt {stamp}",h)
    result.update({"kind":args.kind,"stamp":stamp})

    if result.get("commit_sha") and result.get("readback_verified"):
        verified_at=datetime.now(timezone.utc)
        receipt={
            "schema_version":2,
            "method_version":"private-transfer-readback-v2",
            "kind":args.kind,
            "stamp":stamp,
            "source_generated_at_utc":when.isoformat(),
            "verified_at_utc":verified_at.isoformat(),
            "public_workflow_invocation":invocation,
            "verified_data_commit_sha":result["commit_sha"],
            "readback_verified":True,
            "readback_protocol":result.get("readback_protocol"),
            "readback":result.get("readback") or [],
            "payload_source_sha256":(report.get("private_payload") or {}).get("source_sha256"),
            "audit_input_payload_sha256":audit_sha,
            "payload_destination":(report.get("private_payload") or {}).get("destination"),
            "publication_semantics":"The data commit was read back before publication; latest_success is finalized only in this receipt-bearing child commit.",
        }
        receipt_raw=(json.dumps(receipt,indent=2,ensure_ascii=False,allow_nan=False)+"\n").encode()
        receipt_files=[
            {"path":receipt_path,"content":receipt_raw,"immutable":True},
            pointer_item(receipt_latest_path,receipt_raw,receipt_latest_path,
                         "source_generated_at_utc",when.isoformat()),
        ]
        if report.get("bundle_ready_for_private_revalidation"):
            receipt_files += [
                pointer_item(latest_success_path,report_raw,latest_success_path,
                             "generated_at_utc",when.isoformat()),
                pointer_item(f"reports/collector-health/{args.kind}/latest_success.md",md_raw,
                             latest_success_path,"generated_at_utc",when.isoformat()),
            ]
        rr=atomic_commit(repo,receipt_files,f"collector: record verified {args.kind} transfer {stamp}",h)
        result["transfer_receipt_path"]=receipt_path
        result["transfer_receipt_commit_sha"]=rr.get("commit_sha")
        result["transfer_receipt_commit_readback_verified"]=rr.get("readback_verified",False)
        result["success_pointer_finalized"]=bool(report.get("bundle_ready_for_private_revalidation"))
        result["receipt_finalization"]=rr
    print(json.dumps(result,indent=2))

if __name__=="__main__":
    main()
