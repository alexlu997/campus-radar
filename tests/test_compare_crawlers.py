"""Comparison reporting tests use synthetic HTML only; they are not live evidence."""

import json
from pathlib import Path
import tempfile
import unittest

from tools.compare_crawlers import (
    ROOT, SOURCE_IDS, assess_html, cases_from_registry, common_policy_accepted, concise_error,
    public_html_inputs,
)


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.case = {"id": "fixture-detail", "kind": "detail", "url": "https://me.sjtu.edu.cn/tsfx-xsbg/1.html",
                     "source": {"adapter": "sjtu_me", "allowed_hosts": ["me.sjtu.edu.cn"], "category": "学术科研"}}

    def test_login_html_is_not_a_parser_success(self):
        result = assess_html(b'<title>jAccount</title><input type="password">', self.case["url"], self.case)
        self.assertEqual(result["parser_status"], "failed")
        self.assertIn("Login", result["parser_error"])

    def test_transport_success_does_not_establish_common_policy_acceptance(self):
        self.assertFalse(common_policy_accepted(True, "unknown"))
        self.assertFalse(common_policy_accepted(True, "disallowed"))
        self.assertTrue(common_policy_accepted(True, "allowed"))
        self.assertFalse(common_policy_accepted(False, "allowed"))

    def test_same_parser_preserves_unknown_fields(self):
        raw = '<html><h1>离线合成学术讲座</h1><div class="Article_content"><p>时间：11月9日 14:00</p><p>地点：闵行校区 A 楼</p><p>这是离线合成样本，仅用来核查字段处理，不代表真实校园活动。</p></div></html>'.encode()
        result = assess_html(raw, self.case["url"], self.case)
        self.assertEqual(result["parser_status"], "parsed")
        self.assertIsNone(result["event"]["starts_at"])
        self.assertIsNone(result["event"]["eligibility"])
        self.assertEqual(result["event"]["location"], "闵行校区 A 楼")

    def test_cross_host_redirect_is_rejected(self):
        result = assess_html(b"irrelevant", "https://example.org/", self.case)
        self.assertEqual(result["parser_status"], "failed")
        self.assertIn("verified source hosts", result["parser_error"])

    def test_inputs_are_six_explicit_public_urls(self):
        records = [{"id": source_id, "visibility": "public", "url": f"https://example.edu/{index}",
                    "allowed_hosts": ["example.edu"], "latest_sample": {"url": f"https://example.edu/detail/{index}"}}
                   for index, source_id in enumerate(SOURCE_IDS)]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sources.json"
            path.write_text(json.dumps({"sources": records}))
            cases = cases_from_registry(path)
            self.assertEqual(len(cases), 6)
            self.assertEqual([case["kind"] for case in cases], ["list", "detail"] * 3)
            records[0]["visibility"] = "authenticated"
            path.write_text(json.dumps({"sources": records}))
            with self.assertRaises(ValueError):
                cases_from_registry(path)

    def test_error_output_is_bounded_and_removes_console_codes(self):
        self.assertEqual(concise_error("\x1b[31mfailed\n now\x1b[0m"), "failed now")
        self.assertEqual(len(concise_error("x" * 5000)), 1200)

    def test_raw_input_requires_successful_tls_verified_acquisition(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "outputs") as folder:
            html_path = Path(folder) / "synthetic.html"
            html_path.write_text("<html>offline test fixture only</html>")
            manifest = Path(folder) / "manifest.json"
            data = {"items": [{"url": self.case["url"], "html_path": str(html_path),
                               "metrics": {"http_code": "200", "ssl_verify_result": "0"}}]}
            manifest.write_text(json.dumps(data))
            result = public_html_inputs(manifest, [self.case])
            self.assertEqual(result[self.case["url"]]["raw"], html_path.read_bytes())
            data["items"][0]["metrics"]["ssl_verify_result"] = "20"
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "TLS-verified"):
                public_html_inputs(manifest, [self.case])


if __name__ == "__main__":
    unittest.main()
