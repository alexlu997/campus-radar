"""Public-source CLI. Authenticated browsing has its own explicit auth command."""

import argparse
import json
from pathlib import Path
import sys
import time

from .collector import CollectionError, Fetcher, discover, parse_listing
from .storage import Store, default_db
from .digest import make_digest, markdown_digest
from .service import METHODS, dispatch
from .updates import enqueue_update, run_update, update_status
from .scheduler import install_schedule, tick, uninstall_schedule


def positive(value):
    number = int(value)
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError("must be between 1 and 1000")
    return number


def load_sources(path):
    value = json.loads(Path(path).read_text())
    sources = value["sources"] if isinstance(value, dict) else value
    if not isinstance(sources, list) or len({s["id"] for s in sources}) != len(sources):
        raise ValueError("Sources must be a list with unique IDs")
    return sources


def parser():
    cli = argparse.ArgumentParser(description="Campus Radar: public information collection")
    cli.add_argument("--db", type=Path, default=default_db())
    cli.add_argument("--sources", type=Path, default=Path(__file__).resolve().parent.parent / "data/sources.json")
    commands = cli.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--source", action="append", help="Source ID; repeat for several sources")
    collect.add_argument("--limit", type=positive, default=5, help="Maximum attempted documents per source")
    collect.add_argument("--max-pages", type=positive, default=2)
    collect.add_argument("--refresh", action="store_true", help="Bypass the one-hour successful-page cache")
    for name in ("list", "events"):
        command = commands.add_parser(name)
        command.add_argument("--source")
        command.add_argument("--query")
        command.add_argument("--category")
        command.add_argument("--limit", type=positive, default=50)
        if name == "list":
            command.add_argument("--format", choices=["json", "markdown"], default="json")
        if name == "events":
            for field in ("campus", "date-from", "date-to", "status"):
                command.add_argument("--" + field)
            command.add_argument("--favorites", action="store_true")
            command.add_argument("--offset", type=int, default=0)
    digest = commands.add_parser("digest")
    digest.add_argument("--day")
    digest.add_argument("--refresh", action="store_true")
    digest.add_argument("--limit", type=positive, default=10)
    digest.add_argument("--format", choices=["json", "markdown"], default="markdown")
    api = commands.add_parser("api", help="Validated JSON operation; payload is read from standard input")
    api.add_argument("method", choices=METHODS)
    worker = commands.add_parser("update-worker", help="Internal background update worker")
    worker.add_argument("--id", required=True)
    commands.add_parser("schedule-tick", help="Check the daily update and catch up if due")
    commands.add_parser("schedule-install", help="Install the configured macOS background trigger")
    commands.add_parser("schedule-uninstall", help="Remove this database's background trigger and preserve data")
    for name in ("detail", "favorite", "feedback"):
        command = commands.add_parser(name)
        command.add_argument("event_id")
        if name == "favorite":
            command.add_argument("state", choices=["on", "off"])
        if name == "feedback":
            command.add_argument("value", choices=["interested", "not_interested", "known", "clear"])
    sources = commands.add_parser("sources").add_subparsers(dest="source_command", required=True)
    for name in ("discover", "check"):
        command = sources.add_parser(name)
        command.add_argument("--source", action="append")
        command.add_argument("--refresh", action="store_true")
    return cli


