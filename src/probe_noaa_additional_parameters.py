#!/usr/bin/env python3
"""Bounded NOAA GFS/GEFS additional-parameter live probe.

This diagnostic is deliberately isolated from production collector state:
it writes only explicitly requested local output files and never touches
model_snapshot.json, transfer receipts, latest_success, or private storage.
"""
import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LAT = 52.4942
LON = 9.3418
UA = "mardorf-data-collector/noaa-phase2c-probe-v1 (+github-actions)"
VARS = ("TMP", "DPT", "RH", "PRMSL", "PRES", "TCDC", "CAPE", "CIN", "DSWRF")
COMMON_LEVELS = (
    "2_m_above_ground",
    "mean_sea_level",
    "surface",
    "entire_atmosphere",
    "180-0_mb_above_ground",
)
GFS_EXTRA_LEVELS = ("255-0_mb_above_ground", "90-0_mb_above_ground")
META_KEYS = (
    "shortName", "name", "paramId", "typeOfLevel", "level", "stepType",
    "stepRange", "units", "dataDate", "dataTime", "validityDate",
    "validityTime", "totalLength", "gridType", "Ni", "Nj",
)
PLAN = (
    ("GFS", "gfs_0p25", 24),
    ("GFS", "gfs_0p25", 120),
    ("GFS", "gfs_0p25", 384),
    ("GEFS-control", "gefs_0p25s", 24),
    ("GEFS-control", "gefs_0p25s", 240),
    ("GEFS-control", "gefs_0p50a", 384),
    ("GEFS-control", "gefs_0p50b", 384),
)


def utcnow():
    return datetime.now(timezone.utc)


def cycle_candidates(now=None):
    now = now or utcnow()
    out = []
    for dd in range(3):
        day = (now - timedelta(days=dd)).date()
        for hh in (18, 12, 6, 0):
            run = datetime(day.year, day.month, day.day, hh, tzinfo=timezone.utc)
            if run <= now:
                out.append(run)
    return sorted(set(out), reverse=True)


def http_get(url, timeout=45):
    req = Request(url, headers={"User-Agent": UA})
    started = time.monotonic()
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = int(resp.status)
            headers = dict(resp.headers.items())
    except HTTPError as exc:
        raw = exc.read()
        status = int(exc.code)
        headers = dict(exc.headers.items()) if exc.headers else {}
    except URLError as exc:
        return {
            "status": None,
            "bytes": 0,
            "duration_seconds": round(time.monotonic() - started, 3),
            "headers": {},
            "raw": b"",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "status": status,
        "bytes": len(raw),
        "duration_seconds": round(time.monotonic() - started, 3),
        "headers": headers,
        "raw": raw,
        "error": None,
    }


def base_query(file_name, directory, pad, variables, levels):
    q = {
        "file": file_name,
        "subregion": "",
        "leftlon": f"{LON-pad:.4f}",
        "rightlon": f"{LON+pad:.4f}",
        "toplat": f"{LAT+pad:.4f}",
        "bottomlat": f"{LAT-pad:.4f}",
        "dir": directory,
    }
    for var in variables:
        q[f"var_{var}"] = "on"
    for level in levels:
        q[f"lev_{level}"] = "on"
    return q


def product_url(product, run, lead, variables=VARS, levels=None):
    ymd = run.strftime("%Y%m%d")
    hh = run.strftime("%H")
    if product == "gfs_0p25":
        endpoint = "filter_gfs_0p25.pl"
        file_name = f"gfs.t{hh}z.pgrb2.0p25.f{lead:03d}"
        directory = f"/gfs.{ymd}/{hh}/atmos"
        pad = 0.30
        product_levels = COMMON_LEVELS + GFS_EXTRA_LEVELS
    elif product == "gefs_0p25s":
        endpoint = "filter_gefs_atmos_0p25s.pl"
        file_name = f"gec00.t{hh}z.pgrb2s.0p25.f{lead:03d}"
        directory = f"/gefs.{ymd}/{hh}/atmos/pgrb2sp25"
        pad = 0.30
        product_levels = COMMON_LEVELS
    elif product == "gefs_0p50a":
        endpoint = "filter_gefs_atmos_0p50a.pl"
        file_name = f"gec00.t{hh}z.pgrb2a.0p50.f{lead:03d}"
        directory = f"/gefs.{ymd}/{hh}/atmos/pgrb2ap5"
        pad = 0.75
        product_levels = COMMON_LEVELS
    elif product == "gefs_0p50b":
        endpoint = "filter_gefs_atmos_0p50b.pl"
        file_name = f"gec00.t{hh}z.pgrb2b.0p50.f{lead:03d}"
        directory = f"/gefs.{ymd}/{hh}/atmos/pgrb2bp5"
        pad = 0.75
        product_levels = COMMON_LEVELS
    else:
        raise ValueError(product)
    q = base_query(file_name, directory, pad, variables, levels or product_levels)
    return "https://nomads.ncep.noaa.gov/cgi-bin/" + endpoint + "?" + urlencode(q)


