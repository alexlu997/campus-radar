"""Conservative event identity and time-dependent display rules."""

from datetime import datetime, time
import re
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
EXPLICIT_STATUSES = {"cancelled", "postponed", "recap"}
CHANGE_FIELDS = ("title", "starts_at", "ends_at", "registration_deadline", "location", "campus", "eligibility", "status", "archived")


def changed_fields(before, after):
    def values(record, field):
        variants = record.get("conflicts", {}).get(field)
        found = [v["value"] for v in variants] if variants else [record.get(field)]
        return {title_key(v) if field in {"location", "campus"} and isinstance(v, str)
                else re.sub(r"\s+", " ", v).strip() if isinstance(v, str) else v for v in found}
    return [field for field in CHANGE_FIELDS if values(before, field) != values(after, field)]


def title_key(value):
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def subject_keys(title):
    main = re.split(r"\s+[—–|｜]\s+", title, maxsplit=1)[0]
    variants = [title, main]
    parts = re.split(r"[:：]", main, maxsplit=1)
    if len(parts) == 2 and re.search(r"讲坛|讲堂|工作坊|训练营|提升营|论坛", parts[0]):
        variants.append(parts[1])
    return {key for value in variants if len(key := title_key(value)) >= 8}


def instant(value, *, end=False):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if len(value) == 10:
        parsed = datetime.combine(parsed.date(), time.max if end else time.min)
    return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed


def status_at(event, now=None):
    now = now or datetime.now(SHANGHAI)
    status = event.get("status", "unknown")
    if status in EXPLICIT_STATUSES:
        return status
    if event.get("archived") or event.get("conflicts"):
        return "needs_review"
    if event.get("kind") == "deadline":
        deadline = instant(event.get("registration_deadline"), end=True)
        return "deadline_passed" if deadline and deadline < now else "deadline_open" if deadline else "unknown"
    start, end = instant(event.get("starts_at")), instant(event.get("ends_at"), end=True)
    if end and end < now:
        return "past"
    if start and start < now:
        return "ongoing" if end else "started_end_unknown"
    return "scheduled_registration_unknown" if start else "unknown"


def same_event(left, right):
    """Only merge a dated, fully matching title; ambiguous matches remain separate."""
    field = "registration_deadline" if left.get("kind") == "deadline" else "starts_at"
    start = left.get(field)
    if (not subject_keys(left.get("title", "")) & subject_keys(right.get("title", "")) or not start
            or start != right.get(field) or left.get("kind", "activity") != right.get("kind", "activity")):
        return False
    a, b = left.get("location"), right.get("location")
    if a and b and title_key(a) != title_key(b):
        return False
    # shortcut: date-only notices require the same place; broader matching needs reviewed examples.
    return len(start) > 10 or bool(a and b and title_key(a) == title_key(b))


def stable_event(event):
    event = dict(event)
    event.setdefault("kind", "activity")
    if event.get("status") not in EXPLICIT_STATUSES:
        event["status"] = "unknown"
    for key in ("id", "source_document_ids", "first_seen_at", "visibility"):
        event.pop(key, None)
    return event
