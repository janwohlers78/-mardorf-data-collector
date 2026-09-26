import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import full_horizon_contract as c
import collect_full_horizon as fetch
import fetch_extra_models as extra

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
        expected_native = {
            0: {"ICON-D2":48,"ICON-D2-EPS":48,"ICON-EU":120,"ECMWF-IFS":360,"GFS":384,"GEFS-control":840},
            6: {"ICON-D2":48,"ICON-D2-EPS":48,"ICON-EU":120,"ECMWF-IFS":144,"GFS":384,"GEFS-control":384},
            12: {"ICON-D2":48,"ICON-D2-EPS":48,"ICON-EU":120,"ECMWF-IFS":360,"GFS":384,"GEFS-control":384},
            18: {"ICON-D2":48,"ICON-D2-EPS":48,"ICON-EU":120,"ECMWF-IFS":144,"GFS":384,"GEFS-control":384},
        }
        expected_acquisition_max = {
            0: {"ICON-D2":48,"ICON-D2-EPS":48,"ICON-EU":120,"ECMWF-IFS":360,"GFS":384,"GEFS-control":840},
            6: {"ICON-D2":48,"ICON-D2-EPS":48,"ICON-EU":120,"ECMWF-IFS":120,"GFS":120,"GEFS-control":120},
            12: {"ICON-D2":48,"ICON-D2-EPS":48,"ICON-EU":120,"ECMWF-IFS":360,"GFS":384,"GEFS-control":384},
            18: {"ICON-D2":48,"ICON-D2-EPS":48,"ICON-EU":120,"ECMWF-IFS":120,"GFS":120,"GEFS-control":120},
        }
        for hour in (0, 6, 12, 18):
            run = datetime(2026, 9, 25, hour, tzinfo=timezone.utc)
            for model in c.MODELS:
                self.assertEqual(c.maximum_hours(model, run), expected_native[hour][model])
                self.assertEqual(max(c.acquisition_leads(model, run)), expected_acquisition_max[hour][model])
                self.assertEqual(len(c.acquisition_leads(model, run)), len(set(c.acquisition_leads(model, run))))

    def test_acquisition_grid_v2_exact_sparse_long_range_bands(self):
        run00 = datetime(2026, 9, 25, 0, tzinfo=timezone.utc)
        run06 = datetime(2026, 9, 25, 6, tzinfo=timezone.utc)
        gfs00 = c.acquisition_leads("GFS", run00)
        self.assertEqual([h for h in gfs00 if 132 <= h <= 240], list(range(132,241,12)))
        self.assertEqual([h for h in gfs00 if 240 < h <= 384], list(range(264,385,24)))
        self.assertNotIn(126, gfs00)
        self.assertNotIn(246, gfs00)
        self.assertEqual(c.acquisition_leads("GFS", run06)[-1], 120)
        gefs00 = c.acquisition_leads("GEFS-control", run00)
        self.assertEqual([h for h in gefs00 if h > 384], list(range(408,841,24)))
        self.assertEqual(len(gefs00), 68)
        self.assertEqual(c.ACQUISITION_GRID_VERSION, "acquisition-grid-v2")

    def test_extension_leads_begin_after_compatibility_horizon(self):
        run06 = datetime(2026, 9, 25, 6, tzinfo=timezone.utc)
        self.assertEqual(c.compatibility_hours("ECMWF-IFS", run06), 90)
        self.assertEqual(c.extension_leads("ECMWF-IFS", run06), [96,102,108,114,120])
        self.assertEqual(c.extension_leads("GFS", run06), [])
        self.assertEqual(c.extension_leads("GEFS-control", run06), [])

    def test_gefs_cycle_specific_contract_and_maximum(self):
        run00 = datetime(2026, 9, 25, 0, tzinfo=timezone.utc)
        run06 = datetime(2026, 9, 25, 6, tzinfo=timezone.utc)
        self.assertEqual(c.maximum_hours("GEFS-control", run00), 840)
        self.assertEqual(c.maximum_hours("GEFS-control", run06), 384)
        self.assertEqual(c.gefs_lead_contract(run00, 840)["wind_product"], "gefs_0p50a")
        self.assertTrue(c.gefs_lead_contract(run00, 840)["expected"])
        self.assertFalse(c.gefs_lead_contract(run06, 390)["expected"])

    def test_gefs_discovery_full_validation_falls_back_to_cycle_with_published_cycle_max(self):
        calls = []
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 25, 23, 0, tzinfo=timezone.utc)

        class Response:
            status_code = 200
            def __init__(self, ok):
                self.content = b"GRIB-test" if ok else b"not-grib"

        def get(url, timeout=None):
            calls.append(url)
            return Response("f840" in url)

        with patch.object(extra, "datetime", FixedDateTime), patch.object(extra.S, "get", side_effect=get):
            selected = extra.discover_gefs(48, require_far_horizon=True)
        self.assertEqual(selected, "2026092500")
        self.assertTrue(all("f384" in u for u in calls[:3]))
        self.assertIn("f840", calls[3])

    def test_gefs_discovery_persists_newer_cycle_maturity_evidence(self):
        calls = []
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 25, 23, 0, tzinfo=timezone.utc)

        class Response:
            status_code = 200
            def __init__(self, ok):
                self.content = b"GRIB-test" if ok else b"not-grib"

        def get(url, timeout=None):
            calls.append(url)
            return Response("f840" in url)

        with patch.object(extra, "datetime", FixedDateTime), patch.object(extra.S, "get", side_effect=get):
            selected,evidence = extra.discover_gefs(48, require_far_horizon=True, return_evidence=True)
        self.assertEqual(selected, "2026092500")
        self.assertEqual(evidence["method_version"], "gefs-newest-mature-cycle-selection-v1")
        self.assertTrue(evidence["full_horizon_publication_required"])
        self.assertEqual(evidence["selected_cycle_run_time_utc"], "2026-09-25T00:00:00+00:00")
        self.assertEqual(evidence["selected_publication_probe_lead"], 840)
        self.assertEqual([x["status"] for x in evidence["attempts"]], ["not_published","not_published","not_published","published"])
        self.assertEqual([x["publication_probe_lead"] for x in evidence["attempts"]], [384,384,384,840])

    def test_gefs_product_boundary_and_gust_secondary_product(self):
        run = datetime(2026, 9, 25, tzinfo=timezone.utc)
        self.assertEqual(len(fetch.noaa_requests("GEFS-control", run, 240)), 1)
        queries = fetch.noaa_requests("GEFS-control", run, 246)
        self.assertEqual(len(queries), 2)
        self.assertIn("filter_gefs_atmos_0p50a.pl", queries[0][1])
        self.assertIn("pgrb2a.0p50.f246", queries[0][1])
        self.assertIn("filter_gefs_atmos_0p50b.pl", queries[1][1])
        self.assertIn("pgrb2b.0p50.f246", queries[1][1])
        self.assertIn("var_DPT=on", queries[1][1])
        self.assertIn("var_CAPE=on", queries[1][1])
        self.assertIn("var_CIN=on", queries[1][1])
        self.assertNotIn("var_GUST=on", queries[1][1])
        self.assertNotIn("lev_10_m_above_ground=on", queries[1][1])
        self.assertTrue(queries[0][2])
        self.assertFalse(queries[1][2])
        near = parse_qs(urlparse(fetch.noaa_requests("GEFS-control", run, 240)[0][1]).query)
        far = parse_qs(urlparse(queries[0][1]).query)
        self.assertAlmostEqual(float(near["rightlon"][0]) - float(near["leftlon"][0]), 0.60, places=3)
        self.assertAlmostEqual(float(far["rightlon"][0]) - float(far["leftlon"][0]), 1.50, places=3)
        self.assertLess(float(far["bottomlat"][0]), fetch.ext.LAT)
        self.assertGreater(float(far["toplat"][0]), fetch.ext.LAT)
        far_probe = parse_qs(urlparse(extra.gefs_far_url("2026092500", 840)).query)
        self.assertAlmostEqual(float(far_probe["rightlon"][0]) - float(far_probe["leftlon"][0]), 1.50, places=3)

    def test_gefs_far_horizon_keeps_wind_when_secondary_gust_product_lags(self):
        run = datetime(2026, 9, 25, 6, tzinfo=timezone.utc)

        class Response:
            def __init__(self, content, fail=False):
                self.content = content
                self.fail = fail
            def raise_for_status(self):
                if self.fail:
                    raise RuntimeError("secondary product not yet published")

        class Session:
            def __init__(self):
                self.headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def get(self, url, timeout=None):
                return Response(b"not-grib", fail=True) if "pgrb2b" in url else Response(b"GRIB-test")

        class Nearest(list):
            point = {"latitude": 52.5, "longitude": 9.25}

        values={
            "10u":[{"shortName":"10u","stepRange":"246","value":3.0}],
            "10v":[{"shortName":"10v","stepRange":"246","value":4.0}],
            "tp":[{"shortName":"tp","stepRange":"240-246","value":0.2}],
        }
        point={"latitude":52.5,"longitude":9.25,"selection":"ecCodes_nearest_grid_point"}
        with patch.object(fetch.requests, "Session", Session), \
             patch.object(fetch.ext, "assert_grib_valid_time"), \
             patch.object(fetch.noaa, "extract_native_values", return_value=(values,point)):
            row = fetch.fetch_noaa("GEFS-control", run, 246)[0]
        self.assertAlmostEqual(row["derived"]["wind_speed_ms"], 5.0)
        self.assertNotIn("gust_ms", row["derived"])
        self.assertEqual(row["field_availability"], {"wind_uv": True, "gust": False})
        self.assertEqual(row["provider_product"], "gefs_0p50a")
        self.assertEqual(row["optional_product_errors"][0]["product"], "gefs_0p50b")

    def test_gefs_pgrb2b_supplement_does_not_download_0p50a(self):
        run=datetime(2026,9,25,0,tzinfo=timezone.utc)
        record={
            "model":"GEFS-control",
            "run_time_utc":run.isoformat(),
            "forecast_lead_hours":264,
            "valid_time_utc":(run+timedelta(hours=264)).isoformat(),
            "provider_product":"gefs_0p50a",
            "forecast_coordinate_or_grid_point":{"latitude":52.5,"longitude":9.25},
            "values":{"10u":[{"value":3.0}],"10v":[{"value":4.0}]},
            "grib_evidence":[{"product":"gefs_0p50a","url":"a-url","sha256":"a"*64,"response_bytes":10}],
            "source_urls":["a-url"],
            "optional_product_errors":[{"product":"gefs_0p50b","url":"b-url","reason":"not yet"}],
            "weather_context_availability":{},
            "derived":{"wind_speed_ms":5.0},
        }
        requests=[
            ("gefs_0p50a","a-url",True),
            ("gefs_0p50b","b-url",False),
        ]
        b_values={"2d":[{"shortName":"2d","value":281.0,"source_sha256":"b"*64}]}
        point={"latitude":52.5,"longitude":9.25}
        with patch.object(fetch,"noaa_requests",return_value=requests), \
             patch.object(fetch,"_download",return_value=b"GRIB-test") as download, \
             patch.object(fetch.ext,"assert_grib_valid_time"), \
             patch.object(fetch.noaa,"extract_native_values",return_value=(b_values,point)), \
             patch.object(fetch.noaa,"weather_availability",return_value={"dewpoint_2m":"received"}), \
             patch.object(fetch.availability,"stamp_rows"):
            updated=fetch.retry_gefs_pgrb2b(record,run)
        self.assertEqual(download.call_count,1)
        self.assertEqual(download.call_args.args[1],"b-url")
        self.assertEqual(updated["provider_product"],"gefs_0p50a+gefs_0p50b")
        self.assertEqual(updated["optional_product_errors"],[])
        self.assertEqual(updated["values"]["2d"][0]["value"],281.0)

    def test_gefs_pgrb2b_retry_is_delayed_once_then_exhausted(self):
        source={
            "records":[{
                "forecast_lead_hours":264,
                "optional_product_errors":[{"product":"gefs_0p50b"}],
            }]
        }
        fetch.update_pgrb2b_retry_state(source,"fetch")
        state=source["gefs_pgrb2b_supplemental_retry"]
        self.assertEqual(state["status"],"pending")
        self.assertEqual(state["attempts"],0)
        self.assertIsNotNone(state["next_retry_not_before_utc"])
        fetch.update_pgrb2b_retry_state(source,"supplemental_retry",[{"lead_hours":264,"status":"fetch_error"}])
        state=source["gefs_pgrb2b_supplemental_retry"]
        self.assertEqual(state["status"],"exhausted")
        self.assertEqual(state["attempts"],1)
        self.assertIsNone(state["next_retry_not_before_utc"])

    def collect(self, fail=False, hour=0):
        d = payload(hour)
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

    def test_nonzero_gefs_cycle_records_native_horizon_but_does_not_download_discarded_far_leads(self):
        d, summary = self.collect(hour=6)
        source = d["full_horizon_archive"]["sources"]["GEFS-control"]
        gefs = summary["sources"]["GEFS-control"]
        self.assertEqual(source["provider_native_horizon_hours"], 384)
        self.assertEqual(source["compatibility_horizon_hours"], 120)
        self.assertEqual(source["acquisition_max_lead_hours"], 120)
        self.assertEqual(source["requested_extension_leads"], [])
        self.assertEqual(source["actual_max_lead"], 120)
        self.assertEqual(gefs["expected_max_lead_for_cycle"], 384)
        self.assertEqual(gefs["actual_max_lead"], 120)
        self.assertEqual(source["lead_status"], {})
        self.assertEqual(gefs["missing_leads"], [])

    def test_partial_provider_preserves_successes_and_marks_gap(self):
        d, summary = self.collect(True)
        self.assertEqual(summary["sources"]["GFS"]["missing_leads"], [180])
        self.assertEqual(summary["sources"]["GEFS-control"]["status"], "complete")
        self.assertTrue(d["full_horizon_archive"]["sources"]["GFS"]["errors"])

    def test_explicit_far_gefs_gust_gap_preserves_complete_wind_horizon(self):
        d, summary = self.collect()
        source = d["full_horizon_archive"]["sources"]["GEFS-control"]
        row = next(r for r in source["records"] if r["forecast_lead_hours"] > 240)
        row["derived"].pop("gust_ms")
        row["field_availability"] = {"wind_uv": True, "gust": False}
        summary = c.validate_archive(d)
        gefs = summary["sources"]["GEFS-control"]
        self.assertTrue(gefs["horizon_complete"])
        self.assertFalse(gefs["gust_complete"])
        self.assertEqual(gefs["missing_leads"], [])
        self.assertEqual(gefs["status"], "partial_optional_fields")
        self.assertEqual(summary["horizon_status"], "complete")
        self.assertEqual(summary["status"], "partial")

    def test_optional_gefs_gust_gap_does_not_fail_archive_exit_code(self):
        d, _ = self.collect()
        source = d["full_horizon_archive"]["sources"]["GEFS-control"]
        row = next(r for r in source["records"] if r["forecast_lead_hours"] > 240)
        row["derived"].pop("gust_ms")
        row["field_availability"] = {"wind_uv": True, "gust": False}
        summary = c.validate_archive(d)
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["horizon_status"], "complete")
        self.assertEqual(fetch.archive_exit_code(summary), 0)

    def test_missing_required_horizon_still_fails_archive_exit_code(self):
        _, summary = self.collect(True)
        self.assertNotEqual(summary["horizon_status"], "complete")
        self.assertEqual(fetch.archive_exit_code(summary), 1)

    def test_far_gefs_missing_gust_without_marker_is_rejected(self):
        d, _ = self.collect()
        source = d["full_horizon_archive"]["sources"]["GEFS-control"]
        row = next(r for r in source["records"] if r["forecast_lead_hours"] > 240)
        row["derived"].pop("gust_ms")
        with self.assertRaises(ValueError):
            c.validate_archive(d)

    def test_corrupt_time_and_duplicate_rejected(self):
        d, _ = self.collect()
        source = d["full_horizon_archive"]["sources"]["GFS"]
        row = source["records"][0]
        row["valid_time_utc"] = row["run_time_utc"]
        with self.assertRaises(ValueError):
            c.validate_archive(d)

if __name__ == "__main__":
    unittest.main()
