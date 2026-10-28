import json
from pathlib import Path
import plistlib
import runpy
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from campus_radar.scheduler import agent_label, install_schedule, schedule_config, uninstall_schedule
from campus_radar.storage import Store, default_db
from campus_radar.updates import update_lock


ROOT = Path(__file__).resolve().parent.parent


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve()
        self.user_dir = self.folder / "user"
        self.store = Store(self.folder / "data/public.sqlite3")
        self.sources = self.folder / "sources.json"
        self.sources.write_text('{"sources": []}')
        self.home_patch = patch("campus_radar.scheduler.Path.home", return_value=self.user_dir)
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        self.store.db.close()
        self.temp.cleanup()

    def launch(self, args, **kwargs):
        return SimpleNamespace(returncode=1 if args[1] == "print" else 0)

    def test_install_upgrade_uninstall_use_selected_database_and_preserve_data(self):
        self.store.set_setting("preferences", {"themes": ["机器学习"], "source_ids": []})
        with patch("campus_radar.scheduler.subprocess.run", side_effect=self.launch):
            result = install_schedule(self.store, self.sources)
        path = Path(result["path"])
        data = plistlib.loads(path.read_bytes())
        self.assertEqual(data["ProgramArguments"][4], str(self.store.path.resolve()))
        self.assertEqual(data["EnvironmentVariables"]["CAMPUS_RADAR_HOME"], str(self.store.path.parent.resolve()))
        self.assertNotEqual(agent_label(self.store.path), agent_label(self.folder / "other/public.sqlite3"))
        with patch("campus_radar.scheduler.subprocess.run", return_value=SimpleNamespace(returncode=0)) as launch:
            upgraded = install_schedule(self.store, self.sources)
            removed = uninstall_schedule(self.store)
        self.assertEqual(upgraded["label"], result["label"])
        self.assertEqual(removed["database_preserved"], str(self.store.path.resolve()))
        self.assertFalse(path.exists())
        self.assertFalse(schedule_config(self.store)["enabled"])
        self.assertEqual(self.store.setting("preferences")["themes"], ["机器学习"])
        self.assertTrue(any(call.args[0][1] == "bootout" for call in launch.call_args_list))

    def test_failed_upgrade_restores_runtime_and_previous_agent(self):
        with patch("campus_radar.scheduler.subprocess.run", side_effect=self.launch):
            result = install_schedule(self.store, self.sources)
        path = Path(result["path"])
        previous_plist = path.read_bytes()
        installed = Path(result["runtime_root"]) / "campus_radar/__init__.py"
        installed.write_text("# previous installed version\n")
        attempts = []

        def launch(args, **kwargs):
            if args[1] == "bootstrap":
                attempts.append(args)
                if len(attempts) == 1:
                    raise subprocess.CalledProcessError(5, args)
            return SimpleNamespace(returncode=0)

        with patch("campus_radar.scheduler.subprocess.run", side_effect=launch):
            with self.assertRaises(subprocess.CalledProcessError):
                install_schedule(self.store, self.sources)
        self.assertEqual(path.read_bytes(), previous_plist)
        self.assertEqual(installed.read_text(), "# previous installed version\n")
        self.assertEqual(len(attempts), 2)

    def test_different_owner_or_active_update_blocks_install_and_uninstall(self):
        path = self.user_dir / "Library/LaunchAgents" / (agent_label(self.store.path) + ".plist")
        path.parent.mkdir(parents=True)
        path.write_bytes(plistlib.dumps({"Label": "unrelated", "ProgramArguments": ["unrelated"]}))
        original = path.read_bytes()
        with patch("campus_radar.scheduler.subprocess.run") as launch:
            for operation in (lambda: install_schedule(self.store, self.sources), lambda: uninstall_schedule(self.store)):
                with self.assertRaisesRegex(ValueError, "different target"):
                    operation()
            launch.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        with update_lock(self.store):
            with self.assertRaisesRegex(ValueError, "active update"):
                install_schedule(self.store, self.sources)
            with self.assertRaisesRegex(ValueError, "active update"):
                uninstall_schedule(self.store)

    def test_launcher_setup_is_explicit_and_data_location_is_shared(self):
        launcher = runpy.run_path(str(ROOT / "tools/campus-radar"))["main"]
        data = self.folder / "fresh data"
        with patch("sys.version_info", (3, 13, 2)), patch("venv.EnvBuilder") as builder, patch("subprocess.run") as run:
            self.assertEqual(launcher(["--data-dir", str(data), "setup"]), 0)
            builder.return_value.create.assert_called_once_with(data / "venv")
            self.assertIn(str(ROOT / "requirements.txt"), run.call_args.args[0])
        with patch.dict("os.environ", {"CAMPUS_RADAR_HOME": str(data)}):
            self.assertEqual(default_db(), data / "public.sqlite3")
        self.assertEqual(launcher(["--data-dir", str(data), "events"]), 2)

    def test_optional_source_defaults_do_not_reset_existing_user_choices(self):
        source = {"id": "optional", "name": "公众号", "adapter": "weread_latest", "visibility": "public", "enabled": True}
        self.store.sync_sources([source])
        release = {**source, "optional": True, "enabled": False}
        self.store.sync_sources([release])
        self.assertTrue(self.store.source_states()[0]["enabled"])
        self.store.set_source_enabled("optional", False)
        self.store.sync_sources([release])
        self.assertFalse(self.store.source_states()[0]["enabled"])
        with Store(self.folder / "new/public.sqlite3") as fresh:
            fresh.sync_sources([release])
            self.assertEqual(fresh.enabled_sources(), [])


if __name__ == "__main__":
    unittest.main()