def markdown(data):
    lines = ["# 校园信息简报", "", "仅列已采集的公开来源；未知资格或截止时间不表示仍可报名。", ""]
    for doc in data["documents"]:
        event = doc.get("event") or {}
        title = doc["title"].replace("[", "\\[").replace("]", "\\]")
        activity_time = event.get("starts_at") or "未知"
        if event.get("starts_at"):
            activity_time += " — " + event["ends_at"] if event.get("ends_at") else "（结束时间未知）"
        lines.extend([f"- [{title}]({doc['url']})", f"  - 来源：{doc['source_name']}；状态：{event.get('status', '待识别')}",
                      f"  - 活动时间：{activity_time}；地点：{event.get('location') or '未知'}",
                      f"  - 原文受众：{event.get('audience') or '未知'}；完整参加资格：{event.get('eligibility') or '未知'}",
                      f"  - 发布时间：{doc['published_at'] or '未知'}；首次发现：{doc['first_seen_at']}"])
    if not data["documents"]:
        lines.append("当前查询没有已采集文档；这不代表校园没有活动。")
    return "\n".join(lines) + "\n"


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        sources = load_sources(args.sources)
        with Store(args.db) as store:
            store.sync_sources(sources)
            if args.command in {"schedule-tick", "schedule-install", "schedule-uninstall"}:
                if args.command == "schedule-uninstall":
                    result = uninstall_schedule(store)
                else:
                    result = tick(store, args.sources) if args.command == "schedule-tick" else install_schedule(store, args.sources)
                print(json.dumps(result, ensure_ascii=False))
                return 0
            if args.command == "list":
                result = store.listing(limit=args.limit, source_id=args.source, query=args.query, category=args.category)
                print(json.dumps(result, ensure_ascii=False) if args.format == "json" else markdown(result), end="\n")
                return 0
            if args.command == "events":
                result = store.event_listing(limit=args.limit, source_id=args.source, query=args.query, category=args.category,
                    campus=args.campus, date_from=args.date_from, date_to=args.date_to, status=args.status,
                    favorites=args.favorites, offset=args.offset)
                print(json.dumps(result, ensure_ascii=False))
                return 0
            if args.command == "digest":
                result = make_digest(store, day=args.day, refresh=args.refresh, limit=args.limit)
                print(json.dumps(result, ensure_ascii=False) if args.format == "json" else markdown_digest(result))
                return 0
            if args.command == "api":
                raw = sys.stdin.read(8193)
                if len(raw) > 8192:
                    raise ValueError("Request too large")
                result = dispatch(store, args.sources, args.method, json.loads(raw or "{}"))
                print(json.dumps(result, ensure_ascii=False))
                return 0
            if args.command == "update-worker":
                result = run_update(store, args.id)
                print(json.dumps(result, ensure_ascii=False))
                return 0 if result["status"] == "success" else 1
            if args.command in {"detail", "favorite", "feedback"}:
                payload = {"event_id": args.event_id}
                if args.command == "favorite":
                    payload["enabled"] = args.state == "on"
                if args.command == "feedback":
                    payload["value"] = args.value
                print(json.dumps(dispatch(store, args.sources, args.command, payload), ensure_ascii=False))
                return 0
            if args.command == "collect":
                result = enqueue_update(store, args.sources, source_ids=args.source, limit=args.limit, max_pages=args.max_pages)
                while result["status"] in {"queued", "running"}:
                    time.sleep(0.5)
                    result = update_status(store, result["id"])
                print(json.dumps(result, ensure_ascii=False))
                return 0 if result["status"] == "success" else 1
            selected = [source for source in sources if source["id"] in args.source] if args.source else [s for s in sources if s.get("enabled")]
            if args.source and set(args.source) - {source["id"] for source in selected}:
                raise ValueError("Unknown source ID")
            direct_hosts = {host for source in selected if source.get("network_mode") == "direct"
                            and source.get("visibility") == "public" for host in source.get("allowed_hosts", [])}
            fetcher = Fetcher(args.db.parent / "public-page-cache", cache_ttl=0 if args.refresh else 3600,
                              direct_hosts=direct_hosts)
            results = []
            for source in selected:
                try:
                    if source.get("visibility", "public") != "public":
                        raise CollectionError("Authenticated sources are not read by the public collector")
                    if not isinstance(source.get("url"), str):
                        raise CollectionError("Source has no verified public URL")
                    if args.source_command == "discover":
                        results.append(discover(source, fetcher))
                    else:
                        from urllib.parse import urlsplit
                        raw, final = fetcher.get(source["url"], source.get("allowed_hosts", [urlsplit(source["url"]).hostname]))
                        items, following = parse_listing(raw, final, source)
                        results.append({"source_id": source["id"], "status": "list_parsed", "url": final,
                                        "article_links": len(items), "samples": items[:3], "next_page": following,
                                        "coverage": "One list page checked; article collection not verified"})
                except CollectionError as error:
                    results.append({"source_id": source["id"], "status": "failed", "error": str(error)})
            print(json.dumps({"results": results}, ensure_ascii=False))
            return 1 if any(r.get("status") in {"failed", "partial"} for r in results) else 0
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
