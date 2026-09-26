import bz2
import unittest
from unittest.mock import patch

import fetch_extra_models as extra
import extend_model_horizon as ext


class FakeResponse:
    def __init__(self, content=b"GRIBtest"):
        self.content=content
        self.status_code=200
    def raise_for_status(self):
        return None


def point_rows(cls, step="0"):
    rows=cls()
    rows.point={"latitude":52.5,"longitude":9.34,"selection":"ecCodes_nearest_grid_point"}
    rows.extend([("10u",step,3.0),("10v",step,4.0),("gust",step,6.0)])
    return rows


class ProviderGridIdentityTests(unittest.TestCase):
    def test_gefs_base_persists_selected_grid_point(self):
        evidence={
            "method_version":"gefs-newest-mature-cycle-selection-v1",
            "full_horizon_publication_required":False,
            "selected_cycle_run_time_utc":"2026-09-20T06:00:00+00:00",
            "selected_expected_max_lead_hours":384,
            "selected_publication_probe_lead":0,
            "attempts":[],
        }
        with patch.object(extra,"discover_gefs",return_value=("2026092006",evidence)), \
             patch.object(extra.S,"get",return_value=FakeResponse()), \
             patch.object(extra,"assert_grib_valid_time"), \
             patch.object(extra,"nearest",side_effect=lambda path: point_rows(extra.NearestRows)):
            rows=extra.fetch_gefs([0])
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["forecast_coordinate_or_grid_point"]["latitude"],52.5)
        self.assertEqual(rows[0]["forecast_coordinate_or_grid_point"]["longitude"],9.34)

    def test_icon_eu_extension_initializes_and_persists_grid_point(self):
        data={"models":{"ICON-EU":[{"run_time_utc":"2026-09-20T06:00:00+00:00"}]}}
        def fake_files(base,param):
            cycle=base.strftime("%Y%m%d%H")
            return [f"https://example.invalid/{cycle}_{param}_051_test.grib2.bz2"]
        compressed=bz2.compress(b"GRIBtest")
        with patch.object(ext,"leads_for_cycle",return_value=[51]), \
             patch.object(ext,"dwd_files",side_effect=fake_files), \
             patch.object(ext.S,"get",return_value=FakeResponse(compressed)), \
             patch.object(ext,"assert_grib_valid_time"), \
             patch.object(ext,"nearest",side_effect=lambda path: point_rows(ext.NearestRows,"51")):
            rows=ext.fetch_icon_eu(data)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["forecast_coordinate_or_grid_point"]["latitude"],52.5)
        self.assertEqual(rows[0]["forecast_coordinate_or_grid_point"]["longitude"],9.34)
        self.assertIn("derived",rows[0])


if __name__=="__main__":
    unittest.main()
