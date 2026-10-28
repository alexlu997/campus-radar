"""Saved daily selections with explicit, source-backed rules."""

from datetime import datetime, timedelta, timezone as dt_timezone
import json
from zoneinfo import ZoneInfo

from .events import changed_fields, instant, status_at
from .storage import encode


def preferences(store, value=None):
    if value is None:
        return store.setting("preferences", {"themes": [], "source_ids": []})
    if not isinstance(value, dict) or set(value) != {"themes", "source_ids"}:
        raise ValueError("Preferences require themes and source_ids")
    known_sources = {s["id"] for s in store.source_states()}
    for key, entries in value.items():
        if not isinstance(entries, list) or len(entries) > 30 or any(not isinstance(x, str) or not x.strip() or len(x) > 100 for x in entries):
            raise ValueError("Preferences need at most 30 nonempty strings per field")
        value[key] = list(dict.fromkeys(x.strip() for x in entries))
    if set(value["source_ids"]) - known_sources:
        raise ValueError("Unknown preferred source")
    return store.set_setting("preferences", value)


def make_digest(store, *, day=None, timezone="Asia/Shanghai", refresh=False, limit=10, now=None):
    zone = ZoneInfo(timezone)
    now = (now or datetime.now(zone)).astimezone(zone)
    today = now.date().isoformat()
    day = day or today
    saved = store.db.execute("SELECT data FROM digests WHERE day=? AND timezone=?", (day, timezone)).fetchone()
    if saved and not refresh:
        return json.loads(saved[0])
    if day != today:
        raise ValueError("Only today's digest can be generated; older days must already be saved")
    if not isinstance(limit, int) or not 1 <= limit <= 30:
        raise ValueError("Digest limit must be 1–30")
    previous = store.db.execute("SELECT generated_at FROM digests WHERE day<? AND timezone=? ORDER BY day DESC LIMIT 1", (day, timezone)).fetchone()
    since = datetime.fromisoformat(previous[0]) if previous else now - timedelta(days=7)
    aliases = dict(store.db.execute("SELECT id,event_id FROM event_aliases"))
    selected_before, selected_today = set(), set()
    for row in store.db.execute("SELECT day,data FROM digests WHERE day<=? AND timezone=?", (day, timezone)):
        snapshot = json.loads(row["data"])
        ids = snapshot.get("selected_event_ids", [item["event"]["id"] for item in snapshot["items"]])
        resolved = {aliases.get(event_id, event_id) for event_id in ids}
        if row["day"] == day:
            selected_today.update(resolved)
        else:
            selected_before.update(resolved)
    prefs = preferences(store)
    documents = {d["id"]: d for d in store.listing(limit=-1)["documents"]}
    changes = {}
    for row in store.db.execute("SELECT * FROM event_changes WHERE changed_at>?", (since.astimezone(dt_timezone.utc).isoformat(),)):
        fields = changed_fields(json.loads(row["before"]), json.loads(row["after"]))
        if fields:
            changes.setdefault(row["event_id"], set()).update(fields)
    saved_ids = {r[0] for r in store.db.execute("SELECT event_id FROM favorites")}
    feedback = dict(store.db.execute("SELECT event_id,value FROM feedback"))
    candidates = []
    for row in store.db.execute("SELECT id,data FROM events"):
        event = json.loads(row["data"])
        event["status"] = status_at(event, now)
        event["favorite"] = row["id"] in saved_ids
        event["feedback"] = feedback.get(row["id"])
        related = [documents[d] for d in event["source_document_ids"] if d in documents]
        text = (event["title"] + "\n" + "\n".join(d["body"] for d in related)).casefold()
        themes = [t for t in prefs["themes"] if t.casefold() in text]
        followed = [s["source_name"] for s in event["sources"] if s["source_id"] in prefs["source_ids"]]
        preferred = bool(themes or followed or event["favorite"] or event["feedback"] == "interested")
        changed = event["id"] in changes
        reasons = []
        if changed:
            labels = {"title": "标题", "starts_at": "开始时间", "ends_at": "结束时间", "registration_deadline": "截止时间",
                      "location": "地点", "campus": "校区", "eligibility": "参加资格", "status": "状态", "archived": "最新原文识别结果"}
            reasons.append("、".join(labels[k] for k in sorted(changes[event["id"]])) + "有变更，请核对原文")
        if event["feedback"] == "not_interested" and not (changed and event["favorite"]):
            continue
        closed = event["status"] in {"past", "deadline_passed", "recap", "cancelled", "postponed", "started_end_unknown", "needs_review"}
        if closed and not changed:
            continue
        deadline = instant(event.get("registration_deadline"), end=True)
        upcoming_deadline = deadline and now <= deadline <= now + timedelta(days=7)
        if upcoming_deadline and not closed:
            reasons.append("7 天内截止：" + event["title"])
        new = event["id"] not in selected_before and event["feedback"] != "known"
        if new and not closed:
            reasons.append("首次入选简报（含此前未入选的本地发现）")
        if not reasons:
            continue
        if themes:
            reasons.append("关注主题：" + "、".join(themes))
        if followed:
            reasons.append("关注来源：" + "、".join(dict.fromkeys(followed)))
        if event["favorite"]:
            reasons.append("已收藏")
        if event["status"] == "unknown":
            reasons.append("时间或条件待核对")
        explore = bool(prefs["themes"] or prefs["source_ids"]) and not preferred and not changed and not upcoming_deadline
        section = "changes" if changed else "deadlines" if upcoming_deadline else "explore" if explore else "new"
        if explore:
            reasons.append("探索：当前关注主题与来源以外的内容")
        score = 100 * changed + 60 * bool(upcoming_deadline) + 20 * preferred + 10 * event["favorite"]
        candidates.append({"event": event, "section": section, "reasons": reasons, "score": score})
    candidates.sort(key=lambda c: (c["score"], c["event"]["first_seen_at"], c["event"]["id"]), reverse=True)
    critical = sum(c["section"] in {"changes", "deadlines"} for c in candidates)
    exploration = [c for c in candidates if c["section"] == "explore"][:min(max(1, limit // 5), max(0, limit - critical))]
    selected = [c for c in candidates if c["section"] != "explore"][:limit - len(exploration)] + exploration
    for item in selected:
        item.pop("score")
    result = {"day": day, "timezone": timezone, "generated_at": now.isoformat(), "since": since.isoformat(),
              "items": selected, "candidate_count": len(candidates), "limit": limit,
              "selected_event_ids": sorted(selected_today | {item["event"]["id"] for item in selected}),
              "rules": "明确变更优先，7天内截止次之；未入选且仍有效的内容顺延，关注与收藏加权；探索最多20%（至少一条，有候选才选）；不补足条数。",
              "source_errors": [{"id": s["id"], "name": s["name"], "last_error": s["last_error"]}
                                for s in store.enabled_sources() if s.get("last_error")]}
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO digests VALUES(?,?,?,?)", (day, timezone, now.isoformat(), encode(result)))
    return result


def markdown_digest(digest):
    lines = [f"# 校园信息简报 · {digest['day']}", "", f"生成时间：{digest['generated_at']}；共 {len(digest['items'])} 条。", ""]
    labels = {"changes": "重要变更", "deadlines": "临近截止", "new": "新发现", "explore": "探索"}
    for item in digest["items"]:
        event = item["event"]
        lines.extend([f"## {labels[item['section']]} · {event['title']}", "", "；".join(item["reasons"]),
                      f"状态：{event['status']}；活动时间：{event.get('starts_at') or '未知'} — {event.get('ends_at') or '未知'}",
                      f"截止：{event.get('registration_deadline') or '未知'}；地点：{event.get('location') or '未知'}",
                      f"原文受众：{event.get('audience') or '未知'}；完整参加资格：{event.get('eligibility') or '未知'}"])
        lines.extend(f"- [{s['source_name']}]({s['url']})" for s in event["sources"])
        lines.append("")
    if not digest["items"]:
        lines.append("当前没有符合规则的新条目；完整列表仍可查看。")
    if digest["source_errors"]:
        lines.append("来源更新存在失败，历史数据保留：" + "、".join(s["name"] for s in digest["source_errors"]))
    return "\n".join(lines) + "\n"
