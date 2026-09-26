import unittest
from unittest.mock import patch

import fetch_dwd_additional_models as dwd
import fetch_extra_models as extra
import fetch_model_data as base
import extend_model_horizon as ext

class ProviderEdgeTests(unittest.TestCase):
    def test_open_meteo_session_retries_transient_503_bounded(self):
        retry=dwd.S.get_adapter("https://").max_retries
        self.assertEqual(retry.total,3)
        self.assertEqual(retry.connect,3)
        self.assertEqual(retry.read,3)
        self.assertEqual(retry.status,3)
        self.assertIn(503,retry.status_forcelist)
        self.assertEqual(retry.allowed_methods,frozenset(["GET"]))

    def test_ecmwf_requests_both_gust_aliases(self):
        for params in (extra.ECMWF_PARAMS,ext.ECMWF_PARAMS):
            self.assertIn("10fg",params)
            self.assertIn("10fg3",params)
            self.assertIn("10u",params)
            self.assertIn("10v",params)

    def test_gfs_full_validation_binds_base_cycle_to_terminal_f384(self):
        rows=[
            {"shortName":"10u","value":3.0,"lat":52.5,"lon":9.25},
            {"shortName":"10v","value":4.0,"lat":52.5,"lon":9.25},
            {"shortName":"gust","value":6.0,"lat":52.5,"lon":9.25},
        ]
        with patch.dict("os.environ",{"FULL_VALIDATION":"true"}), \
             patch.object(base,"discover_gfs_cycle",return_value="2026092600") as discover, \
             patch.object(base,"get_grib",return_value=b"GRIBtest"), \
             patch.object(base,"assert_grib_valid_time"), \
             patch.object(base,"grib_nearest",return_value=rows):
            out=base.fetch_gfs([0,48])
        discover.assert_called_once_with(384)
        self.assertEqual({x["forecast_lead_hours"] for x in out},{0,48})
        self.assertTrue(all(x["run_time_utc"]=="2026-09-26T00:00:00+00:00" for x in out))

    def test_gfs_normal_base_selection_keeps_requested_lead_probe(self):
        rows=[
            {"shortName":"10u","value":3.0,"lat":52.5,"lon":9.25},
            {"shortName":"10v","value":4.0,"lat":52.5,"lon":9.25},
            {"shortName":"gust","value":6.0,"lat":52.5,"lon":9.25},
        ]
        with patch.dict("os.environ",{"FULL_VALIDATION":"false"}), \
             patch.object(base,"discover_gfs_cycle",return_value="2026092606") as discover, \
             patch.object(base,"get_grib",return_value=b"GRIBtest"), \
             patch.object(base,"assert_grib_valid_time"), \
             patch.object(base,"grib_nearest",return_value=rows):
            base.fetch_gfs([0,48])
        discover.assert_called_once_with(48)

    def test_ecmwf_gust_alias_is_accepted_by_value_selection(self):
        vals={"10fg3":[{"value":12.5}]}
        def one(*names):
            for name in names:
                if name in vals and vals[name]:
                    return vals[name][0]["value"]
            return None
        self.assertEqual(one("10fg","10fg3","10fg6"),12.5)

if __name__=="__main__":
    unittest.main()
