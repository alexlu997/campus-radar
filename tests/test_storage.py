from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from campus_radar.storage import Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "db.sqlite3"
        self.store = Store(self.path)
        self.store.sync_sources([{"id": "a", "name": "公开来源", "category": "学术科研"}])
        self.doc = {"url": "https://example.edu/1", "title": "同名系列讲座", "body": "时间待确认",
                    "published_at": None, "processing_status": "pending_review", "evidence": {},
                    "event": {"title": "同名系列讲座", "starts_at": None, "status": "unknown"}}

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_idempotent_preserves_first_seen(self):
        self.assertEqual(self.store.save_document("a", self.doc), "inserted")
        before = self.store.listing()["documents"][0]
        self.assertEqual(self.store.save_document("a", self.doc), "unchanged")
        after = self.store.listing()["documents"][0]
        self.assertEqual(before["first_seen_at"], after["first_seen_at"])
        self.assertEqual(after["history"], [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_changed_content_retains_original_evidence(self):
        self.store.save_document("a", self.doc)
        changed = deepcopy(self.doc)
        changed["body"] = "时间：2026年11月9日"
        changed["event"]["starts_at"] = "2026-11-09"
        self.assertEqual(self.store.save_document("a", changed), "updated")
        record = self.store.listing()["documents"][0]
        self.assertEqual(record["history"][0]["body"], "时间待确认")
        self.assertIsNone(record["history"][0]["event"]["starts_at"])
        self.assertEqual(record["event"]["starts_at"], "2026-11-09")

    def test_same_title_different_url_is_not_merged(self):
        self.store.save_document("a", self.doc)
        self.store.save_document("a", {**self.doc, "url": "https://example.edu/2"})
        self.assertEqual(len(self.store.listing()["documents"]), 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)

    def test_private_content_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save_document("a", {**self.doc, "visibility": "authenticated"})
        self.assertEqual(self.store.listing()["documents"], [])

    def test_literal_search_and_source_filter(self):
        self.store.save_document("a", self.doc)
        self.assertEqual(len(self.store.listing(query="系列", source_id="a")["documents"]), 1)
        self.assertEqual(self.store.listing(query="%'")["documents"], [])
        self.assertEqual(self.store.listing(source_id="missing")["documents"], [])

    def test_secondary_source_category_is_searchable(self):
        self.store.sync_sources([{"id": "a", "name": "公开来源", "category": "学术科研",
                                  "categories": ["学术科研", "创新创业"]}])
        self.store.save_document("a", self.doc)
        self.assertEqual(len(self.store.listing(category="创新创业")["documents"]), 1)
        self.assertEqual(self.store.listing(category="不存在")["documents"], [])

    def test_records_survive_reopen(self):
        self.store.save_document("a", self.doc)
        with Store(self.path) as reopened:
            self.assertEqual(reopened.listing()["documents"][0]["title"], "同名系列讲座")

    def test_uncollected_source_retains_audit_error(self):
        self.store.sync_sources([{"id": "a", "name": "公开来源", "last_error": "Entry check timed out"}])
        self.assertEqual(self.store.listing()["sources"][0]["last_error"], "Entry check timed out")

    def test_cli_json_and_markdown_without_api_environment(self):
        self.store.save_document("a", self.doc)
        sources = Path(self.temp.name) / "sources.json"
        sources.write_text(json.dumps({"sources": [{"id": "a", "name": "公开来源", "category": "学术科研"}]}))
        command = [sys.executable, "-m", "campus_radar", "--db", str(self.path), "--sources", str(sources)]
        environment = {key: value for key, value in os.environ.items()
                       if not any(marker in key.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
        root = Path(__file__).resolve().parent.parent
        listing = subprocess.run(command + ["list", "--format", "json", "--query", "系列"],
                                 cwd=root, env=environment, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(listing.stdout)["documents"][0]["title"], "同名系列讲座")
        digest = subprocess.run(command + ["digest", "--format", "markdown"], cwd=root,
                                env=environment, text=True, capture_output=True, check=True)
        self.assertIn("https://example.edu/1", digest.stdout)
        self.assertIn("活动时间：未知", digest.stdout)

    def test_markdown_preserves_date_range_and_partial_audience(self):
        from campus_radar.__main__ import markdown
        self.doc["event"].update(starts_at="2027-04-16", ends_at="2027-04-18", audience="老师")
        self.store.save_document("a", self.doc)
        content = markdown(self.store.listing())
        self.assertIn("2027-04-16 — 2027-04-18", content)
        self.assertIn("原文受众：老师；完整参加资格：未知", content)


if __name__ == "__main__":
    unittest.main()
