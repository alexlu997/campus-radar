"""Daily local updates with a timezone-aware due check and bounded catch-up."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from zoneinfo import ZoneInfo

from .storage import utc_now
from .updates import enqueue_update, update_lock, update_status


LABEL = "org.campusradar.daily"


def schedule_config(store, value=None):
    current = store.setting("schedule", {"enabled": False, "hour": 8, "minute": 0, "timezone": "Asia/Shanghai"})
    if value is None:
        return {**current, "state": store.setting("schedule_state", {}), "runtime": store.setting("schedule_runtime", {})}
    if not isinstance(value, dict) or set(value) != {"enabled", "hour", "minute", "timezone"}:
        raise ValueError("Schedule requires enabled, hour, minute and timezone")
    if type(value["enabled"]) is not bool or type(value["hour"]) is not int or not 0 <= value["hour"] <= 23 or type(value["minute"]) is not int or not 0 <= value["minute"] <= 59:
        raise ValueError("Invalid schedule time")
    ZoneInfo(value["timezone"])
    if value["enabled"] and not current["enabled"]:
        value = {**value, "enabled_since": utc_now()}
    elif current.get("enabled_since"):
        value = {**value, "enabled_since": current["enabled_since"]}
    return store.set_setting("schedule", value)


def tick(store, sources_path, *, now=None):
    config = schedule_config(store)
    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(config["timezone"]))
    state = store.setting("schedule_state", {})
    state["last_tick_at"] = now.isoformat()
    if not config["enabled"]:
        return store.set_setting("schedule_state", {**state, "status": "disabled"})
    due = now.replace(hour=config["hour"], minute=config["minute"], second=0, microsecond=0)
    if due > now:
        due -= timedelta(days=1)
    enabled = datetime.fromisoformat(config.get("enabled_since", now.isoformat())).astimezone(now.tzinfo)
    if due.date() < enabled.date():
        return store.set_setting("schedule_state", {**state, "status": "waiting", "next_due_at": (due + timedelta(days=1)).isoformat()})
    due_day = due.date().isoformat()
    if state.get("due_day") != due_day:
        state = {"due_day": due_day, "attempts": 0, "last_tick_at": now.isoformat()}
    state["next_due_at"] = (due + timedelta(days=1)).isoformat()
    enabled_ids = {s["id"] for s in store.enabled_sources()}
    pending = False
    if state.get("job_id"):
        job = update_status(store, state["job_id"])
        pending = job.get("collection_pending", False)
        if job["status"] == "success" and not pending and set(job.get("source_ids", [])) == enabled_ids:
            state.pop("error", None)
            return store.set_setting("schedule_state", {**state, "status": "success", "completed_at": job["ended_at"]})
        if job["status"] in {"running", "queued"}:
            return store.set_setting("schedule_state", {**state, "status": job["status"]})
    for row in store.db.execute("SELECT id,ended_at,data FROM updates WHERE status='success' AND ended_at>=? ORDER BY ended_at DESC", (due.astimezone(timezone.utc).isoformat(),)):
        data = json.loads(row["data"])
        if set(data["source_ids"]) == enabled_ids and not data.get("collection_pending", False):
            state.pop("error", None)
            return store.set_setting("schedule_state", {**state, "status": "success", "job_id": row["id"], "completed_at": row["ended_at"], "satisfied_by_existing_update": True})
    state.pop("completed_at", None)
    state.pop("satisfied_by_existing_update", None)
    if state.get("attempts", 0) >= 3:
        if pending:
            return store.set_setting("schedule_state", {**state, "status": "catchup_pending", "error": "今日补扫预算已用完，剩余内容将在后续更新继续；也可手动更新"})
        return store.set_setting("schedule_state", {**state, "status": "failed", "error": "Daily retry budget exhausted; manual update remains available"})
    if state.get("last_attempt_at") and now - datetime.fromisoformat(state["last_attempt_at"]) < timedelta(minutes=15):
        return store.set_setting("schedule_state", {**state, "status": "retry_wait"})
    try:
        job = enqueue_update(store, sources_path)
        if job.get("duplicate_trigger") and set(job.get("source_ids", [])) != enabled_ids:
            state.pop("job_id", None)
            return store.set_setting("schedule_state", {**state, "status": "waiting_for_other_update"})
        state.update(job_id=job["id"], status=job["status"], last_attempt_at=now.isoformat(),
                     attempts=state.get("attempts", 0) + (not job.get("duplicate_trigger", False)))
    except (ValueError, OSError) as error:
        state.update(status="failed", error=str(error)[:300], last_attempt_at=now.isoformat(), attempts=state.get("attempts", 0) + 1)
    return store.set_setting("schedule_state", state)


def launch_agent_data(db_path, sources_path, *, label=LABEL, runtime_root=None):
    return {"Label": label, "ProgramArguments": [sys.executable, "-m", "campus_radar", "--db", str(Path(db_path).resolve()),
                "--sources", str(Path(sources_path).resolve()), "schedule-tick"],
            "WorkingDirectory": str(runtime_root or Path(__file__).resolve().parent.parent),
            "EnvironmentVariables": {"CAMPUS_RADAR_HOME": str(Path(db_path).resolve().parent)}, "RunAtLoad": True,
            "StartInterval": 60, "ProcessType": "Background", "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}


def agent_label(db_path):
    database = Path(db_path).resolve()
    original = (Path.home() / "Library/Application Support/CampusRadar/public.sqlite3").resolve()
    # Separate databases need separate launchd names to avoid replacing another profile's job.
    return LABEL if database == original else LABEL + "." + hashlib.sha256(str(database).encode()).hexdigest()[:12]


def verify_agent(path, label, database):
    if path.is_symlink():
        raise ValueError("Refusing a symlinked background configuration")
    if not path.exists():
        return None
    raw = path.read_bytes()
    value = plistlib.loads(raw)
    args = value.get("ProgramArguments", [])
    if (value.get("Label") != label or args[1:4] != ["-m", "campus_radar", "--db"] or
            len(args) < 5 or Path(args[4]).resolve() != database):
        raise ValueError("An existing background configuration belongs to a different target")
    return raw


def install_schedule(store, sources_path):
    if sys.platform != "darwin":
        raise ValueError("Background installation currently supports macOS only")
    update_status(store)
    with update_lock(store) as locked:
        if not locked or update_status(store)["status"] in {"queued", "running"}:
            raise ValueError("Wait for the active update before installing the background runtime")
        target_db = store.path.resolve()
        runtime = target_db.parent / "runtime"
        runtime_sources = runtime / "data/sources.json"
        label = agent_label(target_db)
        path = Path.home() / "Library/LaunchAgents" / (label + ".plist")
        old_plist = verify_agent(path, label, target_db)
        data = launch_agent_data(target_db, runtime_sources, label=label, runtime_root=runtime)
        files = {runtime / "campus_radar" / source.name: source.read_bytes()
                 for source in Path(__file__).resolve().parent.glob("*.py")}
        files[runtime_sources] = Path(sources_path).read_bytes()
        previous = {destination: destination.read_bytes() if destination.exists() else None for destination in files}
        domain = f"gui/{os.getuid()}"
        present = subprocess.run(["launchctl", "print", domain + "/" + label], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if present and old_plist is None:
            raise ValueError("A loaded background job has no verifiable configuration; inspect it before replacing")
        if present:
            subprocess.run(["launchctl", "bootout", domain + "/" + label], check=True, capture_output=True)
        try:
            for destination, content in files.items():
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = destination.with_suffix(destination.suffix + ".tmp")
                temporary.write_bytes(content)
                temporary.replace(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(plistlib.dumps(data))
            path.chmod(0o600)
            subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True, capture_output=True)
        except Exception:
            for destination, content in previous.items():
                if content is None:
                    destination.unlink(missing_ok=True)
                else:
                    destination.write_bytes(content)
            if old_plist is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(old_plist)
                if present:
                    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True, capture_output=True)
            raise
        result = {"label": label, "path": str(path), "installed_at": utc_now(), "tick_seconds": 60,
                  "database": str(target_db), "runtime_root": str(runtime)}
        return store.set_setting("schedule_runtime", result)


def uninstall_schedule(store):
    if sys.platform != "darwin":
        raise ValueError("Background installation currently supports macOS only")
    update_status(store)
    with update_lock(store) as locked:
        if not locked or update_status(store)["status"] in {"queued", "running"}:
            raise ValueError("Wait for the active update before removing its background trigger")
        database = store.path.resolve()
        label = agent_label(database)
        path = Path.home() / "Library/LaunchAgents" / (label + ".plist")
        original = verify_agent(path, label, database)
        domain = f"gui/{os.getuid()}"
        present = subprocess.run(["launchctl", "print", domain + "/" + label], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if present and original is None:
            raise ValueError("A loaded background job has no verifiable configuration; inspect it before removing")
        if present:
            subprocess.run(["launchctl", "bootout", domain + "/" + label], check=True, capture_output=True)
        config = schedule_config(store)
        schedule_config(store, {key: False if key == "enabled" else config[key] for key in ("enabled", "hour", "minute", "timezone")})
        path.unlink(missing_ok=True)
        store.set_setting("schedule_runtime", {})
        return {"removed": label, "database_preserved": str(database)}
