import unittest

import availability_contract as a


class AvailabilityContractTests(unittest.TestCase):
    def test_all_explicit_states_are_accepted_and_missing_never_becomes_zero(self):
        observed="2026-09-26T12:00:00+00:00"
        for state in sorted(a.STATES):
            item={"value":1.5} if state=="received" else {"value":None,"availability_status":state}
            got=a.normalize_field_item(item,observed)
            self.assertEqual(got["availability_status"],state)
            self.assertEqual(got["availability_observed_at_utc"],observed)
            if state=="received":
                self.assertEqual(got["field_available_at_utc"],observed)
                self.assertEqual(got["value"],1.5)
            else:
                self.assertIsNone(got["value"])
                self.assertNotIn("field_available_at_utc",got)

    def test_non_received_field_with_value_fails_closed(self):
        with self.assertRaisesRegex(ValueError,"non-received field cannot carry a value"):
            a.normalize_field_item({
                "value":0.0,
                "availability_status":"fetch_error",
            },"2026-09-26T12:00:00+00:00")

    def test_row_level_retrieval_and_received_field_time_are_stamped(self):
        rows=[{
            "model":"GFS",
            "run_time_utc":"2026-09-26T00:00:00+00:00",
            "valid_time_utc":"2026-09-26T12:00:00+00:00",
            "forecast_lead_hours":12,
            "values":{"2t":[{"value":280.0}]},
        }]
        stamp="2026-09-26T05:15:00+00:00"
        a.stamp_rows(rows,observed_at=stamp,replace_row_time=True)
        self.assertEqual(rows[0]["retrieved_at_utc"],stamp)
        item=rows[0]["values"]["2t"][0]
        self.assertEqual(item["availability_status"],"received")
        self.assertEqual(item["field_available_at_utc"],stamp)
        self.assertEqual(item["availability_observed_at_utc"],stamp)

    def test_later_revision_does_not_move_earlier_field_availability(self):
        rows=[{
            "retrieved_at_utc":"2026-09-26T05:00:00+00:00",
            "values":{
                "wind":[{"value":5.0,"availability_status":"received","field_available_at_utc":"2026-09-26T05:00:00+00:00"}],
                "cape":[{"value":700.0,"availability_status":"received","field_available_at_utc":"2026-09-26T05:20:00+00:00"}],
            },
        }]
        a.stamp_rows(rows,observed_at="2026-09-26T05:20:00+00:00",replace_row_time=True)
        self.assertEqual(rows[0]["retrieved_at_utc"],"2026-09-26T05:20:00+00:00")
        self.assertEqual(rows[0]["values"]["wind"][0]["field_available_at_utc"],"2026-09-26T05:00:00+00:00")
        self.assertEqual(rows[0]["values"]["cape"][0]["field_available_at_utc"],"2026-09-26T05:20:00+00:00")

    def test_snapshot_validator_requires_closed_contract(self):
        payload={"models":{"GFS":[{
            "retrieved_at_utc":"2026-09-26T05:00:00+00:00",
            "values":{"2t":[{
                "value":280.0,
                "availability_status":"received",
                "availability_observed_at_utc":"2026-09-26T05:00:00+00:00",
                "field_available_at_utc":"2026-09-26T05:00:00+00:00",
            }]},
        }]}}
        self.assertTrue(a.validate_snapshot(payload))
        payload["models"]["GFS"][0]["values"]["2t"][0]["availability_status"]="mystery"
        with self.assertRaisesRegex(ValueError,"invalid status"):
            a.validate_snapshot(payload)


if __name__=="__main__":
    unittest.main()
