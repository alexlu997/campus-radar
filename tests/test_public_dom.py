"""Regression cases reduced from actual public pages; no account fixtures."""

import json
from pathlib import Path
import unittest

from campus_radar.collector import event_range, parse_document, parse_listing


ROOT=Path(__file__).resolve().parents[1]
SOURCES={s['adapter']:s for s in json.loads((ROOT/'data/sources.json').read_text())['sources'] if s.get('enabled')}


class PublicDomTests(unittest.TestCase):
    def raw(self,name):return (ROOT/'tests/fixtures/public'/f'{name}.html').read_bytes()

    def test_actual_lists_exclude_navigation_and_find_next_page(self):
        cases=[('news','sjtu_news',10,'https://news.sjtu.edu.cn/xsjz/index_2.html'),
               ('me','sjtu_me',12,'https://me.sjtu.edu.cn/tsfx-xsbg/p2.html'),
               ('ctld','ctld',12,'https://ctld.sjtu.edu.cn/events?page=2')]
        for name,adapter,count,next_url in cases:
            s=SOURCES[adapter];items,following=parse_listing(self.raw(name+'-list'),s['url'],s)
            self.assertEqual(len(items),count)
            self.assertEqual(following,next_url)
            self.assertTrue(all('navonly' not in item['url'] for item in items))
            self.assertFalse(items[0]['title'].startswith('2026-10-27'))

    def test_ctld_pagination_preserves_filters_and_stops_at_last_page(self):
        s=SOURCES['ctld'];raw=self.raw('ctld-list')
        _,following=parse_listing(raw,s['url']+'?keyword=AI&group_id=4&atagId=3',s)
        self.assertEqual(following,s['url']+'?keyword=AI&group_id=4&atagId=3&page=2')
        _,following=parse_listing(raw.replace(b'var _page = 1;',b'var _page = 82;'),s['url']+'?page=82',s)
        self.assertIsNone(following)

    def test_news_header_is_activity_evidence_and_keeps_full_room(self):
        s=SOURCES['sjtu_news'];d=parse_document(self.raw('news-detail'),s['latest_sample']['url'],s)
        e=d['event']
        self.assertEqual(e['starts_at'],'2026-10-27T18:30:00+08:00')
        self.assertEqual(e['ends_at'],'2026-10-27T20:00:00+08:00')
        self.assertEqual(e['location'],'闵行校区陈瑞球楼 100号')
        self.assertEqual(e['audience'],'同学')
        self.assertIsNone(e['eligibility'])
        self.assertEqual(d['evidence']['event_metadata']['origin'],'official_event_header')

    def test_me_actual_body_and_future_publication_warning(self):
        s=SOURCES['sjtu_me'];d=parse_document(self.raw('me-detail'),s['latest_sample']['url'],s)
        self.assertEqual(d['title'],'思源讲堂第四十三期：Prof. Erwin Reisner学术报告会')
        self.assertEqual(d['event']['ends_at'],'2026-11-09T15:00:00+08:00')
        self.assertEqual(d['published_at'],'2026-11-09')

    def test_conference_date_range_does_not_invent_midnight_or_eligibility(self):
        s=SOURCES['ctld'];d=parse_document(self.raw('ctld-detail'),s['latest_sample']['url'],s)
        e=d['event']
        self.assertEqual((e['starts_at'],e['ends_at'],e['time_precision']),('2027-04-16','2027-04-18','date'))
        self.assertIn('高校教学研究人员',e['audience'])
        self.assertIsNone(e['eligibility'])
        self.assertIsNone(d['published_at'])
        deadlines=e['field_evidence']['registration_deadline']['separate_submission_deadlines']
        self.assertEqual([x['raw'] for x in deadlines],['2026年12月31日24:00','2027年2月20日24:00'])
        self.assertIsNone(e['registration_deadline'])

    def test_multiple_full_dates_only_form_a_range_with_a_range_separator(self):
        self.assertEqual(event_range('2027年4月16日至2027年4月18日'),('2027-04-16','2027-04-18','date'))
        self.assertEqual(event_range('2027年4月16日和2027年4月18日'),(None,None,None))
        self.assertEqual(event_range('2027年4月18-16日'),(None,None,None))


if __name__=='__main__':unittest.main()
