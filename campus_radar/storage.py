"""SQLite records for public sources, documents, opportunities and collection runs."""

from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .events import changed_fields, same_event, stable_event, status_at, title_key


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_db():
    root = Path(os.environ.get("CAMPUS_RADAR_HOME", str(Path.home() / "Library/Application Support/CampusRadar"))).expanduser()
    return root / "public.sqlite3"


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, metadata TEXT NOT NULL,
                last_success_at TEXT, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
                url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, body TEXT NOT NULL,
                published_at TEXT, first_seen_at TEXT NOT NULL, fetched_at TEXT NOT NULL,
                fingerprint TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'public',
                processing_status TEXT NOT NULL, evidence TEXT NOT NULL,
                history TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
                started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL,
                page_count INTEGER NOT NULL DEFAULT 0, document_count INTEGER NOT NULL DEFAULT 0,
                inserted_count INTEGER NOT NULL DEFAULT 0, updated_count INTEGER NOT NULL DEFAULT 0,
                errors TEXT NOT NULL DEFAULT '[]', coverage TEXT NOT NULL DEFAULT '{}'
            );
        """)
        self._migrate_events()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS event_sources (
                document_id TEXT NOT NULL REFERENCES documents(id), local_key TEXT NOT NULL,
                event_id TEXT NOT NULL REFERENCES events(id), data TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(document_id,local_key)
            );
            CREATE INDEX IF NOT EXISTS event_sources_event ON event_sources(event_id);
            CREATE TABLE IF NOT EXISTS event_aliases (id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id));
            CREATE TABLE IF NOT EXISTS favorites (event_id TEXT PRIMARY KEY REFERENCES events(id), saved_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS feedback (event_id TEXT PRIMARY KEY REFERENCES events(id), value TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS event_changes (
                id INTEGER PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id), changed_at TEXT NOT NULL,
                before TEXT NOT NULL, after TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS digests (
                day TEXT NOT NULL, timezone TEXT NOT NULL, generated_at TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(day,timezone)
            );
            CREATE TABLE IF NOT EXISTS updates (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL,
                pid INTEGER, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candidates (
                url TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL,
                discovered_at TEXT NOT NULL, status TEXT NOT NULL, error TEXT, document_id TEXT REFERENCES documents(id)
            );
        """)
        with self.db:
            for row in self.db.execute("SELECT id,data FROM events").fetchall():
                if "sources" not in json.loads(row["data"]):
                    self._refresh_event(row["id"], record_change=False)
        self.path.chmod(0o600)

    def _migrate_events(self):
        if "document_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(events)")}:
            return
        backup = self.path.with_suffix(self.path.suffix + ".v1-backup")
        if not backup.exists():
            with sqlite3.connect(backup) as target:
                self.db.backup(target)
            backup.chmod(0o600)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if "document_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(events)")}:
                return
            old = self.db.execute("SELECT * FROM events").fetchall()
            self.db.execute("CREATE TABLE events_new (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            self.db.executemany("INSERT INTO events_new VALUES(?,?)", [(r["id"], r["data"]) for r in old])
            self.db.execute("DROP TABLE events")
            self.db.execute("ALTER TABLE events_new RENAME TO events")
            self.db.execute("""CREATE TABLE event_sources (
                document_id TEXT NOT NULL REFERENCES documents(id), local_key TEXT NOT NULL,
                event_id TEXT NOT NULL REFERENCES events(id), data TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(document_id,local_key))""")
            self.db.executemany("INSERT INTO event_sources VALUES(?,'main',?,?,1)",
                                [(r["document_id"], r["id"], encode(stable_event(json.loads(r["data"])))) for r in old])
            self.db.execute("PRAGMA user_version=2")

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is not None:
            self.db.rollback()
        self.db.close()

    def sync_sources(self, sources):
        overrides = self.setting("source_enabled", {})
        for source in sources:
            old = self.db.execute("SELECT metadata FROM sources WHERE id=?", (source["id"],)).fetchone()
            if source.get("optional") and old and json.loads(old[0]).get("enabled") and source["id"] not in overrides:
                overrides[source["id"]] = True
            self.db.execute("""
                INSERT INTO sources(id,name,metadata) VALUES(?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,metadata=excluded.metadata
            """, (source["id"], source["name"], encode(source)))
        self.set_setting("source_enabled", overrides)

    def start_run(self, source_id):
        cursor = self.db.execute(
            "INSERT INTO runs(source_id,started_at,status) VALUES(?,?,'running')",
            (source_id, utc_now()),
        )
        self.db.commit()
        return cursor.lastrowid

    def finish_run(self, run_id, status, *, pages, documents, inserted, updated, errors, coverage):
        ended = utc_now()
        self.db.execute("""
            UPDATE runs SET ended_at=?,status=?,page_count=?,document_count=?,
            inserted_count=?,updated_count=?,errors=?,coverage=? WHERE id=?
        """, (ended, status, pages, documents, inserted, updated, encode(errors), encode(coverage), run_id))
        source_id = self.db.execute("SELECT source_id FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        if status == "success":
            self.db.execute("UPDATE sources SET last_success_at=?,last_error=NULL WHERE id=?", (ended, source_id))
        else:
            self.db.execute("UPDATE sources SET last_error=? WHERE id=?", (encode(errors), source_id))
        self.db.commit()

    def save_document(self, source_id, document):
        if document.get("visibility", "public") != "public":
            raise ValueError("The public collection database does not accept authenticated content")
        now = utc_now()
        doc_id = hashlib.sha256(document["url"].encode()).hexdigest()[:24]
        event_values = document.get("events", [document["event"]] if document.get("event") else [])
        if not isinstance(event_values, list) or any(not isinstance(e, dict) or not e.get("title") for e in event_values):
            raise ValueError("Events must be a list of titled records")
        local_keys = [e.get("key") or ("main" if i == 0 else title_key(e["title"])) for i, e in enumerate(event_values)]
        if len(local_keys) != len(set(local_keys)):
            raise ValueError("Events in one document need distinct keys")
        fingerprint = hashlib.sha256(encode({**{key: document.get(key) for key in (
            "title", "body", "published_at", "processing_status", "evidence"
        )}, "events": [stable_event(e) for e in event_values]}).encode()).hexdigest()
        old = self.db.execute("SELECT * FROM documents WHERE url=?", (document["url"],)).fetchone()
        history = json.loads(old["history"]) if old else []
        changed = old is not None and old["fingerprint"] != fingerprint
        if changed:
            previous_events = [json.loads(r[0]) for r in self.db.execute(
                "SELECT data FROM event_sources WHERE document_id=? AND active=1 ORDER BY local_key<>'main',local_key", (doc_id,))]
            history.append({
                "fetched_at": old["fetched_at"], "title": old["title"], "body": old["body"],
                "published_at": old["published_at"], "fingerprint": old["fingerprint"],
                "evidence": json.loads(old["evidence"]),
                "event": previous_events[0] if previous_events else None, "events": previous_events,
            })
        self.db.execute("""
            INSERT INTO documents(id,source_id,url,title,body,published_at,first_seen_at,
                fetched_at,fingerprint,visibility,processing_status,evidence,history)
            VALUES(?,?,?,?,?,?,?,?,?,'public',?,?,?)
            ON CONFLICT(url) DO UPDATE SET title=excluded.title,body=excluded.body,
                published_at=excluded.published_at,fetched_at=excluded.fetched_at,
                fingerprint=excluded.fingerprint,processing_status=excluded.processing_status,
                evidence=excluded.evidence,history=excluded.history
        """, (doc_id, source_id, document["url"], document["title"], document["body"],
              document.get("published_at"), old["first_seen_at"] if old else now, now,
              fingerprint, document["processing_status"], encode(document.get("evidence", {})), encode(history)))
        touched = {r[0] for r in self.db.execute("SELECT event_id FROM event_sources WHERE document_id=?", (doc_id,))}
        self.db.execute("UPDATE event_sources SET active=0 WHERE document_id=?", (doc_id,))
        for key, value in zip(local_keys, event_values):
            member = self.db.execute("SELECT event_id FROM event_sources WHERE document_id=? AND local_key=?", (doc_id, key)).fetchone()
            event_id = member[0] if member else None
            match = self.db.execute("SELECT id,data FROM events ORDER BY id").fetchall()
            matches = [r["id"] for r in match if r["id"] != event_id
                       and doc_id not in json.loads(r["data"]).get("source_document_ids", [])
                       and same_event(value, json.loads(r["data"]))]
            if not event_id:
                event_id = matches[0] if len(matches) == 1 else doc_id + ("-event" if key == "main" else "-" + uuid.uuid4().hex[:12])
                self.db.execute("INSERT OR IGNORE INTO events VALUES(?,?)", (event_id, "{}"))
            self.db.execute("""INSERT INTO event_sources VALUES(?,?,?,?,1)
                ON CONFLICT(document_id,local_key) DO UPDATE SET data=excluded.data,active=1""",
                (doc_id, key, event_id, encode(stable_event(value))))
            touched.add(event_id)
        for event_id in touched:
            self._refresh_event(event_id, now)
        self.db.commit()
        for event_id in touched:
            event_id = self.resolve_event(event_id)
            current = json.loads(self.db.execute("SELECT data FROM events WHERE id=?", (event_id,)).fetchone()[0])
            candidates = [json.loads(r[0]) for r in self.db.execute("SELECT data FROM events WHERE id<>?", (event_id,))]
            matches = [e for e in candidates if not e.get("archived") and not current.get("archived")
                       and not set(e["source_document_ids"]) & set(current["source_document_ids"]) and same_event(current, e)]
            if len(matches) == 1:
                keep, remove = sorted([current, matches[0]], key=lambda e: (e["first_seen_at"], e["id"]))
                self.merge_events(keep["id"], remove["id"])
        return "inserted" if old is None else "updated" if changed else "unchanged"

    def _refresh_event(self, event_id, now=None, *, record_change=True):
        rows = self.db.execute("""SELECT es.*,d.first_seen_at,d.source_id,d.url,d.title AS document_title,
            s.name AS source_name FROM event_sources es JOIN documents d ON d.id=es.document_id
            JOIN sources s ON s.id=d.source_id WHERE es.event_id=? ORDER BY d.first_seen_at,d.id,es.local_key""", (event_id,)).fetchall()
        if not rows:
            return
        active = [r for r in rows if r["active"]]
        values = [json.loads(r["data"]) for r in active or rows]
        data = dict(values[0])
        data.update(id=event_id, visibility="public", first_seen_at=min(r["first_seen_at"] for r in rows),
                    source_document_ids=list(dict.fromkeys(r["document_id"] for r in rows)),
                    archived=not bool(active), conflicts={}, field_evidence={})
        data["sources"] = [{k: r[k] for k in ("document_id", "source_id", "source_name", "url", "document_title", "active")} for r in rows]
        fields = ("starts_at", "ends_at", "registration_deadline", "location", "campus", "eligibility", "audience", "registration_url", "organizer", "status")
        for field in fields:
            options = []
            for row, value in zip(active or rows, values):
                option = value.get(field)
                if option is not None:
                    options.append({"value": option, "url": row["url"], "evidence": value.get("field_evidence", {}).get(field)})
            distinct = {title_key(o["value"]) if field in {"location", "campus"} and isinstance(o["value"], str)
                        else encode(o["value"]) for o in options}
            if len(distinct) > 1:
                data["conflicts"][field] = options
                data[field] = "unknown" if field == "status" else None
            elif options:
                data[field] = options[0]["value"]
            data["field_evidence"][field] = options
        previous = json.loads(self.db.execute("SELECT data FROM events WHERE id=?", (event_id,)).fetchone()[0])
        if record_change and previous and changed_fields(previous, data):
            self.db.execute("INSERT INTO event_changes(event_id,changed_at,before,after) VALUES(?,?,?,?)",
                            (event_id, now or utc_now(), encode(previous), encode(data)))
        self.db.execute("UPDATE events SET data=? WHERE id=?", (encode(data), event_id))

    def resolve_event(self, event_id):
        alias = self.db.execute("SELECT event_id FROM event_aliases WHERE id=?", (event_id,)).fetchone()
        event_id = alias[0] if alias else event_id
        if not self.db.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone():
            raise ValueError("Unknown event ID")
        return event_id

    def merge_events(self, keep, remove):
        keep, remove = self.resolve_event(keep), self.resolve_event(remove)
        if keep == remove:
            return keep
        with self.db:
            self.db.execute("UPDATE event_sources SET event_id=? WHERE event_id=?", (keep, remove))
            self.db.execute("INSERT OR IGNORE INTO favorites SELECT ?,saved_at FROM favorites WHERE event_id=?", (keep, remove))
            latest = self.db.execute("SELECT value,updated_at FROM feedback WHERE event_id IN (?,?) ORDER BY updated_at DESC LIMIT 1", (keep, remove)).fetchone()
            if latest:
                self.db.execute("INSERT OR REPLACE INTO feedback VALUES(?,?,?)", (keep, *latest))
            for table in ("favorites", "feedback"):
                self.db.execute(f"DELETE FROM {table} WHERE event_id=?", (remove,))
            self.db.execute("UPDATE event_changes SET event_id=? WHERE event_id=?", (keep, remove))
            self.db.execute("UPDATE event_aliases SET event_id=? WHERE event_id=?", (keep, remove))
            self.db.execute("INSERT OR REPLACE INTO event_aliases VALUES(?,?)", (remove, keep))
            self.db.execute("DELETE FROM events WHERE id=?", (remove,))
            self._refresh_event(keep)
        return keep

    def set_favorite(self, event_id, enabled):
        event_id = self.resolve_event(event_id)
        with self.db:
            if enabled:
                self.db.execute("INSERT OR IGNORE INTO favorites VALUES(?,?)", (event_id, utc_now()))
            else:
                self.db.execute("DELETE FROM favorites WHERE event_id=?", (event_id,))
        return {"event_id": event_id, "favorite": enabled}

    def set_feedback(self, event_id, value):
        if value not in {"interested", "not_interested", "known", "clear"}:
            raise ValueError("Invalid feedback")
        event_id = self.resolve_event(event_id)
        with self.db:
            if value == "clear":
                self.db.execute("DELETE FROM feedback WHERE event_id=?", (event_id,))
            else:
                self.db.execute("INSERT OR REPLACE INTO feedback VALUES(?,?,?)", (event_id, value, utc_now()))
        return {"event_id": event_id, "feedback": value}

    def setting(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, encode(value)))
        return value

    def event_listing(self, *, query=None, source_id=None, category=None, campus=None,
                      date_from=None, date_to=None, status=None, favorites=False, limit=50, offset=0, now=None):
        if not isinstance(limit, int) or not 1 <= limit <= 1000 or not isinstance(offset, int) or offset < 0:
            raise ValueError("Invalid pagination")
        for value in (date_from, date_to):
            if value and (len(value) != 10 or date.fromisoformat(value).isoformat() != value):
                raise ValueError("Dates must use YYYY-MM-DD")
        if date_from and date_to and date_from > date_to:
            raise ValueError("Date range is reversed")
        docs = {r["id"]: dict(r) for r in self.db.execute("""SELECT d.id,d.title,d.body,d.url,d.source_id,s.metadata
            FROM documents d JOIN sources s ON s.id=d.source_id WHERE d.visibility='public'""")}
        saved = {r[0] for r in self.db.execute("SELECT event_id FROM favorites")}
        feedback = dict(self.db.execute("SELECT event_id,value FROM feedback"))
        events = []
        for row in self.db.execute("SELECT * FROM events"):
            event = json.loads(row["data"])
            event["id"] = row["id"]
            event["status"] = status_at(event, now)
            event["favorite"] = row["id"] in saved
            event["feedback"] = feedback.get(row["id"])
            related = [docs[d] for d in event.get("source_document_ids", []) if d in docs]
            if query and query.casefold() not in (event.get("title", "") + "\n" + "\n".join(d["title"] + "\n" + d["body"] for d in related)).casefold():
                continue
            if source_id and not any(d["source_id"] == source_id for d in related):
                continue
            categories = {event.get("category")}
            for doc in related:
                meta = json.loads(doc["metadata"])
                categories.update([meta.get("category"), *meta.get("categories", [])])
            if category and category not in categories:
                continue
            if campus and (event.get("campus") != campus if campus != "unknown" else event.get("campus") is not None):
                continue
            if status and event["status"] != status:
                continue
            if favorites and not event["favorite"]:
                continue
            start = event.get("registration_deadline") if event.get("kind") == "deadline" else event.get("starts_at")
            end = event.get("ends_at") or start
            if (date_from or date_to) and not start:
                continue
            if date_from and end[:10] < date_from or date_to and start[:10] > date_to:
                continue
            events.append(event)
        events.sort(key=lambda e: (e["first_seen_at"], e["id"]), reverse=True)
        return {"events": events[offset:offset + limit], "total": len(events), "offset": offset,
                "next_offset": offset + limit if offset + limit < len(events) else None}

    def event_detail(self, event_id, *, now=None):
        event_id = self.resolve_event(event_id)
        event = json.loads(self.db.execute("SELECT data FROM events WHERE id=?", (event_id,)).fetchone()[0])
        event["status"] = status_at(event, now)
        event["favorite"] = bool(self.db.execute("SELECT 1 FROM favorites WHERE event_id=?", (event_id,)).fetchone())
        feedback = self.db.execute("SELECT value FROM feedback WHERE event_id=?", (event_id,)).fetchone()
        event["feedback"] = feedback[0] if feedback else None
        event["documents"] = [d for d in self.listing(limit=-1)["documents"] if d["id"] in event["source_document_ids"]]
        event["changes"] = [{"changed_at": r["changed_at"], "before": json.loads(r["before"]), "after": json.loads(r["after"])}
                            for r in self.db.execute("SELECT * FROM event_changes WHERE event_id=? ORDER BY id DESC", (event_id,))]
        return event

    def source_states(self):
        return self.listing(limit=0)["sources"]

    def enabled_sources(self, source_ids=None):
        selected = []
        overrides = self.setting("source_enabled", {})
        states = self.source_states()
        if source_ids and set(source_ids) - {s["id"] for s in states}:
            raise ValueError("Unknown source ID")
        for source in states:
            source["enabled"] = overrides.get(source["id"], source.get("enabled", False))
            if source_ids and source["id"] not in source_ids:
                continue
            if source["enabled"]:
                selected.append(source)
            elif source_ids:
                raise ValueError("Source is disabled")
        return selected

    def set_source_enabled(self, source_id, enabled):
        from .collector import SUPPORTED_ADAPTERS
        source = next((s for s in self.source_states() if s["id"] == source_id), None)
        if not source:
            raise ValueError("Unknown source ID")
        if enabled and (source.get("visibility") != "public" or source.get("adapter") not in SUPPORTED_ADAPTERS):
            raise ValueError("Only supported public adapters can be enabled")
        settings = self.setting("source_enabled", {})
        settings[source_id] = enabled
        self.set_setting("source_enabled", settings)
        return {"source_id": source_id, "enabled": enabled}

    def listing(self, *, limit=50, offset=0, source_id=None, query=None, category=None):
        conditions, parameters = ["d.visibility='public'"], []
        if source_id:
            conditions.append("d.source_id=?")
            parameters.append(source_id)
        if query:
            conditions.append("(instr(lower(d.title),lower(?))>0 OR instr(lower(d.body),lower(?))>0)")
            parameters.extend([query, query])
        if category:
            conditions.append("(json_extract(s.metadata,'$.category')=? OR EXISTS (SELECT 1 FROM json_each(s.metadata,'$.categories') WHERE value=?))")
            parameters.extend([category, category])
        total = self.db.execute("SELECT COUNT(*) FROM documents d JOIN sources s ON s.id=d.source_id WHERE " + " AND ".join(conditions), parameters).fetchone()[0]
        parameters.extend([limit, offset])
        rows = self.db.execute("""
            SELECT d.*,s.name AS source_name FROM documents d
            JOIN sources s ON s.id=d.source_id
            WHERE """ + " AND ".join(conditions) + " ORDER BY d.first_seen_at DESC,d.id LIMIT ? OFFSET ?", parameters)
        documents = []
        for row in rows:
            record = dict(row)
            for key in ("evidence", "history"):
                record[key] = json.loads(record[key]) if record[key] else None
            record["events"] = []
            for member in self.db.execute("SELECT event_id,data FROM event_sources WHERE document_id=? AND active=1 ORDER BY local_key<>'main',local_key", (record["id"],)):
                event = json.loads(member["data"])
                event.update(id=member["event_id"], first_seen_at=record["first_seen_at"], source_document_ids=[record["id"]], visibility="public")
                event["status"] = status_at(event)
                record["events"].append(event)
            record["event"] = record["events"][0] if record["events"] else None
            documents.append(record)
        sources = []
        for row in self.db.execute("SELECT * FROM sources ORDER BY id"):
            source = json.loads(row["metadata"])
            source["last_success_at"] = row["last_success_at"]
            if row["last_error"] is not None or row["last_success_at"] is not None:
                source["last_error"] = row["last_error"]
            sources.append(source)
        overrides = self.setting("source_enabled", {})
        for source in sources:
            source["enabled"] = overrides.get(source["id"], source.get("enabled", False))
        runs = []
        for row in self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 30"):
            run = dict(row)
            for key in ("errors", "coverage"):
                run[key] = json.loads(run[key])
            runs.append(run)
        return {"documents": documents, "sources": sources, "runs": runs, "total": total, "offset": offset,
                "next_offset": offset + limit if limit > 0 and offset + limit < total else None}