def discovery_url(model, run):
    if model == "GFS":
        lead = 384
        return lead, product_url(
            "gfs_0p25", run, lead, variables=("UGRD", "VGRD"),
            levels=("10_m_above_ground",),
        )
    if model == "GEFS-control":
        lead = 840 if run.hour == 0 else 384
        return lead, product_url(
            "gefs_0p50a", run, lead, variables=("UGRD", "VGRD"),
            levels=("10_m_above_ground",),
        )
    raise ValueError(model)


def discover_mature_cycle(model):
    attempts = []
    for run in cycle_candidates():
        lead, url = discovery_url(model, run)
        got = http_get(url, timeout=35)
        published = got["status"] == 200 and got["raw"][:4] == b"GRIB"
        attempts.append({
            "run_time_utc": run.isoformat(),
            "terminal_probe_lead_hours": lead,
            "http_status": got["status"],
            "response_bytes": got["bytes"],
            "duration_seconds": got["duration_seconds"],
            "published": published,
            "error": got["error"],
        })
        if published:
            return run, attempts
    raise RuntimeError(f"No mature {model} cycle discovered: {attempts}")


def grib_metadata(path):
    cmd = ["grib_ls", "-j", "-p", ",".join(META_KEYS), str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    parsed = json.loads(proc.stdout)
    if isinstance(parsed, dict):
        rows = parsed.get("messages", [])
    elif isinstance(parsed, list):
        rows = parsed
    else:
        rows = []
    if not isinstance(rows, list):
        raise RuntimeError("Unexpected grib_ls JSON output")
    return rows


def selected_point(path):
    proc = subprocess.run(
        ["grib_ls", "-l", f"{LAT},{LON},1", "-p", "shortName,stepRange", str(path)],
        capture_output=True, text=True, check=True,
    )
    m = re.search(
        r"Grid Point chosen .*?latitude=([+-]?\d+(?:\.\d+)?) longitude=([+-]?\d+(?:\.\d+)?)",
        proc.stdout,
    )
    if not m:
        return None
    return {"latitude": float(m.group(1)), "longitude": float(m.group(2)),
            "selection": "ecCodes_nearest_grid_point"}


def num(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return float(str(value))
    except Exception:
        return None


def classify(row):
    short = str(row.get("shortName", "")).lower()
    name = str(row.get("name", "")).lower()
    tol = str(row.get("typeOfLevel", ""))
    level = num(row.get("level"))
    if ("dew point" in name or short in {"2d", "dpt"}) and tol == "heightAboveGround" and level == 2:
        return "dewpoint_2m"
    if ("relative humidity" in name or short in {"2r"}) and tol == "heightAboveGround" and level == 2:
        return "relative_humidity_2m"
    if ("temperature" in name or short == "2t") and "dew" not in name and tol == "heightAboveGround" and level == 2:
        return "temperature_2m"
    if short in {"prmsl", "msl"} or "mean sea level pressure" in name or "pressure reduced to msl" in name:
        return "mean_sea_level_pressure"
    if (short in {"sp"} or name == "pressure") and tol == "surface":
        return "surface_pressure"
    if short in {"tcc"} or "total cloud cover" in name:
        return "total_cloud_cover"
    if short == "cape" or "convective available potential energy" in name:
        return "cape"
    if short == "cin" or "convective inhibition" in name:
        return "cin"
    if short in {"dswrf", "sdswrf"} or "downward short-wave radiation flux" in name or "downward shortwave radiation flux" in name:
        return "downward_shortwave_flux"
    return "unclassified_candidate"


def run_probe():
    generated = utcnow()
    gfs_run, gfs_attempts = discover_mature_cycle("GFS")
    gefs_run, gefs_attempts = discover_mature_cycle("GEFS-control")
    selected = {"GFS": gfs_run, "GEFS-control": gefs_run}
    results = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for model, product, lead in PLAN:
            run = selected[model]
            # A non-00 GEFS cycle terminates at 384 h; an 00 cycle extends to 840 h.
            if model == "GEFS-control" and lead > (840 if run.hour == 0 else 384):
                results.append({
                    "model": model, "product": product, "lead_hours": lead,
                    "status": "not_expected_for_cycle",
                    "run_time_utc": run.isoformat(),
                })
                continue
            url = product_url(product, run, lead)
            got = http_get(url, timeout=60)
            item = {
                "model": model,
                "product": product,
                "lead_hours": lead,
                "run_time_utc": run.isoformat(),
                "valid_time_utc": (run + timedelta(hours=lead)).isoformat(),
                "request_url": url,
                "http_status": got["status"],
                "response_bytes": got["bytes"],
                "content_length_header": got["headers"].get("Content-Length"),
                "duration_seconds": got["duration_seconds"],
                "sha256": hashlib.sha256(got["raw"]).hexdigest() if got["raw"] else None,
                "grib_magic": got["raw"][:4] == b"GRIB",
                "error": got["error"],
            }
            if got["status"] == 200 and got["raw"][:4] == b"GRIB":
                path = td / f"{model.replace('-','_')}_{product}_{lead}.grib2"
                path.write_bytes(got["raw"])
                rows = grib_metadata(path)
                for row in rows:
                    row["canonical_candidate"] = classify(row)
                item["message_count"] = len(rows)
                item["native_messages"] = rows
                item["selected_point"] = selected_point(path)
                canonical = {}
                for row in rows:
                    key = row["canonical_candidate"]
                    b = num(row.get("totalLength")) or 0
                    bucket = canonical.setdefault(key, {"messages": 0, "grib_message_bytes": 0, "native_signatures": []})
                    bucket["messages"] += 1
                    bucket["grib_message_bytes"] += int(round(b))
                    bucket["native_signatures"].append({
                        k: row.get(k) for k in (
                            "shortName", "name", "paramId", "typeOfLevel", "level",
                            "stepType", "stepRange", "units"
                        )
                    })
                item["availability"] = canonical
                item["sum_grib_message_bytes"] = sum(
                    x["grib_message_bytes"] for x in canonical.values()
                )
                item["status"] = "published"
            else:
                item["message_count"] = 0
                item["native_messages"] = []
                item["availability"] = {}
                item["sum_grib_message_bytes"] = 0
                item["status"] = "request_failed_or_no_matching_fields"
            results.append(item)
    parameter_bytes = sum(x.get("response_bytes", 0) or 0 for x in results)
    parameter_seconds = sum(x.get("duration_seconds", 0) or 0 for x in results)
    availability = {}
    for item in results:
        for canonical, meta in item.get("availability", {}).items():
            if canonical == "unclassified_candidate":
                continue
            key = f"{item['model']}|{item['product']}|{item['lead_hours']}|{canonical}"
            availability[key] = meta
    return {
        "schema_version": 1,
        "method_version": "noaa-additional-parameter-bounded-probe-v1",
        "generated_at_utc": generated.isoformat(),
        "spot": {"latitude": LAT, "longitude": LON},
        "candidate_variables": list(VARS),
        "cycle_selection": {
            "GFS": {
                "selected_run_time_utc": gfs_run.isoformat(),
                "required_terminal_lead_hours": 384,
                "attempts": gfs_attempts,
            },
            "GEFS-control": {
                "selected_run_time_utc": gefs_run.isoformat(),
                "required_terminal_lead_hours": 840 if gefs_run.hour == 0 else 384,
                "attempts": gefs_attempts,
            },
        },
        "probe_requests": results,
        "cost": {
            "parameter_probe_http_requests": len(results),
            "parameter_probe_response_bytes": parameter_bytes,
            "parameter_probe_wall_seconds_sum": round(parameter_seconds, 3),
            "cycle_discovery_http_requests": len(gfs_attempts) + len(gefs_attempts),
            "cycle_discovery_response_bytes": sum(
                x.get("response_bytes", 0) or 0 for x in gfs_attempts + gefs_attempts
            ),
            "note": "Parameter requests are diagnostic separate requests. Production activation should bundle fields into existing per-product requests, so operational incremental cost is primarily returned GRIB message bytes, not seven new calls.",
        },
        "availability_index": availability,
        "isolation": {
            "production_payload_modified": False,
            "private_transfer_performed": False,
            "latest_success_modified": False,
            "raw_grib_persisted": False,
        },
    }


def markdown(data):
    lines = [
        "# NOAA Phase 2c bounded additional-parameter probe", "",
        f"- Generated: {data['generated_at_utc']}",
        f"- GFS run: {data['cycle_selection']['GFS']['selected_run_time_utc']}",
        f"- GEFS-control run: {data['cycle_selection']['GEFS-control']['selected_run_time_utc']}",
        f"- Parameter probe bytes: {data['cost']['parameter_probe_response_bytes']:,}",
        f"- Parameter probe requests: {data['cost']['parameter_probe_http_requests']}",
        "", "| Model | Product | Lead | HTTP | Bytes | Messages | Fields |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for item in data["probe_requests"]:
        fields = ", ".join(sorted(k for k in item.get("availability", {}) if k != "unclassified_candidate")) or "—"
        lines.append(
            f"| {item['model']} | {item['product']} | {item['lead_hours']} | "
            f"{item.get('http_status')} | {item.get('response_bytes',0):,} | "
            f"{item.get('message_count',0)} | {fields} |"
        )
    lines += ["", "## Native signatures", ""]
    for item in data["probe_requests"]:
        lines.append(f"### {item['model']} {item['product']} f{item['lead_hours']:03d}")
        if not item.get("availability"):
            lines.append("- No matching GRIB messages returned.")
            continue
        for canonical, meta in sorted(item["availability"].items()):
            lines.append(f"- **{canonical}** — {meta['messages']} message(s), {meta['grib_message_bytes']:,} GRIB bytes")
            for sig in meta["native_signatures"]:
                lines.append(
                    "  - " + ", ".join(f"{k}={sig.get(k)!r}" for k in (
                        "shortName","paramId","typeOfLevel","level","stepType","stepRange","units"
                    ))
                )
    lines += [
        "", "## Isolation proof", "",
        "- No model_snapshot.json write.",
        "- No private transfer.",
        "- No latest_success update.",
        "- Raw GRIB files are temporary and deleted before exit.",
    ]
    return "\n".join(lines) + "\n"


def self_test():
    fixed = datetime(2026, 9, 26, 0, tzinfo=timezone.utc)
    u = product_url("gfs_0p25", fixed, 24)
    assert "var_TMP=on" in u and "lev_2_m_above_ground=on" in u
    assert "file=gfs.t00z.pgrb2.0p25.f024" in u
    u = product_url("gefs_0p50b", fixed, 384)
    assert "filter_gefs_atmos_0p50b.pl" in u
    assert "pgrb2bp5" in u and "f384" in u
    assert len(PLAN) == 7
    print("self-test: ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="work/noaa_additional_parameter_probe.json")
    ap.add_argument("--md-out", default="work/noaa_additional_parameter_probe.md")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return
    data = run_probe()
    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md = Path(args.md_out); md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(markdown(data), encoding="utf-8")
    print(json.dumps({
        "gfs_run": data["cycle_selection"]["GFS"]["selected_run_time_utc"],
        "gefs_run": data["cycle_selection"]["GEFS-control"]["selected_run_time_utc"],
        "parameter_probe_http_requests": data["cost"]["parameter_probe_http_requests"],
        "parameter_probe_response_bytes": data["cost"]["parameter_probe_response_bytes"],
        "output": str(out),
    }, indent=2))
    print("--- BEGIN NOAA PROBE JSON ---")
    print(out.read_text(encoding="utf-8"))
    print("--- END NOAA PROBE JSON ---")


if __name__ == "__main__":
    main()
