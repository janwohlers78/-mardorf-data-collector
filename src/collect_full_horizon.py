#!/usr/bin/env python3
"""Collect each existing model's remaining native horizon into an archive sidecar.

The legacy models/quality/retrieved_at fields are deliberately untouched. Each
completed chunk is checkpointed, including coverage gaps, before transfer.
"""
import hashlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
import extend_model_horizon as ext
from full_horizon_contract import VERSION, MODELS, utc, maximum_hours, extension_leads, validate_archive

SNAP = Path(os.getenv("COLLECTOR_MODEL_FILE", "work/model_snapshot.json"))

def now():
    return datetime.now(timezone.utc).isoformat()

def noaa_requests(model, run, lead):
    day, hh = run.strftime("%Y%m%d"), run.strftime("%H")
    if model == "GFS":
        products = [("gfs_0p25", "filter_gfs_0p25.pl", f"/gfs.{day}/{hh}/atmos",
                     f"gfs.t{hh}z.pgrb2.0p25.f{lead:03d}", ["UGRD", "VGRD", "GUST", "APCP"], True)]
    elif lead <= 240:
        products = [("gefs_0p25s", "filter_gefs_atmos_0p25s.pl", f"/gefs.{day}/{hh}/atmos/pgrb2sp25",
                     f"gec00.t{hh}z.pgrb2s.0p25.f{lead:03d}", ["UGRD", "VGRD", "GUST", "APCP"], True)]
    else:
        # The 0.25-degree selected-parameter GEFS product ends at FH240.
        # NOMADS publishes FH246+ through the 0.5-degree pgrb2a/pgrb2b trees,
        # both served by the generic GENS 0.5-degree CGI filter. Product a
        # carries U/V wind and precipitation; product b carries gust and can
        # lag product a. Keep the wind record when b is temporarily absent.
        products = [
            ("gefs_0p50a", "filter_gens_0p50.pl", f"/gefs.{day}/{hh}/atmos/pgrb2ap5",
             f"gec00.t{hh}z.pgrb2a.0p50.f{lead:03d}", ["UGRD", "VGRD", "APCP"], True),
            ("gefs_0p50b", "filter_gens_0p50.pl", f"/gefs.{day}/{hh}/atmos/pgrb2bp5",
             f"gec00.t{hh}z.pgrb2b.0p50.f{lead:03d}", ["GUST"], False),
        ]
    result = []
    for product, script, directory, filename, variables, required in products:
        query = {"file": filename, "dir": directory, "lev_10_m_above_ground": "on", "lev_surface": "on",
                 "subregion": "", "leftlon": "9.0418", "rightlon": "9.6418", "toplat": "52.7942", "bottomlat": "52.1942"}
        query.update({"var_" + v: "on" for v in variables})
        result.append((product, "https://nomads.ncep.noaa.gov/cgi-bin/" + script + "?" + urlencode(query), required))
    return result

