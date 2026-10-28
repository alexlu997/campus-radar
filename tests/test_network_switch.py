import unittest

from tools.check_network_switch import summarize


class SwitchEvidenceTests(unittest.TestCase):
    def row(self, before, after, source="s", status="success"):
        return {"before": {"mode": before}, "after": {"mode": after},
                "result": {"source_id": source, "status": status}}

    def test_stable_on_off_and_return_are_required_in_one_session(self):
        rows = [self.row("on", "on"), self.row("off", "off"), self.row("on", "on")]
        self.assertTrue(summarize(rows, ["s"])["passed"])
        self.assertFalse(summarize(rows[:1], ["s"])["passed"])
        self.assertFalse(summarize(rows, ["s", "missing"])["passed"])
        self.assertFalse(summarize(rows + [self.row("on", "on", status="failed")], ["s"])["passed"])

    def test_transition_and_unknown_are_not_stable_off_evidence(self):
        rows = [self.row("on", "off"), self.row("mixed_or_unknown", "mixed_or_unknown"), self.row("on", "on")]
        report = summarize(rows, ["s"])
        self.assertTrue(report["on_off_on_observed"])
        self.assertEqual(report["stable_state_sources"]["off"], [])
        self.assertFalse(report["passed"])

    def test_all_sources_must_succeed_after_reopening(self):
        rows = [self.row(mode, mode, source) for mode in ("on", "off") for source in ("a", "b")]
        rows.append(self.row("on", "on", "a"))
        self.assertFalse(summarize(rows, ["a", "b"])["passed"])
        rows.append(self.row("on", "on", "b"))
        self.assertTrue(summarize(rows, ["a", "b"])["passed"])


if __name__ == "__main__":
    unittest.main()
