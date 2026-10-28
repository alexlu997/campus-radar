from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from campus_radar.collector import extract_events, parse_document
from campus_radar.digest import make_digest, preferences
from campus_radar.storage import Store


NOW = datetime.fromisoformat("2026-10-28T08:00:00+08:00")


class PhaseTwoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "public.sqlite3"
        self.store = Store(self.path)
        self.store.sync_sources([{"id": "a", "name": "官网甲", "category": "学术科研"},
                                 {"id": "b", "name": "官网乙", "category": "创新创业"}])
        self.doc = {"url": "https://example.edu/1", "title": "人工智能与机器人学术讲座", "body": "正文包含机器学习。",
                    "processing_status": "parsed", "evidence": {},
                    "event": {"title": "人工智能与机器人学术讲座", "starts_at": "2026-11-01T14:00:00+08:00",
                              "ends_at": "2026-11-01T16:00:00+08:00", "location": "闵行校区A楼", "campus": "闵行",
                              "status": "scheduled_registration_unknown", "field_evidence": {}}}

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def save(self, doc=None, source="a"):
        self.store.save_document(source, doc or self.doc)
        return self.store.event_listing(now=NOW)["events"][0]

    def test_repost_merges_sources_but_different_day_stays_separate(self):
        event = self.save()
        self.store.set_favorite(event["id"], True)
        repost = {**deepcopy(self.doc), "url": "https://other.edu/2"}
        self.save(repost, "b")
        events = self.store.event_listing(now=NOW)["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["sources"]), 2)
        self.assertTrue(events[0]["favorite"])
        repost["url"] = "https://other.edu/3"
        repost["event"]["starts_at"] = "2026-11-02T14:00:00+08:00"
        self.save(repost, "b")
        self.assertEqual(self.store.event_listing()["total"], 2)
        self.assertEqual(len(self.store.listing()["documents"]), 3)

    def test_actual_series_prefix_and_subtitle_forms_merge_without_fuzzy_matching(self):
        from campus_radar.events import same_event
        left = {**self.doc["event"], "title": "如何在大学阶段学好英语 — 学习能力提升营"}
        right = {**left, "title": "学习能力提升营（总第57期）：如何在大学阶段学好英语", "location": "闵行校区 A 楼"}
        self.assertTrue(same_event(left, right))
        self.assertFalse(same_event(left, {**right, "starts_at": "2026-11-02T14:00:00+08:00"}))
        self.assertFalse(same_event(left, {**right, "title": "学习能力提升营：如何在大学阶段学好数学"}))

    def test_conflict_preserves_each_source_and_does_not_claim_known_end(self):
        self.save()
        repost = {**deepcopy(self.doc), "url": "https://other.edu/2"}
        repost["event"]["ends_at"] = "2026-11-01T15:00:00+08:00"
        event = self.save(repost, "b")
        self.assertIsNone(event["ends_at"])
        self.assertEqual(len(event["conflicts"]["ends_at"]), 2)
        self.assertEqual(event["status"], "needs_review")

    def test_favorites_feedback_and_alias_survive_later_merge_and_restart(self):
        first = self.save()
        unknown = {**deepcopy(self.doc), "url": "https://other.edu/2"}
        unknown["event"]["starts_at"] = None
        self.save(unknown, "b")
        second = next(e for e in self.store.event_listing()["events"] if e["id"] != first["id"])
        self.store.set_favorite(second["id"], True)
        self.store.set_feedback(second["id"], "interested")
        self.store.merge_events(first["id"], second["id"])
        with Store(self.path) as reopened:
            detail = reopened.event_detail(second["id"])
            self.assertEqual(detail["id"], first["id"])
            self.assertTrue(detail["favorite"])
            self.assertEqual(detail["feedback"], "interested")
            self.assertEqual(len(detail["documents"]), 2)

    def test_unknown_time_and_campus_never_match_known_filters(self):
        self.save()
        unknown = {**deepcopy(self.doc), "url": "https://other.edu/unknown"}
        unknown["event"].update(starts_at=None, ends_at=None, campus=None)
        self.save(unknown)
        self.assertEqual(self.store.event_listing(query="机器学习")["total"], 2)
        self.assertEqual(self.store.event_listing(date_from="2026-11-01", date_to="2026-11-01")["total"], 1)
        self.assertEqual(self.store.event_listing(campus="闵行")["total"], 1)
        self.assertEqual(self.store.event_listing(campus="unknown")["total"], 1)
        self.assertEqual(self.store.event_listing(category="创新创业")["total"], 0)
        self.assertEqual(self.store.event_listing(query="%'")["total"], 0)
        self.assertIsNotNone(self.store.event_listing(limit=1)["next_offset"])
        with self.assertRaises(ValueError):
            self.store.event_listing(date_from="2026-11-02", date_to="2026-11-01")

    def test_confirmed_time_can_merge_previously_unknown_saved_repost(self):
        self.save()
        repost = {**deepcopy(self.doc), "url": "https://other.edu/late"}
        repost["event"]["starts_at"] = None
        self.save(repost, "b")
        saved = next(e for e in self.store.event_listing()["events"] if e.get("starts_at") is None)
        self.store.set_favorite(saved["id"], True)
        repost["event"]["starts_at"] = self.doc["event"]["starts_at"]
        self.save(repost, "b")
        self.assertEqual(self.store.event_listing()["total"], 1)
        self.assertTrue(self.store.event_detail(saved["id"])["favorite"])

    def test_elapsed_time_is_not_a_document_change(self):
        self.save()
        changed = deepcopy(self.doc)
        changed["event"]["status"] = "past"
        self.assertEqual(self.store.save_document("a", changed), "unchanged")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM event_changes").fetchone()[0], 0)
        self.assertEqual(self.store.event_listing(now=datetime.fromisoformat("2026-11-02T08:00:00+08:00"))["events"][0]["status"], "past")

    def test_whitespace_cleanup_is_not_an_important_change(self):
        self.save()
        changed = deepcopy(self.doc)
        changed["event"]["location"] = "  闵行校区A楼  "
        self.store.save_document("a", changed)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM event_changes").fetchone()[0], 0)

    def test_disappearing_extraction_keeps_saved_event_as_needs_review(self):
        event = self.save()
        self.store.set_favorite(event["id"], True)
        self.store.save_document("a", {**self.doc, "event": None})
        saved = self.store.event_listing(favorites=True)["events"][0]
        self.assertEqual(saved["status"], "needs_review")
        self.assertTrue(saved["archived"])

    def test_two_labelled_events_and_ambiguous_times(self):
        body = "一、人工智能讲座\n时间：2026年11月1日 14:00\n地点：闵行校区A楼\n二、机器人学术讲座\n时间：2026年11月2日 15:00\n地点：徐汇校区B楼"
        events, flags = extract_events("讲座合集", body, self.doc["url"], {"category": "学术科研"})
        self.assertEqual(len(events), 2)
        self.assertNotIn("multiple_times_or_events_pending", flags)
        self.store.save_document("a", {**self.doc, "events": events})
        self.assertEqual(self.store.event_listing()["total"], 2)
        self.assertEqual(len(self.store.listing()["documents"]), 1)
        events, flags = extract_events("讲座合集", "时间：2026年11月1日\n时间：2026年11月2日", self.doc["url"], {})
        self.assertEqual(events, [])
        self.assertIn("multiple_times_or_events_pending", flags)

    def test_real_notice_has_two_distinct_deadline_actions(self):
        source = {"adapter": "ctld", "category": "学术科研"}
        doc = parse_document((Path(__file__).parent / "fixtures/public/ctld-detail.html").read_bytes(), "https://ctld.sjtu.edu.cn/events/detail/real", source)
        self.store.save_document("a", doc)
        actions = [e for e in self.store.event_listing()["events"] if e["kind"] == "deadline"]
        self.assertEqual(len(actions), 2)
        self.assertEqual({e["registration_deadline"] for e in actions}, {"2027-01-01T00:00:00+08:00", "2027-02-21T00:00:00+08:00"})
        self.assertTrue(any("工作坊申报" in e["title"] for e in actions))
        self.assertIn("24:00", json.dumps(actions, ensure_ascii=False))
        self.assertIsNone(self.store.listing()["documents"][0]["event"]["registration_deadline"])

    def test_digest_is_saved_deduplicated_and_explains_changes(self):
        self.save()
        self.save({**deepcopy(self.doc), "url": "https://other.edu/2"}, "b")
        preferences(self.store, {"themes": ["机器学习"], "source_ids": []})
        digest = make_digest(self.store, now=NOW)
        self.assertEqual(len(digest["items"]), 1)
        self.assertEqual(len(digest["items"][0]["event"]["sources"]), 2)
        self.assertTrue(any("关注主题" in r for r in digest["items"][0]["reasons"]))
        self.assertEqual(make_digest(self.store, now=NOW), digest)
        changed = deepcopy(self.doc)
        changed["event"]["status"] = "cancelled"
        self.store.save_document("a", changed)
        updated = make_digest(self.store, now=NOW, refresh=True)
        self.assertEqual(updated["items"][0]["section"], "changes")
        self.assertEqual(updated["items"][0]["event"]["status"], "needs_review")
        self.store.set_feedback(updated["items"][0]["event"]["id"], "not_interested")
        self.assertEqual(make_digest(self.store, now=NOW, refresh=True)["items"], [])

    def seed_digest_events(self, count):
        with patch("campus_radar.storage.utc_now", return_value="2026-10-27T23:00:00+00:00"):
            for i in range(count):
                doc = deepcopy(self.doc)
                doc.update(url=f"https://example.edu/batch/{i}", title=f"独立学术活动编号{i:02d}")
                doc["event"]["title"] = doc["title"]
                self.store.save_document("a", doc)

    def test_digest_carries_unselected_events_across_days_and_restart(self):
        self.seed_digest_events(11)
        first = make_digest(self.store, now=NOW)
        self.assertEqual((first["candidate_count"], len(first["items"])), (11, 10))
        legacy = {key: value for key, value in first.items() if key != "selected_event_ids"}
        self.store.db.execute("UPDATE digests SET data=?", (json.dumps(legacy),))
        self.store.db.commit()
        with Store(self.path) as reopened:
            second = make_digest(reopened, now=NOW + timedelta(days=1))
            self.assertEqual((second["candidate_count"], len(second["items"])), (1, 1))
            first_ids = {item["event"]["id"] for item in first["items"]}
            self.assertNotIn(second["items"][0]["event"]["id"], first_ids)
            self.assertEqual(make_digest(reopened, now=NOW + timedelta(days=2))["items"], [])
            self.assertEqual(make_digest(reopened, day=first["day"], now=NOW + timedelta(days=2)), legacy)

    def test_digest_refresh_retains_selection_history_when_new_item_displaces_old(self):
        self.seed_digest_events(11)
        first = make_digest(self.store, now=NOW)
        self.assertEqual(make_digest(self.store, now=NOW, refresh=True)["items"], first["items"])
        unselected = {e["id"] for e in self.store.event_listing(now=NOW)["events"]} - {i["event"]["id"] for i in first["items"]}
        with patch("campus_radar.storage.utc_now", return_value="2026-10-28T00:30:00+00:00"):
            newcomer = self.save()
            self.store.set_favorite(newcomer["id"], True)
        refreshed = make_digest(self.store, now=NOW + timedelta(hours=1), refresh=True)
        self.assertIn(newcomer["id"], {item["event"]["id"] for item in refreshed["items"]})
        tomorrow = make_digest(self.store, now=NOW + timedelta(days=1))
        self.assertEqual({item["event"]["id"] for item in tomorrow["items"]}, unselected)

    def test_digest_selected_alias_is_not_new_again_after_merge(self):
        with patch("campus_radar.storage.utc_now", return_value="2026-10-27T23:00:00+00:00"):
            first = self.save()
            repost = deepcopy(self.doc)
            repost["url"] = "https://other.edu/unknown"
            repost["event"].update(starts_at=None, ends_at=None)
            self.save(repost, "b")
            second = next(e for e in self.store.event_listing(now=NOW)["events"] if e["id"] != first["id"])
            self.store.set_favorite(second["id"], True)
            selected = make_digest(self.store, now=NOW, limit=1)
            self.assertEqual(selected["items"][0]["event"]["id"], second["id"])
            self.store.merge_events(first["id"], second["id"])
        tomorrow = make_digest(self.store, now=NOW + timedelta(days=1))
        self.assertEqual(tomorrow["items"], [])

    def test_digest_selection_keeps_deadlines_changes_and_explicit_feedback(self):
        self.seed_digest_events(5)
        events = self.store.event_listing(now=NOW)["events"]
        self.store.set_feedback(events[0]["id"], "known")
        self.store.set_feedback(events[1]["id"], "not_interested")
        with patch("campus_radar.storage.utc_now", return_value="2026-10-27T23:00:00+00:00"):
            deadline_doc = deepcopy(self.doc)
            deadline_doc["event"]["registration_deadline"] = "2026-10-31T18:00:00+08:00"
            self.save(deadline_doc)
            deadline = next(e for e in self.store.event_listing(now=NOW)["events"] if e["title"] == deadline_doc["title"])
        first = make_digest(self.store, now=NOW)
        self.assertEqual(len(first["items"]), 4)
        with patch("campus_radar.storage.utc_now", return_value="2026-10-28T02:00:00+00:00"):
            changed = deepcopy(deadline_doc)
            changed["event"]["location"] = "徐汇校区报告厅"
            self.store.save_document("a", changed)
        second = make_digest(self.store, now=NOW + timedelta(days=1))
        self.assertEqual(len(second["items"]), 1)
        self.assertEqual(second["items"][0]["event"]["id"], deadline["id"])
        self.assertEqual(second["items"][0]["section"], "changes")
        third = make_digest(self.store, now=NOW + timedelta(days=2))
        self.assertEqual(third["items"][0]["section"], "deadlines")
        self.assertEqual(make_digest(self.store, now=NOW + timedelta(days=5))["items"], [])

    def test_source_preferences_do_not_disable_collection(self):
        self.store.sync_sources([{"id": "a", "name": "官网甲", "adapter": "ctld", "visibility": "public", "enabled": True},
                                 {"id": "b", "name": "官网乙", "visibility": "authenticated", "enabled": False}])
        preferences(self.store, {"themes": [], "source_ids": ["b"]})
        self.assertEqual([s["id"] for s in self.store.enabled_sources()], ["a"])
        with self.assertRaises(ValueError):
            self.store.set_source_enabled("b", True)
        self.store.set_source_enabled("a", False)
        self.assertEqual(self.store.enabled_sources(), [])


if __name__ == "__main__":
    unittest.main()
