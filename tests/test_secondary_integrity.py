import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path

from audit_secondary_integrity import audit_wunstorf,audit_etnw

POLICY=json.loads(Path("config/integrity_policy.json").read_text(encoding="utf-8"))


class SecondaryIntegrityTests(unittest.TestCase):
    def test_wunstorf_valid_bundle_passes(self):
        now=datetime.now(timezone.utc)
        rows=[
            {"time_utc":(now-timedelta(hours=3-i)).replace(minute=0,second=0,microsecond=0).isoformat(),
             "wind_speed_ms":4.0+i,"wind_direction_deg":250.0+i}
            for i in range(4)
        ]
        d={
            "schema_version":2,"retrieved_at_utc":now.isoformat(),"provider":"DWD",
            "dataset":"hourly_wind_synop_recent",
            "source_role":"historical_land_reference_not_operational_realtime_source",
            "station":{"id":"05715","name":"Wunstorf"},
            "latest_observation_time_utc":rows[-1]["time_utc"],"latest_observation":rows[-1],
            "observations":rows,"quality":{"success":True}
        }
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"w.json";p.write_text(json.dumps(d))
            r=audit_wunstorf(p,POLICY,now)
        self.assertEqual(r["error_count"],0,r["issues"])
        self.assertTrue(r["bundle_ready_for_private_revalidation"])

    def test_wunstorf_identity_mismatch_is_error(self):
        now=datetime.now(timezone.utc)
        row={"time_utc":(now-timedelta(hours=1)).isoformat(),"wind_speed_ms":4.0,"wind_direction_deg":250.0}
        d={"retrieved_at_utc":now.isoformat(),"provider":"DWD","station":{"id":"99999"},
           "latest_observation_time_utc":row["time_utc"],"latest_observation":row,"observations":[row]}
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"w.json";p.write_text(json.dumps(d))
            r=audit_wunstorf(p,POLICY,now)
        self.assertTrue(any(x["code"]=="WUNSTORF_SOURCE_IDENTITY_MISMATCH" for x in r["issues"]))
        self.assertFalse(r["bundle_ready_for_private_revalidation"])

    def test_etnw_valid_bundle_passes(self):
        now=datetime.now(timezone.utc)
        rows=[
            {"time_utc":(now-timedelta(minutes=60-i*30)).replace(second=0,microsecond=0).isoformat(),
             "wind_speed_ms":5.0,"wind_gust_ms":7.0,"wind_direction_deg":270.0,"wind_direction_variable":False}
            for i in range(3)
        ]
        d={
            "schema_version":3,"retrieved_at_utc":now.isoformat(),"provider":"AviationWeather.gov",
            "source_role":"current_operational_Wunstorf_redundancy",
            "station":{"icao":"ETNW","name":"Fliegerhorst Wunstorf"},
            "latest_observation_time_utc":rows[-1]["time_utc"],"latest_observation":rows[-1],
            "observations":rows,"quality":{"success":True}
        }
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"e.json";p.write_text(json.dumps(d))
            r=audit_etnw(p,POLICY,now)
        self.assertEqual(r["error_count"],0,r["issues"])
        self.assertTrue(r["bundle_ready_for_private_revalidation"])

    def test_etnw_stale_current_source_is_error(self):
        now=datetime.now(timezone.utc)
        t=(now-timedelta(hours=7)).isoformat()
        row={"time_utc":t,"wind_speed_ms":5.0,"wind_gust_ms":None,"wind_direction_deg":270.0,"wind_direction_variable":False}
        d={"retrieved_at_utc":now.isoformat(),"provider":"AviationWeather.gov","station":{"icao":"ETNW"},
           "latest_observation_time_utc":t,"latest_observation":row,"observations":[row]}
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"e.json";p.write_text(json.dumps(d))
            r=audit_etnw(p,POLICY,now)
        self.assertTrue(any(x["code"]=="ETNW_OBSERVATION_TOO_OLD" for x in r["issues"]))
        self.assertFalse(r["bundle_ready_for_private_revalidation"])

    def test_etnw_variable_direction_is_valid(self):
        now=datetime.now(timezone.utc)
        t=(now-timedelta(minutes=20)).isoformat()
        row={"time_utc":t,"wind_speed_ms":5.0,"wind_gust_ms":None,
             "wind_direction_deg":None,"wind_direction_variable":True}
        d={"retrieved_at_utc":now.isoformat(),"provider":"AviationWeather.gov","station":{"icao":"ETNW"},
           "latest_observation_time_utc":t,"latest_observation":row,"observations":[row]}
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"e.json";p.write_text(json.dumps(d))
            r=audit_etnw(p,POLICY,now)
        self.assertEqual(r["error_count"],0,r["issues"])


if __name__=="__main__":
    unittest.main()
