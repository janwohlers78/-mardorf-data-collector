import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "collect-models.yml"


class Phase1BWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()
        self.trigger_block = self.text.split("on:\n", 1)[1].split("\npermissions:", 1)[0]

    def test_model_collector_has_no_push_trigger(self):
        self.assertNotIn("\n  push:", self.trigger_block)
        self.assertIn("  schedule:", self.trigger_block)
        self.assertIn("  workflow_dispatch:", self.trigger_block)

    def test_full_validation_is_explicit_and_not_commit_message_driven(self):
        self.assertIn("full_validation:", self.trigger_block)
        self.assertIn("inputs.full_validation == true", self.text)
        self.assertNotIn("[full-model-validation]", self.text)
        self.assertNotIn("github.event.head_commit.message", self.text)

    def test_schedule_and_watchdog_keep_hard_full_validation(self):
        line = next(line for line in self.text.splitlines() if "FULL_VALIDATION:" in line)
        self.assertIn("github.event_name == 'schedule'", line)
        self.assertIn("inputs.watchdog == true", line)
        self.assertIn("inputs.test_mode != true", line)

    def test_test_mode_remains_non_transfer_smoke_path(self):
        self.assertIn('if [ "${{ inputs.test_mode }}" = "true" ]; then args+=(--test); fi', self.text)
        transfer = self.text.split("- name: Transfer payload and integrity history to private repository", 1)[1]
        transfer_if = next(line for line in transfer.splitlines() if line.strip().startswith("if:"))
        self.assertIn("inputs.test_mode != true", transfer_if)

    def test_tier_a_is_optional_but_runs_before_archive_and_transfer(self):
        self.assertIn("Attach ICON Tier-A weather context", self.text)
        self.assertIn("python src/collect_icon_tier_a.py --workers 4", self.text)
        tier=self.text.index("Attach ICON Tier-A weather context")
        archive=self.text.index("Archive remaining native model horizons")
        transfer=self.text.index("Transfer payload and integrity history")
        self.assertLess(tier,archive)
        self.assertLess(tier,transfer)
        block=self.text[tier:archive]
        self.assertIn("continue-on-error: true",block)



if __name__ == "__main__":
    unittest.main()
