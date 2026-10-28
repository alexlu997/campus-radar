import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campus_radar import auth


class FakePage:
    url = "https://dekt.sjtu.edu.cn/h5/?token=secret#code=secret"

    def __init__(self, counts=None):
        self.counts = counts or {"links": 4, "images": 2, "list_items": 3}
        self.evaluated = False

    def evaluate(self, script):
        self.evaluated = True
        assert "innerText" not in script and "textContent" not in script
        return self.counts


class AuthPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.root = Path(self.tmp.name) / "auth"

    def tearDown(self):
        self.tmp.cleanup()

    def test_private_managed_profile_can_be_cleared(self):
        target = auth.prepare_profile("dekt", self.root)
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertEqual((target / auth.MARKER).stat().st_mode & 0o777, 0o600)
        (target / "private-state").write_text("must not be exported")
        auth.prepare_profile("dekt", self.root)
        self.assertTrue(auth.clear_profile("dekt", self.root))
        self.assertFalse(target.exists())
        self.assertFalse(auth.clear_profile("dekt", self.root))

    def test_unknown_source_cannot_escape_profile_root(self):
        with self.assertRaises(ValueError):
            auth.prepare_profile("../../Chrome", self.root)

    def test_unmarked_directory_is_never_used_or_removed(self):
        target = self.root / "dekt"
        target.mkdir(parents=True)
        (target / "keep").write_text("user file")
        for action in (auth.prepare_profile, auth.clear_profile):
            with self.assertRaises(ValueError):
                action("dekt", self.root)
        self.assertTrue((target / "keep").exists())

    def test_profile_symlink_is_rejected(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        self.root.mkdir()
        (self.root / "dekt").symlink_to(other, target_is_directory=True)
        for action in (auth.prepare_profile, auth.clear_profile):
            with self.assertRaises(ValueError):
                action("dekt", self.root)

    def test_browser_lock_prevents_clear(self):
        target = auth.prepare_profile("dekt", self.root)
        (target / "SingletonLock").symlink_to("process-12345")
        with self.assertRaises(ValueError):
            auth.clear_profile("dekt", self.root)
        self.assertTrue(target.is_dir())

    def test_marker_must_match_source(self):
        target = auth.prepare_profile("dekt", self.root)
        (target / auth.MARKER).write_text("campus-radar-auth-v1:activity\n")
        with self.assertRaises(ValueError):
            auth.clear_profile("dekt", self.root)

    def test_report_does_not_include_url_or_page_content(self):
        page = FakePage({"links": 4, "images": "SECRET", "list_items": -5,
                         "table_rows": 10**9, "password_inputs": 0, "title": "SECRET"})
        result = auth.inspect_structure("dekt", page, "list")
        text = json.dumps(result)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("token", text)
        self.assertNotIn("http", text)
        self.assertEqual(result["counts"]["images"], 0)
        self.assertEqual(result["counts"]["list_items"], 0)
        self.assertEqual(result["counts"]["table_rows"], 1_000_000)
        self.assertEqual(result["status"], "structure_observed")
        self.assertEqual(result["login_state"], "unverified")
        self.assertTrue(all(value == "not_verified" for value in result["fields"].values()))

    def test_authentication_and_external_pages_are_not_inspected(self):
        for url, status in [
            ("https://jaccount.sjtu.edu.cn/jaccount/login?code=SECRET", "authentication_required"),
            ("https://mail.sjtu.edu.cn/", "unexpected_page"),
            ("https://dekt.sjtu.edu.cn.evil.test/", "unexpected_page"),
            ("https://name:SECRET@dekt.sjtu.edu.cn/", "unexpected_page"),
            ("http://dekt.sjtu.edu.cn/", "unexpected_page"),
            ("https://dekt.sjtu.edu.cn:8443/", "unexpected_page"),
        ]:
            page = FakePage()
            page.url = url
            result = auth.inspect_structure("dekt", page, "list")
            self.assertEqual(result["status"], status)
            self.assertFalse(page.evaluated)

    def test_password_form_does_not_count_as_activity_page(self):
        result = auth.inspect_structure("dekt", FakePage({"password_inputs": 1}), "list")
        self.assertEqual(result["status"], "authentication_required")
        self.assertNotIn("counts", result)

    def test_noninteractive_invocation_does_not_launch_browser(self):
        output = io.StringIO()
        with patch.object(auth.os, "isatty", return_value=False), \
             patch.object(auth, "browser_session") as browser, contextlib.redirect_stdout(output):
            self.assertEqual(auth.main(["probe", "--source", "dekt"]), 1)
        browser.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["status"], "local_interactive_terminal_required")

    def test_exception_details_are_not_printed(self):
        output = io.StringIO()
        with patch.object(auth.os, "isatty", return_value=True), \
             patch.object(auth, "browser_session", side_effect=RuntimeError("Cookie: SECRET")), \
             contextlib.redirect_stdout(output):
            self.assertEqual(auth.main(["login", "--source", "dekt"]), 1)
        self.assertNotIn("SECRET", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["status"], "failed_no_sensitive_diagnostics")


if __name__ == "__main__":
    unittest.main()
