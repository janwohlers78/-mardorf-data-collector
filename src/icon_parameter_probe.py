#!/usr/bin/env python3
"""Bounded live probe for Phase-2 DWD ICON weather parameters.

This is acquisition validation, not an analysis change. It downloads one
selected regular-lat-lon file per parameter/model/lead, extracts only GRIB
messages with the exact requested run/valid time, and records provider-native
values plus metadata without normalization.
"""
from __future__ import annotations

import argparse
import bz2
import hashlib
import json
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fetch_dwd_additional_models as dwd
import fetch_model_data as base
from grib_identity import assert_grib_valid_time

PARAMETERS = (
    "t_2m",
    "td_2m",
    "relhum_2m",
    "pmsl",
    "ps",
    "tot_prec",
    "clct",
    "aswdir_s",
    "aswdifd_s",
    "cape_ml",
    "cin_ml",
)
CANONICAL = {
    "t_2m": "air_temperature_2m",
    "td_2m": "dewpoint_2m",
    "relhum_2m": "relative_humidity_2m",
    "pmsl": "mean_sea_level_pressure",
    "ps": "surface_pressure",
    "tot_prec": "precipitation_amount",
    "clct": "total_cloud_fraction",
    "aswdir_s": "surface_downward_shortwave_direct",
    "aswdifd_s": "surface_downward_shortwave_diffuse",
    "cape_ml": "cape_mixed_layer",
    "cin_ml": "cin_mixed_layer",
}
META_KEYS = (
    "shortName", "paramId", "units", "typeOfLevel", "level", "stepType",
    "startStep", "endStep", "stepUnits", "stepRange",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def _lines(path, key):
    p = subprocess.run(
        ["grib_get", "-f", "-p", key, str(path)],
        capture_output=True, text=True, check=True,
    )
    return [x.strip() for x in p.stdout.splitlines() if x.strip()]


def message_metadata(path):
    columns = {key: _lines(path, key) for key in META_KEYS}
    counts = {len(v) for v in columns.values()}
    if len(counts) != 1 or not counts or next(iter(counts)) == 0:
        raise RuntimeError(f"inconsistent GRIB metadata row counts: { {k:len(v) for k,v in columns.items()} }")
    n = next(iter(counts))
    out = []
    for i in range(n):
        row = {key: columns[key][i] for key in META_KEYS}
        for key in ("paramId", "level", "startStep", "endStep"):
            try:
                row[key] = int(row[key])
            except (TypeError, ValueError):
                pass
        out.append(row)
    return out


def select_exact_message(source, target, run, valid):
    run = run.astimezone(timezone.utc)
    valid = valid.astimezone(timezone.utc)
    where = ",".join((
        f"dataDate={run:%Y%m%d}",
        f"dataTime={run.hour * 100}",
        f"validityDate={valid:%Y%m%d}",
        f"validityTime={valid.hour * 100 + valid.minute}",
    ))
    subprocess.run(["grib_copy", "-w", where, str(source), str(target)],
                   capture_output=True, text=True, check=True)
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError("no GRIB message matches exact run/valid time")
    assert_grib_valid_time(target, run, valid, "Phase-2 ICON parameter probe")


def regular_file(model, cycle, lead, param):
    return dwd.find_dwd_regular_file(model, cycle, lead, param)


def probe_field(model, cycle, lead, param, directory):
    run = datetime.strptime(cycle, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    valid = run + timedelta(hours=lead)
    checked = now()
    result = {
        "parameter_native": param,
        "parameter_id_candidate": CANONICAL[param],
        "required_for_core_wind": False,
        "run_time_utc": run.isoformat(),
        "valid_time_utc": valid.isoformat(),
        "forecast_lead_hours": lead,
        "checked_at_utc": checked,
    }
    started = time.monotonic()
    try:
        url = regular_file(model, cycle, lead, param)
        result["source_url"] = url
        response = dwd.S.get(url, timeout=120)
        response.raise_for_status()
        result["response_bytes"] = len(response.content)
        result["response_sha256"] = hashlib.sha256(response.content).hexdigest()
        grib = directory / f"{model}_{param}_{lead:03d}.grib2"
        selected = directory / f"{model}_{param}_{lead:03d}_selected.grib2"
        grib.write_bytes(bz2.decompress(response.content))
        select_exact_message(grib, selected, run, valid)
        result["selected_grib_sha256"] = hashlib.sha256(selected.read_bytes()).hexdigest()
        metadata = message_metadata(selected)
        nearest = base.grib_nearest(selected)
        if len(metadata) != len(nearest):
            raise RuntimeError(
                f"metadata/nearest row mismatch metadata={len(metadata)} nearest={len(nearest)}")
        values = []
        for meta, point_value in zip(metadata, nearest):
            values.append({
                **meta,
                "value": point_value["value"],
                "latitude": point_value["lat"],
                "longitude": point_value["lon"],
                "availability_status": "received",
                "source_sha256": result["response_sha256"],
            })
        result["forecast_coordinate_or_grid_point"] = {
            "latitude": nearest[0]["lat"],
            "longitude": nearest[0]["lon"],
            "selection": "ecCodes_nearest_grid_point",
        }
        result["values"] = values
        result["status"] = "received"
    except Exception as exc:
        result["status"] = "missing_or_invalid"
        result["error_type"] = type(exc).__name__
        result["error_message"] = str(exc)[:1000]
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def probe_model(model_name, dwd_model, lead):
    cycle = dwd.discover_cycle(dwd_model, lead)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        fields = [probe_field(dwd_model, cycle, lead, param, root) for param in PARAMETERS]
    received = [x["parameter_native"] for x in fields if x["status"] == "received"]
    missing = [x["parameter_native"] for x in fields if x["status"] != "received"]
    return {
        "model": model_name,
        "provider_model": dwd_model,
        "cycle": cycle,
        "lead_hours": lead,
        "received_count": len(received),
        "missing_count": len(missing),
        "received_parameters": received,
        "missing_parameters": missing,
        "fields": fields,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lead", type=int, default=12)
    ap.add_argument("--output", default="work/icon_parameter_probe.json")
    args = ap.parse_args()
    if args.lead < 0:
        raise SystemExit("--lead must be non-negative")
    started = now()
    result = {
        "schema_version": 1,
        "method_version": "phase2-icon-parameter-live-probe-v1",
        "started_at_utc": started,
        "normalization_performed": False,
        "analysis_changed": False,
        "models": [
            probe_model("ICON-D2", "icon-d2", args.lead),
            probe_model("ICON-EU", "icon-eu", args.lead),
        ],
    }
    result["completed_at_utc"] = now()
    result["total_response_bytes"] = sum(
        f.get("response_bytes", 0) for m in result["models"] for f in m["fields"])
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "method_version": result["method_version"],
        "lead_hours": args.lead,
        "total_response_bytes": result["total_response_bytes"],
        "models": [{
            "model": m["model"],
            "cycle": m["cycle"],
            "received_count": m["received_count"],
            "missing_count": m["missing_count"],
            "missing_parameters": m["missing_parameters"],
        } for m in result["models"]],
        "output": str(out),
    }, indent=2))
    if any(m["missing_count"] for m in result["models"]):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
