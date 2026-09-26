import bz2
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import collect_icon_tier_a as t

UTC=timezone.utc


def row(model,run,lead):
    return {
        "model":model,
        "run_time_utc":run.isoformat(),
        "forecast_lead_hours":lead,
        "valid_time_utc":(run+timedelta(hours=lead)).isoformat(),
        "forecast_coordinate_or_grid_point":{"latitude":52.5,"longitude":9.25},
        "values":{},
        "derived":{"wind_speed_ms":5.0},
    }


class IconTierARoutineTests(unittest.TestCase):
    def test_inventory_uses_regular_grid_and_exact_cycle(self):
        hrefs=[
            "https://x/icon-d2_native_2026092603_012_2d_clct.grib2.bz2",
            "https://x/icon-d2_regular-lat-lon_2026092603_012_2d_clct.grib2.bz2",
            "https://x/icon-d2_regular-lat-lon_2026092600_012_2d_clct.grib2.bz2",
        ]
        with patch.object(t.dwd,"directory_hrefs",return_value=("https://x/",hrefs)):
            _,got=t.url_inventory("icon-d2","2026092603","clct")
        self.assertEqual(got,{12:hrefs[1]})

    def test_missing_field_is_explicit_null_never_zero(self):
        run=datetime(2026,9,26,3,tzinfo=UTC)
        value,diag,url=t.fetch_field("icon-d2","2026092603",12,"cin_ml",None,run,run+timedelta(hours=12))
        self.assertIsNone(value["value"])
        self.assertEqual(value["availability_status"],"not_offered")
        self.assertNotEqual(value["value"],0)
        self.assertEqual(diag["status"],"not_offered")
        self.assertIsNone(url)

    @patch("collect_icon_tier_a.base.grib_nearest")
    @patch("collect_icon_tier_a.message_metadata")
    @patch("collect_icon_tier_a.select_exact_message")
    @patch("collect_icon_tier_a.dwd.S.get")
    def test_received_field_preserves_native_metadata_and_hash(self,get,select,metadata,nearest):
        response=Mock()
        response.content=bz2.compress(b"GRIBfake")
        response.raise_for_status=Mock()
        get.return_value=response
        select.side_effect=lambda raw,selected,run,valid:selected.write_bytes(b"GRIBselected")
        metadata.return_value=[{
            "shortName":"tp","paramId":228,"units":"kg m**-2","typeOfLevel":"surface","level":0,
            "stepType":"accum","startStep":0,"endStep":12,"stepUnits":"1","stepRange":"0-12",
        }]
        nearest.return_value=[{"shortName":"tp","stepRange":"0-12","lat":52.5,"lon":9.25,"value":1.25}]
        run=datetime(2026,9,26,0,tzinfo=UTC)
        values,diag,url=t.fetch_field("icon-d2","2026092600",12,"tot_prec","https://x/file.bz2",run,run+timedelta(hours=12))
        self.assertEqual(diag["status"],"received")
        self.assertEqual(url,"https://x/file.bz2")
        self.assertEqual(values[0]["value"],1.25)
        self.assertEqual(values[0]["units"],"kg m**-2")
        self.assertEqual(values[0]["stepType"],"accum")
        self.assertEqual(values[0]["stepRange"],"0-12")
        self.assertEqual(values[0]["availability_status"],"received")
        self.assertEqual(len(values[0]["source_sha256"]),64)

    def test_attach_adds_four_fields_to_both_models_without_changing_wind(self):
        run=datetime(2026,9,26,0,tzinfo=UTC)
        snapshot={"models":{
            "ICON-D2":[row("ICON-D2",run,0),row("ICON-D2",run,3)],
            "ICON-EU":[row("ICON-EU",run,0),row("ICON-EU",run,6)],
        }}
        def inventory(model,cycle,param):
            return "x",{0:f"https://x/{model}/{param}/0",3:f"https://x/{model}/{param}/3",6:f"https://x/{model}/{param}/6"}
        def fetch(provider_model,cycle,lead,param,url,run,valid):
            return ([{"value":float(lead),"units":"1","shortName":param,"paramId":1,"typeOfLevel":"surface",
                      "level":0,"stepType":"instant","startStep":lead,"endStep":lead,"stepUnits":"1",
                      "stepRange":str(lead),"availability_status":"received","source_sha256":"a"*64}],
                    {"parameter":param,"lead_hours":lead,"status":"received","response_bytes":100,"elapsed_seconds":0.1},
                    url)
        with patch.object(t,"url_inventory",side_effect=inventory), patch.object(t,"fetch_field",side_effect=fetch):
            summary=t.attach(snapshot,workers=2)
        self.assertEqual(summary["total_response_bytes"],1600)
        self.assertEqual(summary["models"]["ICON-D2"]["received_fields"],8)
        self.assertEqual(summary["models"]["ICON-EU"]["received_fields"],8)
        for model in ("ICON-D2","ICON-EU"):
            for r in snapshot["models"][model]:
                self.assertEqual(set(t.TIER_A),set(r["values"]))
                self.assertEqual(r["derived"]["wind_speed_ms"],5.0)

    def test_tier_a_contract_is_exactly_four_fields(self):
        self.assertEqual(t.TIER_A,("tot_prec","cape_ml","cin_ml","clct"))


if __name__=="__main__":
    unittest.main()
