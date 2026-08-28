import json
import tempfile
import unittest
from pathlib import Path

from control_tower import REQUIRED_METRICS, evaluate, run
from replay_bundle import replay_n6, replay_n7, replay

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "golden_cases.json"


class ReplayBundleTests(unittest.TestCase):
    def test_metrics_separate_format_and_overreach(self):
        cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
        results = [evaluate(case) for case in cases]
        self.assertEqual([result.status for result in results], ["passed", "blocked", "blocked", "blocked"])
        for result in results:
            self.assertTrue(set(REQUIRED_METRICS) <= set(result.metrics))
        self.assertTrue(results[2].metrics["format_valid"] is False)
        self.assertTrue(results[3].metrics["format_valid"] is True)
        self.assertTrue(results[3].metrics["overreach_blocked"] is False)
        self.assertEqual(results[2].metrics["assertion_pass_rate"], 1.0)
        self.assertEqual(results[3].metrics["assertion_pass_rate"], 1.0)
        changed = dict(cases[3], expected=dict(cases[3]["expected"], overreach_blocked=True))
        failed = evaluate(changed)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.metrics["assertion_pass_rate"], 0.8)
        self.assertFalse(failed.metrics["overreach_blocked"])

    def test_blocked_and_failed_retain_trace_events(self):
        cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
        for case in cases[1:]:
            result = evaluate(case)
            self.assertTrue(result.trace_id)
            self.assertGreaterEqual(len(result.kanban_event_ids), 2)
        malformed = dict(cases[0], input=dict(cases[0]["input"], output={}))
        failed = evaluate(malformed)
        self.assertEqual(failed.status, "failed")
        self.assertGreaterEqual(len(failed.kanban_event_ids), 2)

    def test_n6_n7_replay_is_offline_and_hash_addressed(self):
        n6 = replay_n6(ROOT / "inputs/n6-daily-report.json")
        n7 = replay_n7(ROOT / "inputs/n7-meeting-minutes.v1.json", ROOT / "inputs/n7-manifest.json")
        self.assertGreater(n6["fact_count"], 0)
        self.assertEqual(n7["status_counts"], {"preview": 3, "needs_input": 2})
        with tempfile.TemporaryDirectory() as directory:
            report = replay(Path(directory) / "bundle-report.json")
            self.assertTrue(report["read_only"])
            self.assertFalse(report["network_allowed"])
            self.assertEqual(report["control_tower"]["summary"], {"cases": 4, "passed": 1, "failed": 0, "blocked": 3, "failures_retain_trace": True})


if __name__ == "__main__":
    unittest.main()
