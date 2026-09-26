import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import icon_parameter_probe as p

UTC=timezone.utc


class IconParameterProbeTests(unittest.TestCase):
    @patch("icon_parameter_probe.subprocess.run")
    def test_message_metadata_keeps_units_and_interval_fields(self, run):
        outputs={
            "shortName":"2t\n","paramId":"167\n","units":"K\n","typeOfLevel":"heightAboveGround\n",
            "level":"2\n","stepType":"instant\n","startStep":"12\n","endStep":"12\n",
            "stepUnits":"1\n","stepRange":"12\n",
        }
        run.side_effect=lambda args,**kw: subprocess.CompletedProcess(args,0,stdout=outputs[args[-2]],stderr="")
        rows=p.message_metadata("x.grib2")
        self.assertEqual(rows[0]["units"],"K")
        self.assertEqual(rows[0]["typeOfLevel"],"heightAboveGround")
        self.assertEqual(rows[0]["startStep"],12)
        self.assertEqual(rows[0]["endStep"],12)

    @patch("icon_parameter_probe.assert_grib_valid_time")
    @patch("icon_parameter_probe.subprocess.run")
    def test_exact_valid_time_selection_is_enforced(self, run, identity):
        def fake(args,**kw):
            Path(args[-1]).write_bytes(b"GRIB")
            return subprocess.CompletedProcess(args,0,stdout="",stderr="")
        run.side_effect=fake
        base=datetime(2026,9,26,0,tzinfo=UTC)
        valid=datetime(2026,9,26,12,tzinfo=UTC)
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/"in.grib2";src.write_bytes(b"x")
            dst=Path(td)/"out.grib2"
            p.select_exact_message(src,dst,base,valid)
        where=run.call_args.args[0][2]
        self.assertIn("dataDate=20260926",where)
        self.assertIn("dataTime=0",where)
        self.assertIn("validityDate=20260926",where)
        self.assertIn("validityTime=1200",where)
        identity.assert_called_once()

    def test_parameter_inventory_matches_phase2_icon_scope(self):
        self.assertEqual(set(p.PARAMETERS),{
            "t_2m","td_2m","relhum_2m","pmsl","ps","tot_prec","clct",
            "aswdir_s","aswdifd_s","cape_ml","cin_ml"})
        self.assertEqual(len(p.PARAMETERS),11)
        self.assertTrue(all(x in p.CANONICAL for x in p.PARAMETERS))


if __name__=="__main__":
    unittest.main()
