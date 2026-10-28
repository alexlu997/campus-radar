"""Validated business operations shared by the CLI and the DSH adapter."""

from .digest import make_digest, preferences
from .updates import enqueue_update, update_status
from .scheduler import schedule_config
from .discovery import add_candidate, fetch_candidate, search_public


METHODS = {
    "list": {"query", "source_id", "category", "limit", "offset"},
    "events": {"query", "source_id", "category", "campus", "date_from", "date_to", "status", "favorites", "limit", "offset"},
    "detail": {"event_id"}, "favorite": {"event_id", "enabled"}, "feedback": {"event_id", "value"},
    "preferences": set(), "preferences-set": {"themes", "source_ids"},
    "sources": set(), "source-toggle": {"source_id", "enabled"},
    "digest": {"day", "refresh", "limit"}, "digests": set(),
    "update": {"source_ids", "limit", "max_pages"}, "update-status": {"job_id"},
    "schedule": set(), "schedule-set": {"enabled", "hour", "minute", "timezone"},
    "candidates": set(), "candidate-add": {"url"}, "candidate-fetch": {"url"}, "discover-search": {"query", "limit"},
}


def dispatch(store, sources_path, method, payload):
    if method not in METHODS or not isinstance(payload, dict) or set(payload) - METHODS[method]:
        raise ValueError("Unknown operation or fields")
    for key, value in payload.items():
        if key in {"enabled", "favorites", "refresh"}:
            if type(value) is not bool:
                raise ValueError(key + " must be boolean")
        elif key in {"limit", "offset", "max_pages", "hour", "minute"}:
            if type(value) is not int:
                raise ValueError(key + " must be an integer")
        elif key in {"themes", "source_ids"}:
            if not isinstance(value, list) or len(value) > 30 or any(not isinstance(x, str) or not x or len(x) > 100 for x in value):
                raise ValueError(key + " must be a short list of strings")
        elif not isinstance(value, str) or len(value) > 500:
            raise ValueError(key + " must be a short string")
    if method == "list":
        if not 1 <= payload.get("limit", 200) <= 1000 or payload.get("offset", 0) < 0:
            raise ValueError("Invalid pagination")
        return store.listing(**{"limit": 200, **payload})
    if method == "events":
        return store.event_listing(**payload)
    if method == "detail":
        return store.event_detail(payload["event_id"])
    if method == "favorite":
        return store.set_favorite(payload["event_id"], payload["enabled"])
    if method == "feedback":
        return store.set_feedback(payload["event_id"], payload["value"])
    if method == "preferences":
        return preferences(store)
    if method == "preferences-set":
        return preferences(store, payload)
    if method == "sources":
        return {"sources": store.source_states(), "runs": store.listing(limit=0)["runs"],
                "favorites": [r[0] for r in store.db.execute("SELECT event_id FROM favorites")],
                "feedback": dict(store.db.execute("SELECT event_id,value FROM feedback"))}
    if method == "source-toggle":
        return store.set_source_enabled(payload["source_id"], payload["enabled"])
    if method == "digest":
        return make_digest(store, **payload)
    if method == "digests":
        return {"digests": [dict(r) for r in store.db.execute("SELECT day,timezone,generated_at FROM digests ORDER BY day DESC LIMIT 60")]}
    if method == "update":
        return enqueue_update(store, sources_path, **payload)
    if method == "update-status":
        return update_status(store, payload.get("job_id"))
    if method == "schedule":
        return schedule_config(store)
    if method == "schedule-set":
        return schedule_config(store, payload)
    if method == "candidates":
        return {"candidates": [dict(r) for r in store.db.execute("SELECT * FROM candidates ORDER BY discovered_at DESC,url")],
                "last_search": store.setting("discovery_last_search")}
    if method == "candidate-add":
        return add_candidate(store, payload["url"])
    if method == "candidate-fetch":
        return fetch_candidate(store, payload["url"])
    if method == "discover-search":
        return search_public(store, **payload)
