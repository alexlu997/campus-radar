"""Offline fixtures exercise failure and evidence boundaries, not site compatibility."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from campus_radar.collector import (
    CollectionError, Fetcher, HostRedirect, collect_source, extract_event, normalize_url,
    page_tree, parse_date, parse_document, parse_listing, require_host,
)
from campus_radar.storage import Store


SOURCE = {"id": "me", "name": "Fixture public source", "url": "https://me.sjtu.edu.cn/tsfx-xsbg.html",
          "adapter": "sjtu_me", "allowed_hosts": ["me.sjtu.edu.cn"], "enabled": True,
          "category": "学术科研", "visibility": "public"}
URL = "https://me.sjtu.edu.cn/tsfx-xsbg/123.html"


def article(title="机器学习讲座", body=None):
    body = body or "<p>时间：2026年11月9日 14:00-15:00</p><p>地点：闵行校区 A 楼会议室</p><p>本活动介绍机器学习方法及科学研究中的应用。参加对象尚未在原文中说明。</p>"
    return f'<html><head><title>{title}</title></head><body><h1>{title}</h1><div class="Article_content">{body}</div></body></html>'.encode()


class ParserTests(unittest.TestCase):
    def test_login_is_not_an_empty_list(self):
        for raw in (b'<html><title>jAccount</title><body>login</body></html>',
                    b'<html><input type="password"></html>'):
            with self.assertRaisesRegex(CollectionError, "Login"):
                page_tree(raw)

    def test_shell_and_missing_selector_fail(self):
        with self.assertRaisesRegex(CollectionError, "shell"):
            page_tree(b'<html><div id="app"></div></html>')
        with self.assertRaisesRegex(CollectionError, "selector"):
            parse_document(b'<html><h1>Example</h1><div>' + b'ordinary navigation ' * 10 + b'</div></html>', URL, SOURCE)

    def test_listing_filters_hosts_deduplicates_and_finds_next(self):
        raw = ('<html><body>Public academic lecture listing for students'
               '<a href="/tsfx-xsbg/123.html">Lecture A</a>'
               '<a href="/tsfx-xsbg/123.html#part">Duplicate A</a>'
               '<a href="https://evil.example/tsfx-xsbg/9.html">external</a>'
               '<a href="/tsfx-xsbg.html?page=2">下一页</a></body></html>').encode()
        records, following = parse_listing(raw, SOURCE["url"], SOURCE)
        self.assertEqual([r["url"] for r in records], [URL])
        self.assertEqual(following, SOURCE["url"] + "?page=2")

    def test_no_links_is_a_parse_failure(self):
        with self.assertRaisesRegex(CollectionError, "parser failure"):
            parse_listing(b'<html><body>' + b'No matching content ' * 5 + b'</body></html>', SOURCE["url"], SOURCE)

    def test_next_page_cannot_leave_the_column(self):
        raw = (f'<html><body>Public academic lecture listing for students<a href="{URL}">Lecture A</a>'
               '<a href="/unrelated-action">下一页</a></body></html>').encode()
        _, following = parse_listing(raw, SOURCE["url"], SOURCE)
        self.assertIsNone(following)

    def test_dates_do_not_invent_year_or_midnight(self):
        self.assertEqual(parse_date("11月9日 14:00"), (None, None))
        self.assertEqual(parse_date("2026-11-09"), ("2026-11-09", "date"))
        self.assertEqual(parse_date("2026年11月9日 14:00"), ("2026-11-09T14:00:00+08:00", "minute"))
        self.assertEqual(parse_date("2026年2月30日"), (None, None))

    def test_document_has_field_evidence_and_unknown_eligibility(self):
        doc = parse_document(article(), URL, SOURCE)
        event = doc["event"]
        self.assertEqual(event["starts_at"], "2026-11-09T14:00:00+08:00")
        self.assertEqual(event["ends_at"], "2026-11-09T15:00:00+08:00")
        self.assertEqual(event["location"], "闵行校区 A 楼会议室")
        self.assertEqual(event["campus"], "闵行")
        self.assertIsNone(event["eligibility"])
        self.assertIsNone(doc["published_at"])
        self.assertEqual(event["field_evidence"]["starts_at"]["url"], URL)

    def test_recap_is_not_available(self):
        event, _ = extract_event("机器学习讲座精彩回顾", "时间：2026年11月9日 14:00", URL, SOURCE)
        self.assertEqual(event["status"], "recap")

    def test_multiple_events_require_review(self):
        event, flags = extract_event("系列讲座", "时间：2026年11月9日\n时间：2026年11月10日", URL, SOURCE)
        self.assertIsNone(event)
        self.assertIn("multiple_times_or_events_pending", flags)

    def test_conflicting_location_keeps_evidence_uncertain(self):
        event, flags = extract_event("学术讲座", "地点：A 楼\n地点：B 楼", URL, SOURCE)
        self.assertIsNone(event["location"])
        self.assertIn("location_conflict", flags)

    def test_images_are_flagged(self):
        doc = parse_document(article(body='<p>机器学习讲座详情请见下方海报，海报包含活动时间和地点，尚待人工核实。</p><img src="poster.png">'), URL, SOURCE)
        self.assertIn("images_or_attachments_pending", doc["evidence"]["warnings"])
        self.assertIsNone(doc["event"]["starts_at"])

    def test_untrusted_urls_and_redirects_are_rejected(self):
        with self.assertRaises(CollectionError):
            normalize_url("https://user:secret@me.sjtu.edu.cn/a")
        with self.assertRaises(CollectionError):
            require_host("https://elsewhere.example/a", SOURCE["allowed_hosts"])
        with self.assertRaises(CollectionError):
            HostRedirect(SOURCE["allowed_hosts"]).redirect_request(None, None, 302, "", {}, "https://login.example/")


class FakeFetcher:
    cache_hits = 0

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, url, hosts):
        self.calls.append(url)
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return value, url


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "db.sqlite3")
        self.store.sync_sources([SOURCE])

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def listing(self, extra=""):
        return (f'<html><body>Academic lecture list and information for all students<a href="{URL}">Lecture</a>{extra}</body></html>').encode()

    def test_repeated_page_terminates(self):
        fetcher = FakeFetcher({SOURCE["url"]: self.listing('<a href="?page=2">下一页</a>'),
                               SOURCE["url"] + "?page=2": self.listing(f'<a href="{SOURCE["url"]}">下一页</a>'), URL: article()})
        result = collect_source(self.store, SOURCE, fetcher, limit=5, max_pages=5)
        self.assertEqual(result["documents"], 1)
        self.assertEqual(result["coverage"]["stop_reason"], "repeated_page")
        self.assertEqual(len(fetcher.calls), 3)

    def test_failures_consume_attempt_budget(self):
        second = "https://me.sjtu.edu.cn/tsfx-xsbg/124.html"
        fetcher = FakeFetcher({SOURCE["url"]: self.listing(f'<a href="{second}">Second</a>'), URL: CollectionError("timeout")})
        result = collect_source(self.store, SOURCE, fetcher, limit=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["coverage"]["attempted_documents"], 1)
        self.assertNotIn(second, fetcher.calls)

    def test_source_failure_does_not_clear_history(self):
        collect_source(self.store, SOURCE, FakeFetcher({SOURCE["url"]: self.listing(), URL: article()}))
        result = collect_source(self.store, SOURCE, FakeFetcher({SOURCE["url"]: CollectionError("HTTP 503")}))
        self.assertEqual(result["status"], "failed")
        listing = self.store.listing()
        self.assertEqual(len(listing["documents"]), 1)
        self.assertIn("503", listing["sources"][0]["last_error"])
        self.assertEqual(result["coverage"]["stop_reason"], "request_or_parse_failure")
        self.assertEqual(result["coverage"]["list_pages"], [])
        self.assertEqual(result["coverage"]["attempted_list_pages"], [SOURCE["url"]])
        self.assertEqual(listing["runs"][0]["page_count"], 0)

    def test_disabled_source_is_never_requested(self):
        fetcher = FakeFetcher({})
        with self.assertRaises(CollectionError):
            collect_source(self.store, {**SOURCE, "enabled": False}, fetcher)
        self.assertEqual(fetcher.calls, [])

    def test_collect_twice_is_idempotent(self):
        fetcher = FakeFetcher({SOURCE["url"]: self.listing(), URL: article()})
        first = collect_source(self.store, SOURCE, fetcher)
        first_seen = self.store.listing()["documents"][0]["first_seen_at"]
        second = collect_source(self.store, SOURCE, fetcher)
        self.assertEqual((first["inserted"], second["inserted"], second["updated"]), (1, 0, 0))
        result = self.store.listing()
        self.assertEqual(len(result["documents"]), 1)
        self.assertEqual(result["documents"][0]["first_seen_at"], first_seen)
        self.assertEqual(len(result["runs"]), 2)


class FetcherTests(unittest.TestCase):
    def test_cache_avoids_another_request(self):
        with tempfile.TemporaryDirectory() as folder:
            fetcher = Fetcher(folder, delay=0)
            def request(url, hosts):
                return (b"User-agent: *\nAllow: /" if url.endswith("robots.txt") else article(), url, "text/html")
            with patch.object(fetcher, "_request", side_effect=request) as call:
                first = fetcher.get(URL, SOURCE["allowed_hosts"])
                second = fetcher.get(URL, SOURCE["allowed_hosts"])
            self.assertEqual(first, second)
            self.assertEqual(call.call_count, 2)
            self.assertEqual(fetcher.cache_hits, 1)

    def test_robots_denial_never_fetches_article(self):
        with tempfile.TemporaryDirectory() as folder:
            fetcher = Fetcher(folder, delay=0)
            with patch.object(fetcher, "_request", return_value=(b"User-agent: *\nDisallow: /", URL, "text/plain")) as call:
                with self.assertRaisesRegex(CollectionError, "does not allow"):
                    fetcher.get(URL, SOURCE["allowed_hosts"])
            self.assertEqual(call.call_count, 1)

    def test_network_retries_are_finite(self):
        with tempfile.TemporaryDirectory() as folder:
            fetcher = Fetcher(folder, delay=0, retries=1)
            with patch("campus_radar.collector.build_opener") as factory:
                factory.return_value.open.side_effect = URLError(TimeoutError())
                with self.assertRaisesRegex(CollectionError, "unverified"):
                    fetcher._request(URL, SOURCE["allowed_hosts"])
                self.assertEqual(factory.return_value.open.call_count, 2)


if __name__ == "__main__":
    unittest.main()
