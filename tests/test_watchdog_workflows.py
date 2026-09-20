import unittest
from pathlib import Path

from check_collection_due import state_pointer


class WatchdogWorkflowTests(unittest.TestCase):
    def text(self,name):
        return Path(".github/workflows",name).read_text(encoding="utf-8")

    def test_secondary_due_state_uses_atomic_batch_receipt(self):
        path,field=state_pointer("secondary")
        self.assertEqual(path,"data/inbox/public_collector/transfer_receipts/secondary/latest.json")
        self.assertEqual(field,"source_generated_at_utc")

    def test_single_watchdog_dispatches_existing_collectors(self):
        s=self.text("collector-watchdog.yml")
        self.assertIn("workflow_dispatch:",s)
        self.assertIn("actions: write",s)
        for wf in ("collect-svg.yml","collect-models.yml","collect-secondary.yml"):
            self.assertIn(wf,s)
        self.assertIn("watchdog=true",s)
        self.assertNotIn("provider_fetch.py",s)
        self.assertNotIn("fetch_svg_weatherlink.py",s)
        self.assertNotIn("fetch_etnw_metar.py",s)

    def test_failed_optional_skm_is_not_persisted_privately(self):
        s=self.text("collect-svg.yml")
        self.assertIn("id: skm_transfer_gate",s)
        self.assertIn("bundle_ready_for_private_revalidation",s)
        self.assertIn("steps.skm_transfer_gate.outputs.ready == 'true'",s)

    def test_svg_watchdog_is_freshness_gated(self):
        s=self.text("collect-svg.yml")
        self.assertIn("watchdog:",s)
        self.assertIn("inputs.watchdog == true",s)
        self.assertIn("--kind svg --max-age-minutes 40",s)
        self.assertIn("inputs.watchdog != true",s)

    def test_model_watchdog_is_freshness_gated(self):
        s=self.text("collect-models.yml")
        self.assertIn("watchdog:",s)
        self.assertIn("inputs.watchdog == true",s)
        self.assertIn("--kind models --max-age-minutes 150",s)
        self.assertIn("inputs.watchdog != true",s)

    def test_secondary_watchdog_is_freshness_gated(self):
        s=self.text("collect-secondary.yml")
        self.assertIn("watchdog:",s)
        self.assertIn("inputs.watchdog == true",s)
        self.assertIn("--kind secondary --max-age-minutes 240",s)
        self.assertIn("inputs.watchdog != true",s)

    def test_manual_individual_dispatch_remains_force_capable(self):
        for wf in ("collect-svg.yml","collect-models.yml","collect-secondary.yml"):
            s=self.text(wf)
            self.assertIn("default: false",s)
            self.assertIn("github.event_name == 'workflow_dispatch' && inputs.watchdog != true",s)


if __name__=="__main__":
    unittest.main()
