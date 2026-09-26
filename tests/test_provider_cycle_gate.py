import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import provider_cycle_gate as gate


RUN=datetime(2026,9,26,12,tzinfo=timezone.utc)


def row(model,run=RUN):
    return {
        "model":model,
        "run_time_utc":run.isoformat(),
        "forecast_lead_hours":0,
        "valid_time_utc":run.isoformat(),
        "derived":{"wind_speed_ms":5.0,"gust_ms":7.0},
    }


class ProviderCycleGateTests(unittest.TestCase):
    def seed(self):
        latest={
            "generated_at_utc":"2026-09-26T15:00:00+00:00",
            "sources":{
                model:{
                    "selected_run_time_utc":RUN.isoformat(),
                    "provider_cycle_complete":True,
                } for model in gate.MODELS
            },
        }
        payload={
            "schema_version":2,
            "retrieved_at_utc":"2026-09-26T15:00:00+00:00",
            "spot":{"lat":52.4942,"lon":9.3418},
            "mode":"production",
            "models":{model:[row(model)] for model in gate.MODELS},
            "quality":{"errors":[]},
            "provider_attempts":[{"model":"GFS","status":"success"}],
        }
        return latest,payload

    def test_exact_cycle_evidence_path_is_stable(self):
        self.assertEqual(
            gate.evidence_path("GEFS-control",RUN),
            "data/weather_archive/cycle_evidence/gefs-control/year=2026/month=09/day=26/run=20260926T120000Z.json",
        )

    def test_all_archived_cycles_predict_zero_delta(self):
        latest,payload=self.seed()
        with patch.object(gate,"load_seed",return_value=(latest,payload,"seed.json.gz","a"*64)), \
             patch.object(gate,"exact_archived_cycle",return_value={"model":"ok"}):
            plan,seed=gate.build_plan("owner/private","token",True,discover_fn=lambda model,full: RUN)
        self.assertFalse(plan["any_work"])
        self.assertEqual(plan["delta_prediction"],"zero")
        self.assertTrue(plan["no_op_transfer_suppressed"])
        self.assertTrue(all(x["action"]=="carry_forward" for x in plan["models"].values()))
        self.assertIs(seed,payload)

    def test_one_new_provider_keeps_only_that_provider_fetchable(self):
        latest,payload=self.seed()
        newer=RUN.replace(hour=18)
        def discover(model,full):
            return newer if model=="GFS" else RUN
        def archived(repo,token,model,run):
            return None if model=="GFS" else {"model":model,"run_time_utc":run.isoformat()}
        with patch.object(gate,"load_seed",return_value=(latest,payload,"seed.json.gz","a"*64)), \
             patch.object(gate,"exact_archived_cycle",side_effect=archived):
            plan,_=gate.build_plan("owner/private","token",True,discover_fn=discover)
        self.assertTrue(plan["any_work"])
        self.assertEqual(plan["delta_prediction"],"nonzero")
        self.assertEqual(plan["models"]["GFS"]["action"],"fetch")
        self.assertTrue(all(
            entry["action"]=="carry_forward"
            for model,entry in plan["models"].items() if model!="GFS"
        ))

    def test_missing_private_evidence_fails_open_to_fetch(self):
        latest,payload=self.seed()
        with patch.object(gate,"load_seed",return_value=(latest,payload,"seed.json.gz","a"*64)), \
             patch.object(gate,"exact_archived_cycle",return_value=None):
            plan,_=gate.build_plan("owner/private","token",True,discover_fn=lambda model,full: RUN)
        self.assertTrue(plan["any_work"])
        self.assertTrue(all(x["action"]=="fetch" for x in plan["models"].values()))

    def test_gefs_supplemental_retry_is_due_only_after_not_before(self):
        payload={
            "full_horizon_archive":{
                "sources":{
                    "GEFS-control":{
                        "run_time_utc":RUN.isoformat(),
                        "gefs_pgrb2b_supplemental_retry":{
                            "status":"pending",
                            "attempts":0,
                            "maximum_attempts":1,
                            "next_retry_not_before_utc":(RUN+timedelta(hours=3)).isoformat(),
                        },
                    }
                }
            }
        }
        due,_=gate.gefs_supplemental_due(payload,RUN,RUN+timedelta(hours=2,minutes=59))
        self.assertFalse(due)
        due,state=gate.gefs_supplemental_due(payload,RUN,RUN+timedelta(hours=3))
        self.assertTrue(due)
        self.assertEqual(state["attempts"],0)
        payload["full_horizon_archive"]["sources"]["GEFS-control"]["gefs_pgrb2b_supplemental_retry"]["attempts"]=1
        due,_=gate.gefs_supplemental_due(payload,RUN,RUN+timedelta(hours=6))
        self.assertFalse(due)

    def test_prepare_seed_resets_attempts_and_persists_gate(self):
        _latest,payload=self.seed()
        plan={"checked_at_utc":"2026-09-26T18:00:00+00:00","any_work":True,"models":{}}
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"model.json"
            gate.prepare_seed(copy.deepcopy(payload),plan,path)
            import json
            saved=json.loads(path.read_text())
        self.assertEqual(saved["provider_attempts"],[])
        self.assertEqual(saved["provider_cycle_gate"],plan)
        self.assertEqual(saved["retrieved_at_utc"],plan["checked_at_utc"])


if __name__=="__main__":
    unittest.main()
