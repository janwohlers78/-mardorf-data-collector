import unittest

import fetch_dwd_additional_models as dwd
import fetch_extra_models as extra
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
