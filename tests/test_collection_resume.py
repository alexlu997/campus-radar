from datetime import datetime, timedelta
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from campus_radar.collector import CollectionError, collect_source
from campus_radar.storage import Store


SOURCE = {"id": "me", "name": "公开测试栏目", "url": "https://me.sjtu.edu.cn/tsfx-xsbg.html",
          "adapter": "sjtu_me", "allowed_hosts": ["me.sjtu.edu.cn"], "enabled": True, "visibility": "public"}


class Site:
    cache_hits = 0

    def __init__(self, count=35):
        self.ids = list(range(1000, 1000 + count))
        self.failures, self.changes, self.calls = set(), {}, []
        self.interrupt_at = None

    def get(self, url, hosts):
        self.calls.append(url)
        match = re.search(r"/tsfx-xsbg/(\d+)\.html$", url)
        if match:
            number = int(match[1])
            if number == self.interrupt_at:
                raise RuntimeError("simulated worker exit")
            if number in self.failures:
                raise CollectionError("HTTP 404; no page accepted")
            title, when, place = self.changes.get(number, (f"独立学术活动编号{number}学术报告", "2099年11月9日 14:00-15:00", "闵行校区 A 楼"))
            body = f'<html><h1>{title}</h1><article><p>时间：{when}</p><p>地点：{place}</p><p>此处为足够完整的公开报告正文，介绍科研工作及面向同学的学术交流。</p></article></html>'
        else:
            match = re.search(r"tsfx-xsbg_(\d+)\.html$", url)
            page = int(match[1]) if match else 1
            ids = self.ids[(page - 1) * 7:page * 7]
            if not ids:
                raise CollectionError("HTTP 404; no page accepted")
            # A pinned old article must not be mistaken for a complete overlap boundary.
            ids = list(dict.fromkeys(([1000] if page > 1 else []) + ids))
            links = ''.join(f'<a href="/tsfx-xsbg/{i}.html">独立学术活动编号{i}学术报告</a>' for i in ids)
            more = f'<a href="/tsfx-xsbg_{page + 1}.html">下一页</a>' if page * 7 < len(self.ids) else ''
            body = '<html><body>Academic public reports and information for all students' + links + more + '</body></html>'
        return body.encode(), url


class CollectionResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "db.sqlite3"
        self.store = Store(self.path)
        self.store.sync_sources([SOURCE])
        self.site = Site()
        self.now = datetime.fromisoformat("2026-10-28T00:00:00+00:00")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def collect(self, *, limit=12, max_pages=2):
        self.now += timedelta(minutes=1)
        with patch("campus_radar.collector.utc_now", return_value=self.now.isoformat()), patch(
                "campus_radar.storage.utc_now", return_value=self.now.isoformat()):
            result = collect_source(self.store, SOURCE, self.site, limit=limit, max_pages=max_pages)
        self.assertLessEqual(result["coverage"]["attempted_documents"], limit)
        self.assertLessEqual(len(result["coverage"]["attempted_list_pages"]), max_pages)
        return result

    def finish(self, *, max_pages=2):
        results = []
        for _ in range(20):
            result = self.collect(max_pages=max_pages)
            results.append(result)
            if not result["coverage"]["collection_pending"]:
                return results
        self.fail("bounded continuation did not finish")

    def test_35_documents_resume_across_pages_and_restarts_without_duplicates(self):
        first = self.collect()
        self.assertTrue(first["coverage"]["collection_pending"])
        self.assertFalse(first["coverage"]["historical_pagination_complete"])
        self.store.db.close()
        self.store = Store(self.path)
        rest = self.finish()
        self.assertEqual(sum(r["inserted"] for r in [first, *rest]), 35)
        self.assertEqual(self.store.listing(limit=0)["total"], 35)
        self.assertEqual(self.store.event_listing()["total"], 35)
        self.assertTrue(rest[-1]["coverage"]["historical_pagination_complete"])
        self.assertEqual(self.collect()["inserted"], 0)

    def test_one_page_budget_still_makes_progress(self):
        self.finish(max_pages=1)
        self.assertEqual(self.store.listing(limit=0)["total"], 35)

    def test_burst_of_new_posts_and_pinned_links_does_not_skip_middle_pages(self):
        self.collect()
        self.collect()
        self.site.ids = list(range(2000, 2021)) + self.site.ids
        self.finish()
        self.assertEqual(self.store.listing(limit=0)["total"], 56)
        self.assertEqual(self.store.event_listing()["total"], 56)

    def test_failed_article_remains_pending_and_recovers(self):
        self.site.failures.add(1000)
        first = self.collect()
        self.assertEqual(first["status"], "partial")
        self.assertTrue(first["coverage"]["collection_pending"])
        self.site.failures.clear()
        self.finish()
        self.assertEqual(self.store.listing(limit=0)["total"], 35)
        self.assertIsNone(self.store.source_states()[0]["last_error"])

    def test_checkpoint_survives_worker_exit_mid_article_batch(self):
        self.site.interrupt_at = 1001
        with self.assertRaisesRegex(RuntimeError, "worker exit"):
            self.collect()
        self.assertEqual(self.store.listing(limit=0)["total"], 1)
        self.store.db.close()
        self.store = Store(self.path)
        self.site.interrupt_at = None
        self.finish()
        self.assertEqual(self.store.listing(limit=0)["total"], 35)

    def test_active_article_outside_latest_pages_is_rechecked_and_state_survives(self):
        self.finish()
        target_url = "https://me.sjtu.edu.cn/tsfx-xsbg/1034.html"
        event = next(e for e in self.store.event_listing()["events"] if any(s["url"] == target_url for s in e["sources"]))
        self.store.set_favorite(event["id"], True)
        self.store.set_feedback(event["id"], "interested")
        self.site.ids.remove(1034)
        self.site.changes[1034] = (event["title"], "2099年11月10日 15:00-16:00", "徐汇校区报告厅")
        for _ in range(6):
            self.collect()
            detail = self.store.event_detail(event["id"])
            if detail["location"] == "徐汇校区报告厅":
                break
        self.assertEqual(detail["starts_at"], "2099-11-10T15:00:00+08:00")
        self.assertEqual(detail["location"], "徐汇校区报告厅")
        self.assertTrue(detail["favorite"])
        self.assertEqual(detail["feedback"], "interested")
        self.assertTrue(detail["changes"])
        self.site.failures.add(1034)
        for _ in range(6):
            result = self.collect()
            if any(error.get("url") == target_url for error in result["errors"]):
                break
        self.assertTrue(any(error.get("url") == target_url for error in result["errors"]))
        self.assertEqual(self.store.event_detail(event["id"])["location"], "徐汇校区报告厅")
        self.assertNotEqual(self.store.event_detail(event["id"])["status"], "cancelled")
        self.site.failures.clear()
        self.site.changes[1034] = ("取消：" + event["title"], "2099年11月10日 15:00-16:00", "徐汇校区报告厅")
        for _ in range(6):
            self.collect()
            if self.store.event_detail(event["id"])["status"] == "cancelled":
                break
        self.assertEqual(self.store.event_detail(event["id"])["status"], "cancelled")
        self.assertTrue(self.store.event_detail(event["id"])["favorite"])

    def test_unknown_old_articles_have_explicit_bounded_recheck_window(self):
        self.site = Site(1)
        with patch("campus_radar.storage.utc_now", return_value="2026-08-01T00:00:00+00:00"):
            self.store.save_document("me", {"url": "https://me.sjtu.edu.cn/tsfx-xsbg/1000.html", "title": "时间未知的历史学术报告",
                "body": "已有公开正文", "processing_status": "parsed", "event": {"title": "时间未知的历史学术报告", "starts_at": None}})
        result = self.collect()
        self.assertEqual(result["coverage"]["rechecked_documents"], 0)
        self.assertEqual(result["coverage"]["unknown_recheck_window_days"], 30)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(self.site.calls), 1)


if __name__ == "__main__":
    unittest.main()