def fetch_noaa(model, run, lead):
    vals, evidence, optional_errors, point = {}, [], [], None
    with requests.Session() as session, tempfile.TemporaryDirectory() as td:
        session.headers.update({"User-Agent": "mardorf-data-collector/full-horizon-v1"})
        for product, url, required in noaa_requests(model, run, lead):
            try:
                response = session.get(url, timeout=(10, 45))
                response.raise_for_status()
                if not response.content.startswith(b"GRIB"):
                    raise ValueError("NOMADS response is not GRIB")
                path = Path(td) / (product + ".grib2")
                path.write_bytes(response.content)
                ext.assert_grib_valid_time(path, run, run + timedelta(hours=lead), f"{model} archive {lead}")
                rows = ext.nearest(path)
                if point is not None and point != rows.point:
                    raise ValueError("GEFS a/b extraction points differ")
                point = rows.point
                for name, step, value in rows:
                    vals.setdefault(name, []).append({"stepRange": step, "value": value})
                evidence.append({"product": product, "url": url, "sha256": hashlib.sha256(response.content).hexdigest()})
            except Exception as exc:
                if required:
                    raise
                optional_errors.append({"product": product, "url": url, "type": type(exc).__name__,
                                        "reason": str(exc)[:400]})
    def one(*names):
        for name in names:
            if vals.get(name):
                return vals[name][0]["value"]
        return None
    u, v, gust = one("10u", "u"), one("10v", "v"), one("gust", "10fg")
    if u is None or v is None:
        raise ValueError("required U/V absent from returned product")
    allow_missing_gust = model == "GEFS-control" and lead > 240
    if gust is None and not allow_missing_gust:
        raise ValueError("required gust absent from returned product")
    return [{"model": model, "run_time_utc": run.isoformat(), "forecast_lead_hours": lead,
             "valid_time_utc": (run + timedelta(hours=lead)).isoformat(), "retrieved_at_utc": now(),
             "provider_product": "+".join(x["product"] for x in evidence),
             "source": "NOAA/NCEP NOMADS GRIB2 full horizon", "source_urls": [x["url"] for x in evidence],
             "grib_evidence": evidence, "optional_product_errors": optional_errors,
             "field_availability": {"wind_uv": True, "gust": gust is not None},
             "forecast_coordinate_or_grid_point": point,
             "values": vals, "derived": ext.derived(u, v, gust)}]

def fetch_ifs(payload, leads):
    rows = ext.fetch_ifs(payload, requested_leads=leads)
    for row in rows:
        row.update(retrieved_at_utc=now(), provider_product="ifs_oper_fc_0p25")
    return rows

def checkpoint(payload, path):
    archive = payload["full_horizon_archive"]
    for source in archive["sources"].values():
        source["records"].sort(key=lambda r: r["forecast_lead_hours"])
    archive["coverage"] = validate_archive(payload)
    archive["updated_at_utc"] = now()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n")
    tmp.replace(path)

def collect(payload, path, workers=4):
    sources, jobs = {}, []
    for model in MODELS:
        core = payload.get("models", {}).get(model, [])
        source = {"records": [], "errors": []}
        sources[model] = source
        if not core:
            source["errors"].append({"reason": "parent_cycle_missing"})
            continue
        run = ext.cycle_from_existing(payload, model)
        leads = extension_leads(model, run)
        source.update(run_time_utc=run.isoformat(), target_max_hours=maximum_hours(model, run),
                      requested_extension_leads=leads)
        if model == "ECMWF-IFS":
            # Small independent chunks keep successful downloads if a later step is unpublished.
            jobs.extend((model, leads[i:i+6]) for i in range(0, len(leads), 6))
        else:
            jobs.extend((model, [lead]) for lead in leads)
    payload["full_horizon_archive"] = {"method_version": VERSION, "started_at_utc": now(), "sources": sources}
    checkpoint(payload, path)
    # Keep each provider bounded: at most four simultaneous point-subset requests.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for model, leads in jobs:
            if model == "ECMWF-IFS":
                future = pool.submit(fetch_ifs, payload, leads)
            else:
                future = pool.submit(fetch_noaa, model, utc(sources[model]["run_time_utc"]), leads[0])
            futures[future] = (model, leads)
        for future in as_completed(futures):
            model, leads = futures[future]
            try:
                rows = future.result()
                prior = list(sources[model]["records"])
                sources[model]["records"].extend(rows)
                try:
                    validate_archive(payload)
                except Exception:
                    sources[model]["records"] = prior
                    raise
            except Exception as exc:
                sources[model]["errors"].append({"leads": leads, "type": type(exc).__name__, "reason": str(exc)[:600]})
            checkpoint(payload, path)
    return payload["full_horizon_archive"]["coverage"]

def main():
    payload = json.loads(SNAP.read_text())
    if payload.get("mode") == "test":
        raise RuntimeError("full-horizon collection is production-only")
    result = collect(payload, SNAP)
    print(json.dumps(result, sort_keys=True))
    if result["status"] != "complete":
        raise SystemExit(1)

if __name__ == "__main__":
    main()
