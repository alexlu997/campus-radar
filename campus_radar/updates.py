"""One bounded update worker per database, independent of the DSH process."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from .collector import CollectionError, Fetcher, collect_source
from .digest import make_digest
from .storage import encode, utc_now


@contextmanager
def update_lock(store, *, blocking=False):
    path = store.path.with_suffix(store.path.suffix + ".update.lock")
    with path.open("a") as lock:
        path.chmod(0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def update_status(store, job_id=None):
    row = store.db.execute("SELECT * FROM updates WHERE id=?", (job_id,)).fetchone() if job_id else store.db.execute("SELECT * FROM updates ORDER BY started_at DESC,rowid DESC LIMIT 1").fetchone()
    if not row:
        return {"status": "idle"}
    if row["status"] in {"queued", "running"} and not alive(row["pid"]):
        with update_lock(store) as locked:
            if locked:
                store.db.execute("UPDATE updates SET status='interrupted',ended_at=? WHERE id=? AND status IN ('queued','running')", (utc_now(), row["id"]))
                store.db.execute("UPDATE runs SET status='interrupted',ended_at=?,errors=? WHERE status='running'",
                    (utc_now(), encode([{"error": "Update worker exited; old documents are retained"}])))
                store.db.commit()
                row = store.db.execute("SELECT * FROM updates WHERE id=?", (row["id"],)).fetchone()
    result = dict(row)
    result.update(json.loads(result.pop("data")))
    result.pop("pid")
    return result


def _save_job(store, job_id, status, data, *, ended=False):
    store.db.execute("UPDATE updates SET status=?,data=?,ended_at=? WHERE id=?", (status, encode(data), utc_now() if ended else None, job_id))
    store.db.commit()


def enqueue_update(store, sources_path, *, source_ids=None, limit=12, max_pages=2):
    if type(limit) is not int or not 1 <= limit <= 1000 or type(max_pages) is not int or not 1 <= max_pages <= 20:
        raise ValueError("Invalid update budget")
    selected = store.enabled_sources(source_ids)
    if not selected:
        raise ValueError("No enabled supported sources")
    with update_lock(store) as locked:
        if not locked:
            return {**update_status(store), "duplicate_trigger": True}
        running = store.db.execute("SELECT id,pid FROM updates WHERE status IN ('queued','running') ORDER BY rowid DESC LIMIT 1").fetchone()
        if running and alive(running["pid"]):
            return {**update_status(store, running["id"]), "duplicate_trigger": True}
        if running:
            store.db.execute("UPDATE updates SET status='interrupted',ended_at=? WHERE status IN ('queued','running')", (utc_now(),))
            store.db.execute("UPDATE runs SET status='interrupted',ended_at=?,errors=? WHERE status='running'",
                             (utc_now(), encode([{"error": "Worker exited before recording completion; previous documents retained"}])))
        job_id = uuid.uuid4().hex
        data = {"source_ids": [s["id"] for s in selected], "limit": limit, "max_pages": max_pages,
                "results": [], "completed_sources": 0, "total_sources": len(selected), "current_source": None}
        store.db.execute("INSERT INTO updates VALUES(?,?,NULL,'queued',NULL,?)", (job_id, utc_now(), encode(data)))
        store.db.commit()
        try:
            process = subprocess.Popen([sys.executable, "-m", "campus_radar", "--db", str(store.path.resolve()),
                "--sources", str(Path(sources_path).resolve()), "update-worker", "--id", job_id],
                cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            _save_job(store, job_id, "failed", {**data, "error": "Could not start update worker"}, ended=True)
            raise
        store.db.execute("UPDATE updates SET pid=? WHERE id=?", (process.pid, job_id))
        store.db.commit()
        return update_status(store, job_id)


def run_update(store, job_id, *, fetcher=None):
    with update_lock(store, blocking=True):
        row = store.db.execute("SELECT * FROM updates WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "queued":
            return update_status(store, job_id)
        data = json.loads(row["data"])
        store.db.execute("UPDATE updates SET pid=? WHERE id=?", (os.getpid(), job_id))
        _save_job(store, job_id, "running", data)
        try:
            sources = store.enabled_sources(data["source_ids"])
            direct_hosts = {host for s in sources if s.get("network_mode") == "direct" for host in s.get("allowed_hosts", [])}
            fetcher = fetcher or Fetcher(store.path.parent / "public-page-cache", cache_ttl=0, direct_hosts=direct_hosts)
            for source in sources:
                data["current_source"] = source["id"]
                _save_job(store, job_id, "running", data)
                try:
                    result = collect_source(store, source, fetcher, limit=data["limit"], max_pages=data["max_pages"])
                except (CollectionError, OSError, ValueError) as error:
                    store.db.rollback()
                    result = {"source_id": source["id"], "status": "failed", "errors": [{"error": str(error)[:300]}]}
                    store.db.execute("UPDATE sources SET last_error=? WHERE id=?", (encode(result["errors"]), source["id"]))
                    store.db.execute("UPDATE runs SET status='failed',ended_at=?,errors=? WHERE source_id=? AND status='running'",
                                     (utc_now(), encode(result["errors"]), source["id"]))
                data["results"].append(result)
                data["completed_sources"] += 1
                _save_job(store, job_id, "running", data)
            successful = sum(r["status"] == "success" for r in data["results"])
            status = "success" if successful == len(sources) else "partial" if any(r.get("documents", 0) for r in data["results"]) else "failed"
            data["current_source"] = None
            data["inserted"] = sum(r.get("inserted", 0) for r in data["results"])
            data["updated"] = sum(r.get("updated", 0) for r in data["results"])
            data["unchanged"] = sum(r.get("documents", 0) - r.get("inserted", 0) - r.get("updated", 0) for r in data["results"])
            data["collection_pending"] = any(r.get("coverage", {}).get("collection_pending", False) for r in data["results"])
            data["digest"] = {k: v for k, v in make_digest(store, refresh=True).items() if k in {"day", "generated_at"}}
            _save_job(store, job_id, status, data, ended=True)
        except Exception as error:
            store.db.rollback()
            data["error"] = f"{type(error).__name__}: {str(error)[:300]}"
            _save_job(store, job_id, "failed", data, ended=True)
        return update_status(store, job_id)
