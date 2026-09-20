#!/usr/bin/env python3
"""Publish one atomic secondary-observation batch receipt.

Wunstorf and ETNW remain independently acquired, audited and transferred.  This
finalizer runs only after both child transfers and integrity gates succeeded.  It
verifies that the private repository's current child receipts exactly match this
collector invocation, then publishes one readback-verified secondary receipt.
That receipt is the sole production trigger for private secondary promotion.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from push_private import (
    DEFAULT_REPO,
    atomic_commit,
    content_meta,
    decoded_json_content,
    hdr,
    parse_time,
    pointer_item,
)

CHILDREN=("wunstorf","etnw")
METHOD_VERSION="secondary-batch-receipt-v1"


def compact_stamp(value):
    when=parse_time(value)
    return when.strftime("%Y%m%dT%H%M%S")+f"{when.microsecond:06d}Z"


def immutable_receipt_path(kind,generated_at):
    when=parse_time(generated_at)
    return (
        f"data/inbox/public_collector/transfer_receipts/{kind}/"
        f"{when:%Y/%m/%d}/receipt_{compact_stamp(generated_at)}.json"
    )


def validate_child(kind,report,receipt):
    if report.get("kind")!=kind:
        raise RuntimeError(f"{kind} integrity report kind mismatch")
    if report.get("bundle_ready_for_private_revalidation") is not True:
        raise RuntimeError(f"{kind} bundle is not ready for private revalidation")
    if int(report.get("error_count",0))!=0:
        raise RuntimeError(f"{kind} integrity report contains errors")

    generated=parse_time(report.get("generated_at_utc")).isoformat()
    expected_stamp=compact_stamp(generated)
    if not isinstance(receipt,dict):
        raise RuntimeError(f"{kind} latest transfer receipt is missing")
    checks={
        "method_version":"private-transfer-readback-v2",
        "kind":kind,
        "stamp":expected_stamp,
        "source_generated_at_utc":generated,
        "readback_verified":True,
    }
    for field,expected in checks.items():
        if receipt.get(field)!=expected:
            raise RuntimeError(
                f"{kind} child receipt mismatch for {field}: "
                f"expected={expected!r} got={receipt.get(field)!r}"
            )

    audit_sha=report.get("input_payload_sha256")
    if not audit_sha or receipt.get("payload_source_sha256")!=audit_sha:
        raise RuntimeError(f"{kind} child receipt payload SHA does not match audited input")
    if receipt.get("audit_input_payload_sha256")!=audit_sha:
        raise RuntimeError(f"{kind} child receipt audit SHA does not match audited input")
    if not receipt.get("verified_data_commit_sha"):
        raise RuntimeError(f"{kind} child receipt lacks verified data commit SHA")

    return {
        "kind":kind,
        "stamp":receipt["stamp"],
        "source_generated_at_utc":generated,
        "verified_at_utc":receipt.get("verified_at_utc"),
        "verified_data_commit_sha":receipt["verified_data_commit_sha"],
        "payload_source_sha256":receipt["payload_source_sha256"],
        "audit_input_payload_sha256":receipt["audit_input_payload_sha256"],
        "payload_destination":receipt.get("payload_destination"),
        "receipt_path":immutable_receipt_path(kind,generated),
        "readback_verified":True,
    }


def build_batch(reports,receipts,now=None,run_id=None,run_attempt=None):
    now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    children={
        kind:validate_child(kind,reports[kind],receipts[kind])
        for kind in CHILDREN
    }
    source_generated=max(
        parse_time(children[kind]["source_generated_at_utc"]) for kind in CHILDREN
    ).isoformat()
    return {
        "schema_version":1,
        "method_version":METHOD_VERSION,
        "kind":"secondary",
        "source_generated_at_utc":source_generated,
        "published_at_utc":now.isoformat(),
        "complete":True,
        "transaction_semantics":
            "published only after both independently audited child transfers "
            "are exact-byte readback verified and match this collector invocation",
        "public_workflow_run_id":str(run_id) if run_id else None,
        "public_workflow_run_attempt":str(run_attempt) if run_attempt else None,
        "children":children,
    }


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--wunstorf-integrity",required=True)
    p.add_argument("--etnw-integrity",required=True)
    args=p.parse_args()

    token=os.getenv("PRIVATE_REPO_TOKEN")
    if not token:
        raise RuntimeError("PRIVATE_REPO_TOKEN is not configured")
    repo=os.getenv("PRIVATE_REPO",DEFAULT_REPO)
    h=hdr(token)

    reports={
        "wunstorf":json.loads(Path(args.wunstorf_integrity).read_text(encoding="utf-8")),
        "etnw":json.loads(Path(args.etnw_integrity).read_text(encoding="utf-8")),
    }
    receipts={}
    for kind in CHILDREN:
        path=f"data/inbox/public_collector/transfer_receipts/{kind}/latest.json"
        receipts[kind]=decoded_json_content(content_meta(repo,path,h))

    batch=build_batch(
        reports,
        receipts,
        run_id=os.getenv("GITHUB_RUN_ID"),
        run_attempt=os.getenv("GITHUB_RUN_ATTEMPT"),
    )
    when=parse_time(batch["source_generated_at_utc"])
    stamp=compact_stamp(when.isoformat())
    day=f"{when:%Y/%m/%d}"
    raw=(json.dumps(batch,indent=2,ensure_ascii=False,allow_nan=False)+"\n").encode("utf-8")
    immutable_path=(
        f"data/inbox/public_collector/transfer_receipts/secondary/{day}/"
        f"receipt_{stamp}.json"
    )
    latest_path="data/inbox/public_collector/transfer_receipts/secondary/latest.json"
    files=[
        {"path":immutable_path,"content":raw,"immutable":True},
        pointer_item(
            latest_path,raw,latest_path,"source_generated_at_utc",
            batch["source_generated_at_utc"],
        ),
    ]
    result=atomic_commit(
        repo,files,f"collector: finalize verified secondary batch {stamp}",h
    )
    result.update({
        "kind":"secondary",
        "method_version":METHOD_VERSION,
        "batch_receipt_path":immutable_path,
        "source_generated_at_utc":batch["source_generated_at_utc"],
    })
    print(json.dumps(result,indent=2,ensure_ascii=False))


if __name__=="__main__":
    main()
