import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import full_horizon_contract as c
import collect_full_horizon as fetch

def payload(hour=0):
    run = datetime(2026, 9, 25, hour, tzinfo=timezone.utc)
    d = {"mode": "production", "models": {}, "quality": {"sentinel": 1}, "retrieved_at_utc": (run+timedelta(hours=6)).isoformat()}
    for model in c.MODELS:
        rows = []
        for lead in c.sampled_leads(model, run):
            if lead > c.compatibility_hours(model, run):
                continue
            rows.append({"model": model, "run_time_utc": run.isoformat(), "forecast_lead_hours": lead,
                         "valid_time_utc": (run+timedelta(hours=lead)).isoformat(),
                         "forecast_coordinate_or_grid_point": {"latitude": 52.5, "longitude": 9.25},
                         "derived": {"wind_speed_ms": 5., "gust_ms": 7.}, "values": {}})
        d["models"][model] = rows
    return d

def rows(model, run, leads):
    return [{"model": model, "run_time_utc": run.isoformat(), "forecast_lead_hours": h,
             "valid_time_utc": (run+timedelta(hours=h)).isoformat(), "retrieved_at_utc": (run+timedelta(hours=7)).isoformat(),
             "provider_product": "test", "forecast_coordinate_or_grid_point": {"latitude": 52.5, "longitude": 9.5},
             "values": {"10u": [{"stepRange": str(h), "value": 3.}]},
             "derived": {"wind_speed_ms": 5., "gust_ms": 7.}} for h in leads]

class HorizonTests(unittest.TestCase):
    def test_cycle_maxima_and_sampling(self):
        for hour in (0, 6, 12, 18):
            run = datetime(2026, 9, 25, hour, tzinfo=timezone.utc)
            expected = {"ICON-D2": 48, "ICON-D2-EPS": 48, "ICON-EU": 120,
                        "ECMWF-IFS": 360 if hour in (0, 12) else 144,
                        "GFS": 384, "GEFS-control": 840 if hour == 0 else 384}
            for model, maximum in expected.items():
                self.assertEqual(max(c.sampled_leads(model, run)), maximum)
                self.assertEqual(len(c.sampled_leads(model, run)), len(set(c.sampled_leads(model, run))))

    def test_gefs_product_boundary_and_gust_secondary_product(self):
        run = datetime(2026, 9, 25, tzinfo=timezone.utc)
        self.assertEqual(len(fetch.noaa_requests("GEFS-control", run, 240)), 1)
        queries = fetch.noaa_requests("GEFS-control", run, 246)
        self.assertEqual(len(queries), 2)
        self.assertIn("pgrb2a.0p50.f246", queries[0][1])
        self.assertIn("pgrb2b.0p50.f246", queries[1][1])
        self.assertIn("var_GUST=on", queries[1][1])

    def collect(self, fail=False):
        d = payload()
        original = copy.deepcopy(d)
        def noaa(model, run, lead):
            if fail and model == "GFS" and lead == 180:
                raise RuntimeError("not yet published")
            return rows(model, run, [lead])
        with tempfile.TemporaryDirectory() as td, patch.object(fetch, "fetch_noaa", side_effect=noaa), patch.object(fetch, "fetch_ifs", side_effect=lambda p, h: rows("ECMWF-IFS", c.utc(p["models"]["ECMWF-IFS"][0]["run_time_utc"]), h)):
            path = Path(td)/"payload.json"
            summary = fetch.collect(d, path)
            self.assertEqual(json.loads(path.read_text()), d)
        for field in ("models", "quality", "retrieved_at_utc"):
            self.assertEqual(d[field], original[field])
        return d, summary

    def test_complete_archive_preserves_legacy_inputs_exactly(self):
        d, summary = self.collect()
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["sources"]["GEFS-control"]["received_extension_leads"][-1], 840)

    def test_partial_provider_preserves_successes_and_marks_gap(self):
        d, summary = self.collect(True)
        self.assertEqual(summary["sources"]["GFS"]["missing_leads"], [180])
        self.assertEqual(summary["sources"]["GEFS-control"]["status"], "complete")
        self.assertTrue(d["full_horizon_archive"]["sources"]["GFS"]["errors"])

    def test_corrupt_time_and_duplicate_rejected(self):
        d, _ = self.collect()
        source = d["full_horizon_archive"]["sources"]["GFS"]
        row = source["records"][0]
        row["valid_time_utc"] = row["run_time_utc"]
        with self.assertRaises(ValueError):
            c.validate_archive(d)

if __name__ == "__main__":
    unittest.main()
