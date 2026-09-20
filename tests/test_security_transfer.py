import unittest

from fetch_svg_weatherlink import redact_sensitive
from push_private import parse_time,timestamp_not_older,ref_retry_delay,REF_UPDATE_MAX_ATTEMPTS

class SecurityAndTransferTests(unittest.TestCase):
    def test_weatherlink_exception_redacts_raw_and_urlencoded_key(self):
        key="abc+DEF/123="
        text="GET https://api.weatherlink.com/v2/current/42374?api-key=abc%2BDEF%2F123%3D failed"
        cleaned=redact_sensitive(text,key)
        self.assertNotIn("abc%2BDEF%2F123%3D",cleaned)
        self.assertNotIn(key,cleaned)
        self.assertIn("***REDACTED***",cleaned)

    def test_missing_transfer_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_time(None)

    def test_naive_transfer_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_time("2026-09-20T10:00:00")

    def test_ref_race_retry_budget_handles_longer_contention(self):
        self.assertEqual(REF_UPDATE_MAX_ATTEMPTS,8)
        self.assertEqual([ref_retry_delay(i) for i in range(8)],[1,2,4,8,12,16,20,30])
        self.assertGreaterEqual(sum(ref_retry_delay(i) for i in range(7)),60)

    def test_monotonic_pointer_rejects_older_writer(self):
        self.assertFalse(timestamp_not_older(
            "2026-09-20T10:05:00+00:00","2026-09-20T10:04:59+00:00"))
        self.assertTrue(timestamp_not_older(
            "2026-09-20T10:05:00+00:00","2026-09-20T10:05:00+00:00"))
        self.assertTrue(timestamp_not_older(
            "2026-09-20T10:05:00+00:00","2026-09-20T10:06:00+00:00"))

if __name__=="__main__":
    unittest.main()
