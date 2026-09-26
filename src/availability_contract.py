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


def _declaration(parameter, status, observed_at, *, product=None, evidence_type):
    out = {
        "semantic_id": f"availability:{product + ':' if product else ''}{parameter}",
        "parameter_native": str(parameter),
        "namespace": "availability",
        "availability_status": status,
        "availability_observed_at_utc": observed_at,
        "availability_evidence_type": evidence_type,
    }
    if product:
        out["field_provider_product"] = str(product)
    if status == "received":
        out["field_available_at_utc"] = observed_at
    return out


def availability_declarations(row, observed_at):
    """Normalize legacy boolean missingness evidence into explicit state rows."""
    out = []
    seen = set()

    def add(parameter, status, *, product=None, evidence_type):
        key = (str(parameter), str(product or ""), status, evidence_type)
        if key in seen:
            return
        seen.add(key)
        out.append(_declaration(
            parameter, status, observed_at,
            product=product, evidence_type=evidence_type,
        ))

    product = row.get("provider_product")
    for parameter, present in (row.get("field_availability") or {}).items():
        if not isinstance(present, bool):
            raise ValueError(f"field_availability must be boolean: {parameter}={present!r}")
        add(
            parameter,
            "received" if present else "unsupported_by_provider_or_product",
            product=product,
            evidence_type="legacy_field_availability_boolean_v1",
        )

    for field_product, fields in (row.get("weather_context_availability") or {}).items():
        if not isinstance(fields, dict):
            raise ValueError(f"weather_context_availability must be an object for {field_product!r}")
        for parameter, present in fields.items():
            if not isinstance(present, bool):
                raise ValueError(
                    f"weather_context_availability must be boolean: {field_product}:{parameter}={present!r}"
                )
            add(
                parameter,
                "received" if present else "unsupported_by_provider_or_product",
                product=field_product,
                evidence_type="product_weather_context_boolean_v1",
            )
    return out


def stamp_rows(rows, observed_at=None, replace_row_time=False):
    observed_at = observed_at or now()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if replace_row_time or not row.get("retrieved_at_utc"):
            row["retrieved_at_utc"] = observed_at
        row_time = row["retrieved_at_utc"]
        values = row.get("values")
        if isinstance(values, dict):
            for parameter, raw in list(values.items()):
                if isinstance(raw, list):
                    values[parameter] = [
                        normalize_field_item(item, row_time) if isinstance(item, dict) else item
                        for item in raw
                    ]
                elif isinstance(raw, dict):
                    values[parameter] = normalize_field_item(raw, row_time)
        row["field_availability_states"] = availability_declarations(row, row_time)
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
                declarations = row.get("field_availability_states")
                if declarations is None:
                    problems.append(f"{scope}:{model}[{index}] missing field_availability_states")
                elif not isinstance(declarations, list):
                    problems.append(f"{scope}:{model}[{index}] field_availability_states is not a list")
                else:
                    for declaration_index, declaration in enumerate(declarations):
                        if not isinstance(declaration, dict):
                            problems.append(
                                f"{scope}:{model}[{index}] availability declaration[{declaration_index}] is not an object"
                            )
                            continue
                        status = declaration.get("availability_status")
                        if status not in STATES:
                            problems.append(
                                f"{scope}:{model}[{index}] availability declaration[{declaration_index}] invalid status {status!r}"
                            )
                        if not declaration.get("parameter_native") or declaration.get("namespace") != "availability":
                            problems.append(
                                f"{scope}:{model}[{index}] availability declaration[{declaration_index}] identity incomplete"
                            )
                        if not declaration.get("availability_observed_at_utc"):
                            problems.append(
                                f"{scope}:{model}[{index}] availability declaration[{declaration_index}] missing observed time"
                            )
                        if status == "received" and not declaration.get("field_available_at_utc"):
                            problems.append(
                                f"{scope}:{model}[{index}] received declaration[{declaration_index}] missing field_available_at_utc"
                            )
                        if "value" in declaration:
                            problems.append(
                                f"{scope}:{model}[{index}] availability declaration[{declaration_index}] must not carry a value"
                            )
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
