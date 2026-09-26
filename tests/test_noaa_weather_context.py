import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import collect_full_horizon as full
import fetch_extra_models as extra
import fetch_model_data as base
import noaa_weather_context as noaa


class NoaaWeatherContextTests(unittest.TestCase):
    def query(self,url):
        return parse_qs(urlparse(url).query)

    def test_gfs_production_request_contains_all_confirmed_weather_fields(self):
        q=self.query(base.gfs_url("2026092600",24,probe=False))
        for var in ("TMP","DPT","RH","PRMSL","PRES","TCDC","CAPE","CIN","DSWRF"):
            self.assertEqual(q["var_"+var],["on"])
        for lev in ("2_m_above_ground","mean_sea_level","surface","entire_atmosphere",
                    "180-0_mb_above_ground","90-0_mb_above_ground","255-0_mb_above_ground"):
            self.assertEqual(q["lev_"+lev],["on"])

    def test_gfs_cycle_discovery_probe_remains_minimal(self):
        q=self.query(base.gfs_url("2026092600",384,probe=True))
        self.assertIn("var_UGRD",q)
        self.assertIn("var_VGRD",q)
        self.assertNotIn("var_TMP",q)
        self.assertNotIn("var_CAPE",q)

    def test_gefs_discovery_probe_remains_minimal(self):
        near=self.query(extra.gefs_required_url("2026092518",240))
        far=self.query(extra.gefs_required_url("2026092518",384))
        self.assertNotIn("var_TMP",near)
        self.assertNotIn("var_TMP",far)
        self.assertIn("var_UGRD",near)
        self.assertIn("var_UGRD",far)

    def test_gefs_full_requests_are_product_specific(self):
        run=datetime(2026,9,25,18,tzinfo=timezone.utc)
        near=self.query(full.noaa_requests("GEFS-control",run,240)[0][1])
        self.assertIn("var_DPT",near)
        self.assertIn("var_DSWRF",near)
        far=full.noaa_requests("GEFS-control",run,384)
        self.assertEqual(len(far),2)
        a=self.query(far[0][1]); b=self.query(far[1][1])
        for var in ("TMP","RH","PRMSL","PRES","TCDC","CAPE","CIN","DSWRF"):
            self.assertIn("var_"+var,a)
        self.assertNotIn("var_DPT",a)
        for var in ("DPT","CAPE","CIN"):
            self.assertIn("var_"+var,b)
        self.assertNotIn("var_TCDC",b)
        self.assertNotIn("var_DSWRF",b)

    def test_weather_availability_distinguishes_native_fields(self):
        values={
            "2t":[{"shortName":"2t","name":"2 metre temperature","typeOfLevel":"heightAboveGround","level":2}],
            "2d":[{"shortName":"2d","name":"2 metre dewpoint temperature","typeOfLevel":"heightAboveGround","level":2}],
            "tcc":[
                {"shortName":"tcc","name":"Total Cloud Cover","typeOfLevel":"atmosphere","level":0,"stepType":"instant"},
                {"shortName":"tcc","name":"Total Cloud Cover","typeOfLevel":"atmosphere","level":0,"stepType":"avg"},
            ],
            "cape":[
                {"shortName":"cape","name":"Convective available potential energy","typeOfLevel":"surface","level":0},
                {"shortName":"cape","name":"Convective available potential energy","typeOfLevel":"pressureFromGroundLayer","level":18000},
            ],
        }
        got=noaa.weather_availability("gfs_0p25",values)
        self.assertTrue(got["TMP"])
        self.assertTrue(got["DPT"])
        self.assertTrue(got["TCDC"])
        self.assertTrue(got["CAPE"])
        self.assertFalse(got["DSWRF"])

    def test_all_returned_variants_are_preserved_as_lists(self):
        # Storage relies on keeping distinct native messages rather than
        # collapsing stepType/level variants to one scalar.
        values={
            "tcc":[
                {"shortName":"tcc","stepType":"instant","stepRange":"120","value":42.0},
                {"shortName":"tcc","stepType":"avg","stepRange":"114-120","value":55.0},
            ],
            "cape":[
                {"shortName":"cape","typeOfLevel":"surface","level":0,"value":100.0},
                {"shortName":"cape","typeOfLevel":"pressureFromGroundLayer","level":18000,"value":200.0},
            ],
        }
        self.assertEqual(len(values["tcc"]),2)
        self.assertEqual({x["stepType"] for x in values["tcc"]},{"instant","avg"})
        self.assertEqual(len(values["cape"]),2)
        self.assertEqual({x["level"] for x in values["cape"]},{0,18000})


if __name__=="__main__":
    unittest.main()
