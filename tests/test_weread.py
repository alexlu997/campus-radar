import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from campus_radar.collector import CollectionError, collect_source, labelled_fields, parse_wechat_document
from campus_radar.storage import Store
from campus_radar.weread import NoRedirect, payload, read_latest, remote


SOURCE = {"id": "wechat-me-liyuan", "name": "ME砺远学术", "book_id": "MP_WXS_3519281128",
          "wechat_biz": "MzUxOTI4MTEyOA==", "category": "学术科研", "adapter": "weread_latest",
          "visibility": "public", "enabled": True}
URL = "https://mp.weixin.qq.com/s/x2O6fxPWTh6Y4lVgFMqZgw"
RAW = (Path(__file__).parent / "fixtures/public/wechat-me-detail.html").read_bytes()
COVER = {"name": SOURCE["name"], "title": "讲座报名 | Prof. Badreddine Assouar学术报告会",
         "reviewId": SOURCE["book_id"] + "_x2O6fxPWTh6Y4lVgFMqZgw"}


class WeReadTests(unittest.TestCase):
    def test_real_article_fields_and_publication_are_separate(self):
        doc = parse_wechat_document(RAW, URL, SOURCE)
        self.assertEqual(doc["published_at"], "2026-10-23T17:56:00+08:00")
        self.assertEqual(doc["title"], COVER["title"])
        self.assertEqual(doc["event"]["title"], "Prof. Badreddine Assouar学术报告会")
        self.assertEqual(doc["event"]["starts_at"], "2026-10-27T10:00:00+08:00")
        self.assertEqual(doc["event"]["ends_at"], "2026-10-27T11:00:00+08:00")
        self.assertEqual(doc["event"]["location"], "机械与动力工程学院A楼 F310会议室")
        self.assertIsNone(doc["event"]["eligibility"])
        self.assertTrue(doc["evidence"]["account_identity_verified"])
        self.assertEqual(doc["evidence"]["image_count"], 2)

    def test_unknown_publication_not_replaced_with_collection_time(self):
        doc = parse_wechat_document(RAW.replace(b'var ct = "1792749360";', b'var ct=et(r,G);'), URL, SOURCE)
        self.assertIsNone(doc["published_at"])
        self.assertIn("published_time_unknown", doc["evidence"]["warnings"])

    def test_throttle_page_reports_wait_not_missing_login(self):
        with self.assertRaisesRegex(CollectionError, "操作频繁.*无需重复扫码"):
            parse_wechat_document('<html><title>验证</title><body>操作频繁，请稍后再试。</body></html>', URL, SOURCE)

    def test_missing_standalone_field_does_not_swallow_next_label(self):
        self.assertEqual(labelled_fields("讲座时间\n主讲人\n某教授", ["讲座时间"]), [])
        self.assertEqual(labelled_fields("谈到讲座时间\n2026年10月27日", ["讲座时间"]), [])

    def test_identity_mismatch_and_missing_body_are_failures(self):
        for raw in (RAW.replace(b'MzUxOTI4MTEyOA==', b'unknown'), RAW.replace('ME砺远学术'.encode(), b'Other'),
                    RAW.replace(b'id="js_content"', b'id="verification"')):
            with self.assertRaises(CollectionError):
                parse_wechat_document(raw, URL, SOURCE)

    def test_latest_only_requests_verified_account_and_body(self):
        cover = {**COVER, 'title': COVER['title'].replace('Badreddine ', 'Badreddine\u2002')}
        with patch('campus_radar.weread.remote', side_effect=[json.dumps(cover).encode(), RAW]) as request, patch('campus_radar.weread.time.sleep'):
            doc = read_latest(SOURCE, 'test-cookie')
        self.assertEqual(doc['url'], URL)
        self.assertEqual([call.args[0] for call in request.call_args_list], ['/api/mp/cover', '/web/mp/content'])
        self.assertNotIn('test-cookie', json.dumps(doc))

    def test_changed_account_or_review_id_stops_before_body_request(self):
        for key, value in [('name', 'Other'), ('reviewId', 'MP_WXS_9999_x2O6fxPWTh6Y4lVgFMqZgw')]:
            with patch('campus_radar.weread.remote', return_value=json.dumps({**COVER, key: value}).encode()) as request:
                with self.assertRaises(CollectionError):
                    read_latest(SOURCE, 'test-cookie')
                self.assertEqual(request.call_count, 1)

    def test_error_response_and_title_mismatch_do_not_accept_content(self):
        for raw in (b'{"errCode": -2012}', RAW.replace(b'Badreddine', b'Another')):
            with patch('campus_radar.weread.remote', side_effect=[json.dumps(COVER).encode(), raw]), patch('campus_radar.weread.time.sleep'):
                with self.assertRaises(CollectionError):
                    read_latest(SOURCE, 'test-cookie')

    def test_upstream_error_text_is_never_reported(self):
        with self.assertRaises(CollectionError) as caught:
            payload(b'{"errCode": -2041, "errMsg": "secret-cookie"}')
        self.assertIn('-2041', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))

    def test_credentials_are_never_redirected(self):
        with self.assertRaises(CollectionError):
            NoRedirect().redirect_request(Request('https://weread.qq.com', headers={'Cookie': 'secret'}), None,
                                          302, 'redirect', {}, 'https://example.com')

    def test_network_retry_but_no_http_retry(self):
        for error, calls in [(URLError('secret'), 2), (HTTPError('https://weread.qq.com', 403, 'secret', {}, None), 1)]:
            with patch('campus_radar.weread.build_opener') as opener, patch('campus_radar.weread.time.sleep'):
                opener.return_value.open.side_effect = error
                with self.assertRaises(CollectionError) as caught:
                    remote('/api/mp/cover', {'bookId': SOURCE['book_id']}, 'test-cookie')
                self.assertEqual(opener.return_value.open.call_count, calls)
                self.assertNotIn('secret', str(caught.exception))

    def test_official_merge_repeated_refresh_and_failure_preserve_records(self):
        with tempfile.TemporaryDirectory() as temp, Store(Path(temp) / 'db.sqlite3') as store:
            store.sync_sources([SOURCE, {'id': 'official', 'name': '官网', 'visibility': 'public'}])
            document = parse_wechat_document(RAW, URL, SOURCE)
            official = {**document, 'url': 'https://me.sjtu.edu.cn/tsfx-xsbg/78074.html', 'title': document['event']['title']}
            store.save_document('official', official)
            event_id = store.event_listing()['events'][0]['id']
            store.set_favorite(event_id, True)
            with patch('campus_radar.weread.fetch_latest', return_value=document):
                first = collect_source(store, SOURCE, None)
                second = collect_source(store, SOURCE, None)
            with patch('campus_radar.weread.fetch_latest', side_effect=CollectionError('授权已失效')):
                failed = collect_source(store, SOURCE, None)
            self.assertEqual((first['inserted'], second['inserted'], second['updated']), (1, 0, 0))
            self.assertFalse(first['coverage']['historical_pagination_complete'])
            self.assertEqual(first['coverage']['scope'], 'latest_article_only')
            self.assertEqual((failed['status'], failed['documents']), ('failed', 0))
            self.assertEqual(store.event_listing()['total'], 1)
            self.assertEqual(store.event_listing(favorites=True)['events'][0]['id'], event_id)
            self.assertEqual(len(store.event_detail(event_id)['sources']), 2)
            self.assertEqual(len(store.listing()['documents']), 2)
            store.set_source_enabled(SOURCE['id'], False)
            store.set_source_enabled(SOURCE['id'], True)


if __name__ == '__main__':
    unittest.main()
