from pathlib import Path
import tempfile
import unittest

from campus_radar.collector import CollectionError
from campus_radar.discovery import add_candidate, fetch_candidate, search_public
from campus_radar.storage import Store


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "db.sqlite3")
        self.source = {"id": "ctld", "name": "教学发展中心", "adapter": "ctld", "visibility": "public",
                       "allowed_hosts": ["ctld.sjtu.edu.cn"], "url": "https://ctld.sjtu.edu.cn/events"}
        self.store.sync_sources([self.source])

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_candidate_not_counted_as_document_until_body_is_read(self):
        path = Path(__file__).parent / "fixtures/public"
        class Fake:
            def get(self, url, allowed):
                return (path / ("ctld-list.html" if "keyword=" in url else "ctld-detail.html")).read_bytes(), url
        result = search_public(self.store, "AI", fetcher=Fake())
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(self.store.listing()["documents"]), 0)
        candidate = result["candidates"][0]
        fetched = fetch_candidate(self.store, candidate["url"], fetcher=Fake())
        self.assertEqual(fetched["status"], "fetched")
        self.assertEqual(len(self.store.listing()["documents"]), 1)
        class Broken:
            def get(self, *args):
                raise CollectionError("network unavailable")
        failed = fetch_candidate(self.store, candidate["url"], fetcher=Broken())
        self.assertEqual(failed["status"], "refresh_failed")
        self.assertEqual(failed["document_id"], fetched["document_id"])
        self.assertEqual(len(self.store.listing()["documents"]), 1)

    def test_official_zero_results_is_distinct_from_broken_page(self):
        class Empty:
            def get(self, url, allowed):
                return b'''<html><body>Official activity search page with no matching entries<div class="event-list"></div><script>laypage.render({elem:'pages', count: 0, limit:12})</script></body></html>''', url
        result = search_public(self.store, "no match", fetcher=Empty())
        self.assertEqual((result["status"], result["result_count"]), ("success", 0))

    def test_unknown_site_and_login_page_never_enter_public_documents(self):
        for url in ["http://127.0.0.1/private", "file:///etc/passwd", "https://sjtu.edu.cn.evil.example/", "https://u:p@news.sjtu.edu.cn/x"]:
            with self.assertRaises(ValueError):
                add_candidate(self.store, url)
        class Verification:
            def get(self, url, allowed):
                return '<html><title>登录验证</title><body>需要验证，正文不可读，请登录后再访问此页面</body></html>'.encode(), url
        row = fetch_candidate(self.store, "https://mp.weixin.qq.com/s/sample", fetcher=Verification())
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["document_id"])
        self.assertEqual(len(self.store.listing()["documents"]), 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
