from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campus_radar.scheduler import launch_agent_data, schedule_config, tick
from campus_radar.storage import Store


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "db.sqlite3")
        self.store.sync_sources([{"id": "a", "name": "官网", "visibility": "public", "adapter": "ctld", "enabled": True}])
        self.start = datetime.fromisoformat("2026-10-28T07:00:00+08:00")
        with patch("campus_radar.scheduler.utc_now", return_value=self.start.isoformat()):
            schedule_config(self.store, {"enabled": True, "hour": 8, "minute": 0, "timezone": "Asia/Shanghai"})

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_before_due_does_not_run_previous_day_then_late_wake_runs_once(self):
        with patch("campus_radar.scheduler.enqueue_update", return_value={"id": "job", "status": "queued", "source_ids": ["a"]}) as enqueue:
            self.assertEqual(tick(self.store, "sources.json", now=self.start)["status"], "waiting")
            enqueue.assert_not_called()
            late = self.start + timedelta(hours=3)
            result = tick(self.store, "sources.json", now=late)
            self.assertEqual((result["due_day"], result["attempts"]), ("2026-10-28", 1))
            with patch("campus_radar.scheduler.update_status", return_value={"status": "success", "ended_at": late.isoformat(), "source_ids": ["a"]}):
                self.assertEqual(tick(self.store, "sources.json", now=late + timedelta(minutes=1))["status"], "success")
            self.assertEqual(enqueue.call_count, 1)

    def test_retry_wait_budget_and_new_day_reset(self):
        now = self.start + timedelta(hours=2)
        with patch("campus_radar.scheduler.enqueue_update", return_value={"id": "job", "status": "queued", "source_ids": ["a"]}) as enqueue, patch("campus_radar.scheduler.update_status", return_value={"status": "failed"}):
            tick(self.store, "sources.json", now=now)
            self.assertEqual(tick(self.store, "sources.json", now=now + timedelta(minutes=1))["status"], "retry_wait")
            tick(self.store, "sources.json", now=now + timedelta(minutes=15))
            tick(self.store, "sources.json", now=now + timedelta(minutes=30))
            self.assertEqual(tick(self.store, "sources.json", now=now + timedelta(minutes=45))["status"], "failed")
            self.assertEqual(enqueue.call_count, 3)
            result = tick(self.store, "sources.json", now=now + timedelta(days=3))
            self.assertEqual((result["due_day"], result["attempts"]), ("2026-10-31", 1))

    def test_running_single_source_does_not_fulfill_full_daily_update(self):
        self.store.sync_sources([{"id": "b", "name": "第二官网", "visibility": "public", "adapter": "ctld", "enabled": True}])
        with patch("campus_radar.scheduler.enqueue_update", return_value={"id": "single", "status": "running", "source_ids": ["a"], "duplicate_trigger": True}):
            result = tick(self.store, "sources.json", now=self.start + timedelta(hours=2))
            self.assertEqual(result["status"], "waiting_for_other_update")
            self.assertNotIn("job_id", result)
            self.assertEqual(result["attempts"], 0)

    def test_successful_but_incomplete_scan_continues_within_daily_budget(self):
        now = self.start + timedelta(hours=2)
        with patch("campus_radar.scheduler.enqueue_update", return_value={"id": "job", "status": "queued", "source_ids": ["a"]}) as enqueue, patch(
                "campus_radar.scheduler.update_status", return_value={"status": "success", "source_ids": ["a"], "collection_pending": True}):
            tick(self.store, "sources.json", now=now)
            self.assertEqual(tick(self.store, "sources.json", now=now + timedelta(minutes=1))["status"], "retry_wait")
            tick(self.store, "sources.json", now=now + timedelta(minutes=15))
            tick(self.store, "sources.json", now=now + timedelta(minutes=30))
            result = tick(self.store, "sources.json", now=now + timedelta(minutes=45))
            self.assertEqual(result["status"], "catchup_pending")
            self.assertEqual(enqueue.call_count, 3)
        with patch("campus_radar.scheduler.update_status", return_value={"status": "success", "source_ids": ["a"], "ended_at": now.isoformat(), "collection_pending": False}):
            self.assertEqual(tick(self.store, "sources.json", now=now + timedelta(minutes=50))["status"], "success")

    def test_enabling_new_source_clears_old_completion_evidence(self):
        self.store.set_setting('schedule_state', {'due_day': '2026-10-28', 'job_id': 'old',
            'completed_at': self.start.isoformat(), 'satisfied_by_existing_update': True, 'attempts': 0})
        self.store.sync_sources([{'id': 'b', 'name': '公众号', 'visibility': 'public', 'adapter': 'weread_latest', 'enabled': True}])
        with patch('campus_radar.scheduler.update_status', return_value={'status': 'success', 'source_ids': ['a']}), patch(
                'campus_radar.scheduler.enqueue_update', return_value={'id': 'new', 'status': 'queued', 'source_ids': ['a', 'b']}):
            result = tick(self.store, 'sources.json', now=self.start + timedelta(hours=2))
        self.assertEqual(result['job_id'], 'new')
        self.assertNotIn('completed_at', result)
        self.assertNotIn('satisfied_by_existing_update', result)

    def test_bad_configuration_and_literal_program_arguments(self):
        with self.assertRaises(ValueError):
            schedule_config(self.store, {"enabled": True, "hour": 24, "minute": 0, "timezone": "Asia/Shanghai"})
        data = launch_agent_data("/tmp/path with spaces/db.sqlite3", "/tmp/sources.json")
        self.assertEqual(data["StartInterval"], 60)
        self.assertIn(str(Path("/tmp/path with spaces/db.sqlite3").resolve()), data["ProgramArguments"])
        self.assertEqual(data["ProgramArguments"][-1], "schedule-tick")
        self.assertTrue(data["RunAtLoad"])


if __name__ == "__main__":
    unittest.main()
