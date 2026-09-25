"""Versioned acquisition-only horizon contract; no forecast/decision policy.

The public contract deliberately remains provider-native. Verified payloads are
persisted by the private repository as mardorf-weather-archive-v2.
Full-horizon live proof generation: 2026-09-25-gefs-0p50-v2.
"""
from datetime import datetime, timezone
import math

VERSION = "full-horizon-archive-v1"
MODELS = ("ICON-D2", "ICON-D2-EPS", "ICON-EU", "ECMWF-IFS", "GFS", "GEFS-control")

def utc(value):
    x = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if x.tzinfo is None:
        raise ValueError("timezone required")
    return x.astimezone(timezone.utc)

def maximum_hours(model, run):
    if model in ("ICON-D2", "ICON-D2-EPS"):
        return 48
    if model == "ICON-EU":
        return 120 if run.hour in (0, 6, 12, 18) else 51
    if model == "ECMWF-IFS":
        return 360 if run.hour in (0, 12) else 144
    if model == "GFS":
        return 384
    if model == "GEFS-control":
        return 840 if run.hour == 0 else 384
    raise ValueError(model)

def compatibility_hours(model, run):
    if model == "ECMWF-IFS" and run.hour in (6, 18):
        return 90  # Existing, frozen input contract; full 144 h is archived separately.
    return min(120, maximum_hours(model, run))

def sampled_leads(model, run):
    end = maximum_hours(model, run)
    # Preserve existing cadence: 3h through 72h, 6h thereafter. No interpolation.
    return list(range(0, min(72, end) + 1, 3)) + list(range(78, end + 1, 6))

def extension_leads(model, run):
    return [h for h in sampled_leads(model, run) if h > compatibility_hours(model, run)]

def validate_archive(payload):
    """Recompute coverage; reject corrupt identities, never equate gaps with zero."""
    archive = payload.get("full_horizon_archive")
    if archive is None:
        return {"status": "absent_legacy", "sources": {}}
    if archive.get("method_version") != VERSION:
        raise ValueError("unknown full-horizon archive version")
    sources = archive.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(MODELS):
        raise ValueError("full-horizon source inventory mismatch")
    summary = {}
    for model in MODELS:
        source = sources[model]
        core = payload.get("models", {}).get(model, [])
        if not core:
            if source.get("records"):
                raise ValueError(f"{model}: archive lacks parent cycle")
            summary[model] = {"status": "parent_missing", "complete": False, "horizon_complete": False, "gust_complete": False}
            continue
        runs = {utc(r["run_time_utc"]) for r in core}
        if len(runs) != 1:
            raise ValueError(f"{model}: mixed parent cycles")
        run = next(iter(runs))
        if utc(source["run_time_utc"]) != run or source["target_max_hours"] != maximum_hours(model, run):
            raise ValueError(f"{model}: archive parent/target mismatch")
        expected = set(extension_leads(model, run))
        seen = set()
        missing_gust = []
        points = {}
        for row in source.get("records", []):
            h = row["forecast_lead_hours"]
            if isinstance(h, bool) or not isinstance(h, int) or h not in expected or h in seen:
                raise ValueError(f"{model}: duplicate/unexpected archive lead {h}")
            seen.add(h)
            if row.get("model") != model or utc(row["run_time_utc"]) != run:
                raise ValueError(f"{model}: archive model/run mismatch")
            if (utc(row["valid_time_utc"]) - run).total_seconds() != h * 3600:
                raise ValueError(f"{model}: archive validity mismatch")
            retrieved = utc(row["retrieved_at_utc"])
            if retrieved < run:
                raise ValueError(f"{model}: retrieval precedes run")
            p = row["forecast_coordinate_or_grid_point"]
            lat, lon = p["latitude"], p["longitude"]
            if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in (lat, lon)):
                raise ValueError(f"{model}: invalid coordinates")
            if abs(lat - 52.4942) > .30 or abs(lon - 9.3418) > .30:
                raise ValueError(f"{model}: wrong extraction location")
            product = row.get("provider_product")
            if not product:
                raise ValueError(f"{model}: product identity missing")
            point = (round(lat, 6), round(lon, 6))
            if product in points and points[product] != point:
                raise ValueError(f"{model}: grid changed within product")
            points[product] = point
            wind = row.get("derived", {}).get("wind_speed_ms")
            if not isinstance(wind, (int, float)) or isinstance(wind, bool) or not math.isfinite(wind) or wind < 0:
                raise ValueError(f"{model}: invalid wind_speed_ms")
            gust = row.get("derived", {}).get("gust_ms")
            if gust is None:
                availability = row.get("field_availability") or {}
                if not (model == "GEFS-control" and h > 240 and availability.get("gust") is False):
                    raise ValueError(f"{model}: gust missing without explicit provider-unavailable marker")
                missing_gust.append(h)
            elif not isinstance(gust, (int, float)) or isinstance(gust, bool) or not math.isfinite(gust) or gust < 0:
                raise ValueError(f"{model}: invalid gust_ms")
        core_leads = {r.get("forecast_lead_hours") for r in core if r.get("derived")}
        missing = sorted(set(sampled_leads(model, run)) - core_leads - seen)
        horizon_complete = not missing
        gust_complete = not missing_gust
        complete = horizon_complete and gust_complete
        status = "complete" if complete else ("partial_optional_fields" if horizon_complete else "partial")
        summary[model] = {"status": status, "complete": complete, "horizon_complete": horizon_complete,
                          "gust_complete": gust_complete, "target_max_hours": maximum_hours(model, run),
                          "received_extension_leads": sorted(seen), "missing_leads": missing,
                          "missing_gust_leads": sorted(missing_gust),
                          "product_grid_points": {k: list(v) for k, v in points.items()}}
    horizon_complete = all(s.get("horizon_complete", s.get("complete", False)) for s in summary.values())
    return {"status": "complete" if all(s["complete"] for s in summary.values()) else "partial",
            "horizon_status": "complete" if horizon_complete else "partial", "sources": summary}
