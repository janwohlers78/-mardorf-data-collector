import subprocess
import unittest
from datetime import datetime,timezone
from unittest.mock import patch

from grib_identity import _step_end_hours,assert_grib_batch_leads,assert_grib_valid_time

UTC=timezone.utc

class GribIdentityTests(unittest.TestCase):
    def completed(self,stdout):
        return subprocess.CompletedProcess(["grib_get"],0,stdout=stdout,stderr="")

    def test_step_range_minute_units_are_converted_to_hours(self):
        self.assertEqual(_step_end_hours("0m-15m"),0.25)
        self.assertEqual(_step_end_hours("0m-90m"),1.5)
        self.assertEqual(_step_end_hours("0-3"),3.0)

    @patch("grib_identity.subprocess.run")
    def test_single_file_validity_matches_provider_metadata(self,mock_run):
        mock_run.return_value=self.completed(
            "10u 20260920 600 3 20260920 900\n"
            "10v 20260920 600 3 20260920 900\n"
            "10fg 20260920 600 3 20260920 900\n")
        run=datetime(2026,9,20,6,tzinfo=UTC)
        valid=datetime(2026,9,20,9,tzinfo=UTC)
        self.assertEqual(assert_grib_valid_time("x.grib2",run,valid,"test"),valid)

    @patch("grib_identity.subprocess.run")
    def test_wrong_provider_validity_is_rejected(self,mock_run):
        mock_run.return_value=self.completed("10u 20260920 600 3 20260920 1200\n")
        run=datetime(2026,9,20,6,tzinfo=UTC)
        valid=datetime(2026,9,20,9,tzinfo=UTC)
        with self.assertRaises(RuntimeError):
            assert_grib_valid_time("x.grib2",run,valid,"test")

    @patch("grib_identity.subprocess.run")
    def test_step_range_disagreement_is_rejected(self,mock_run):
        mock_run.return_value=self.completed("10u 20260920 600 0-6 20260920 900\n")
        run=datetime(2026,9,20,6,tzinfo=UTC)
        valid=datetime(2026,9,20,9,tzinfo=UTC)
        with self.assertRaises(RuntimeError):
            assert_grib_valid_time("x.grib2",run,valid,"test")

    @patch("grib_identity.subprocess.run")
    def test_batch_requires_every_requested_lead(self,mock_run):
        mock_run.return_value=self.completed(
            "10u 20260920 600 0 20260920 600\n"
            "10u 20260920 600 3 20260920 900\n")
        run=datetime(2026,9,20,6,tzinfo=UTC)
        with self.assertRaises(RuntimeError):
            assert_grib_batch_leads("batch.grib2",run,[0,3,6],"batch")

if __name__=="__main__":
    unittest.main()
