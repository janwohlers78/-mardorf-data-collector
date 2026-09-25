"""Versioned acquisition-only horizon contract; no forecast/decision policy."""
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
            summary[model] = {"status": "parent_missing", "complete": False}
            continue
        runs = {utc(r["run_time_utc"]) for r in core}
        if len(runs) != 1:
            raise ValueError(f"{model}: mixed parent cycles")
        run = next(iter(runs))
        if utc(source["run_time_utc"]) != run or source["target_max_hours"] != maximum_hours(model, run):
            raise ValueError(f"{model}: archive parent/target mismatch")
        expected = set(extension_leads(model, run))
        seen = set()
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
            for field in ("wind_speed_ms", "gust_ms"):
                value = row.get("derived", {}).get(field)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                    raise ValueError(f"{model}: invalid {field}")
        core_leads = {r.get("forecast_lead_hours") for r in core if r.get("derived")}
        missing = sorted(set(sampled_leads(model, run)) - core_leads - seen)
        summary[model] = {"status": "complete" if not missing else "partial",
                          "complete": not missing, "target_max_hours": maximum_hours(model, run),
                          "received_extension_leads": sorted(seen), "missing_leads": missing,
                          "product_grid_points": {k: list(v) for k, v in points.items()}}
    return {"status": "complete" if all(s["complete"] for s in summary.values()) else "partial",
            "sources": summary}
