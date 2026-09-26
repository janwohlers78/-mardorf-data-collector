#!/usr/bin/env python3
"""Normalize model-row and native-field availability timestamps/states.

Phase 2D-3 contract:
- every newly emitted model row has row-level retrieved_at_utc;
- every native field carries an explicit availability_status;
- received native fields carry field_available_at_utc;
- every availability observation carries availability_observed_at_utc;
- missing states use a closed enum and are never represented by synthetic zeroes.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

STATES = {
    "received",
    "unsupported_by_provider_or_product",
    "not_requested_by_policy",
    "not_yet_published",
    "fetch_error",
}

LEGACY_STATUS_MAP = {
    "received": "received",
    "not_offered": "not_yet_published",
    "missing": "fetch_error",
    "fetch_error": "fetch_error",
    "unsupported": "unsupported_by_provider_or_product",
    "not_requested": "not_requested_by_policy",
    "pending": "not_yet_published",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def _status(item):
    raw = item.get("availability_status")
    if raw is not None:
        normalized = LEGACY_STATUS_MAP.get(str(raw), str(raw))
        if normalized not in STATES:
            raise ValueError(f"unknown field availability status: {raw!r}")
        return normalized
    if item.get("error_type") or item.get("error_message"):
        return "fetch_error"
    return "received"


def normalize_field_item(item, observed_at):
    if not isinstance(item, dict):
        return item
    status = _status(item)
    if status != "received" and item.get("value") is not None:
        raise ValueError(
            f"non-received field cannot carry a value: status={status} value={item.get('value')!r}"
        )
    item["availability_status"] = status
    item.setdefault("availability_observed_at_utc", observed_at)
    if status == "received":
        item.setdefault("field_available_at_utc", observed_at)
    else:
        item.pop("field_available_at_utc", None)
    return item


def stamp_rows(rows, observed_at=None, replace_row_time=False):
    observed_at = observed_at or now()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if replace_row_time or not row.get("retrieved_at_utc"):
            row["retrieved_at_utc"] = observed_at
        row_time = row["retrieved_at_utc"]
        values = row.get("values")
        if not isinstance(values, dict):
            continue
        for parameter, raw in list(values.items()):
            if isinstance(raw, list):
                values[parameter] = [
                    normalize_field_item(item, row_time) if isinstance(item, dict) else item
                    for item in raw
                ]
            elif isinstance(raw, dict):
                values[parameter] = normalize_field_item(raw, row_time)
    return rows


def normalize_snapshot(snapshot, observed_at=None):
    observed_at = observed_at or now()
    for rows in (snapshot.get("models") or {}).values():
        stamp_rows(rows, observed_at=observed_at, replace_row_time=False)
    archive = snapshot.get("full_horizon_archive") or {}
    for source in (archive.get("sources") or {}).values():
        stamp_rows(source.get("records") or [], observed_at=observed_at, replace_row_time=False)
    snapshot["availability_contract"] = {
        "schema_version": 1,
        "method_version": "model-field-availability-v1",
        "normalized_at_utc": observed_at,
        "states": sorted(STATES),
        "missing_value_policy": "explicit_status_never_zero_fill",
        "row_time_semantics": "retrieved_at_utc_is_the_public_acquisition_or_revision-observation_time",
        "field_time_semantics": "field_available_at_utc_is_present_only_for_received_fields",
    }
    return snapshot


def validate_snapshot(snapshot):
    problems = []
    for scope, models in (
        ("models", snapshot.get("models") or {}),
        ("full_horizon", {
            model: source.get("records") or []
            for model, source in ((snapshot.get("full_horizon_archive") or {}).get("sources") or {}).items()
        }),
    ):
        for model, rows in models.items():
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                if not row.get("retrieved_at_utc"):
                    problems.append(f"{scope}:{model}[{index}] missing retrieved_at_utc")
                for parameter, raw in (row.get("values") or {}).items():
                    items = raw if isinstance(raw, list) else [raw]
                    for item_index, item in enumerate(items):
                        if not isinstance(item, dict):
                            continue
                        status = item.get("availability_status")
                        if status not in STATES:
                            problems.append(
                                f"{scope}:{model}[{index}] {parameter}[{item_index}] invalid status {status!r}"
                            )
                            continue
                        if not item.get("availability_observed_at_utc"):
                            problems.append(
                                f"{scope}:{model}[{index}] {parameter}[{item_index}] missing availability_observed_at_utc"
                            )
                        if status == "received" and not item.get("field_available_at_utc"):
                            problems.append(
                                f"{scope}:{model}[{index}] {parameter}[{item_index}] received without field_available_at_utc"
                            )
                        if status != "received" and item.get("value") is not None:
                            problems.append(
                                f"{scope}:{model}[{index}] {parameter}[{item_index}] non-received carries value"
                            )
    if problems:
        raise ValueError("availability contract failed: " + "; ".join(problems[:20]))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    args = ap.parse_args()
    path = Path(args.input)
    payload = json.loads(path.read_text(encoding="utf-8"))
    normalize_snapshot(payload)
    validate_snapshot(payload)
    path.write_text(json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "method_version": payload["availability_contract"]["method_version"],
        "states": payload["availability_contract"]["states"],
        "status": "ok",
    }))


if __name__ == "__main__":
    main()
