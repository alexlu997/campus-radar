import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campus_radar.collector import CollectionError
from campus_radar.service import dispatch
from campus_radar.storage import Store, encode, utc_now
from campus_radar.updates import enqueue_update, run_update, update_lock, update_status


ROOT = Path(__file__).resolve().parent.parent


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.sources = [s for s in json.loads((ROOT / "data/sources.json").read_text())["sources"] if s.get("enabled")][:2]
        self.store.sync_sources(self.sources)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_repeated_trigger_returns_existing_batch(self):
        with patch("campus_radar.updates.subprocess.Popen") as launch:
            launch.return_value.pid = os.getpid()
            first = enqueue_update(self.store, ROOT / "data/sources.json")
            second = enqueue_update(self.store, ROOT / "data/sources.json")
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["duplicate_trigger"])
        self.assertEqual(launch.call_count, 1)
        with update_lock(self.store):
            with patch("campus_radar.updates.subprocess.Popen") as launch:
                third = enqueue_update(self.store, ROOT / "data/sources.json")
                self.assertTrue(third["duplicate_trigger"])
                launch.assert_not_called()

    def test_dead_worker_is_reported_as_interrupted_and_can_be_restarted(self):
        self.store.db.execute("INSERT INTO updates VALUES('old',?,NULL,'running',99999999,?)", (utc_now(), "{}"))
        self.store.db.commit()
        self.assertEqual(update_status(self.store)["status"], "interrupted")
        with patch("campus_radar.updates.subprocess.Popen") as launch:
            launch.return_value.pid = os.getpid()
            self.assertEqual(enqueue_update(self.store, ROOT / "data/sources.json")["status"], "queued")

    def test_single_source_failure_keeps_old_favorite_and_other_source_updates(self):
        good, bad = self.sources
        old = {"url": "https://example.edu/old", "title": "历史保留讲座", "body": "之前已成功采集的正文",
               "processing_status": "parsed", "event": {"title": "历史保留讲座", "starts_at": None}}
        self.store.save_document(bad["id"], old)
        event_id = self.store.event_listing()["events"][0]["id"]
        self.store.set_favorite(event_id, True)
        fixtures = ROOT / "tests/fixtures/public"
        class Fetcher:
            cache_hits = 0
            network_events = []
            def get(self, url, allowed):
                if bad["allowed_hosts"][0] in url:
                    raise CollectionError("Injected source failure")
                name = "news-list.html" if url == good["url"] else "news-detail.html"
                return (fixtures / name).read_bytes(), url
        data = {"source_ids": [good["id"], bad["id"]], "limit": 1, "max_pages": 1,
                "results": [], "completed_sources": 0, "total_sources": 2}
        self.store.db.execute("INSERT INTO updates VALUES('test',?,NULL,'queued',?,?)", (utc_now(), os.getpid(), encode(data)))
        self.store.db.commit()
        result = run_update(self.store, "test", fetcher=Fetcher())
        self.assertEqual(result["status"], "partial")
        self.assertEqual({r["source_id"]: r["status"] for r in result["results"]}, {good["id"]: "success", bad["id"]: "failed"})
        self.assertEqual(result["completed_sources"], 2)
        self.assertEqual(len(self.store.listing()["documents"]), 2)
        self.assertEqual(self.store.event_listing(favorites=True)["events"][0]["id"], event_id)
        self.assertEqual(run_update(self.store, "test", fetcher=Fetcher())["id"], "test")
        self.assertEqual(len(self.store.listing()["runs"]), 2)

    def test_service_rejects_paths_commands_and_wrong_types(self):
        for method, payload in [("events", {"db": "/private"}), ("exec", {}),
                                ("events", {"limit": True}), ("favorite", {"event_id": "x", "enabled": "false"}),
                                ("update", {"source_ids": "a"})]:
            with self.assertRaises(ValueError):
                dispatch(self.store, ROOT / "data/sources.json", method, payload)
        self.assertEqual(dispatch(self.store, ROOT / "data/sources.json", "events", {})["total"], 0)


if __name__ == "__main__":
    unittest.main()
