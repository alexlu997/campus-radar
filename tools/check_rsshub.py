#!/usr/bin/env python3
"""Bounded, read-only check of RSSHub's existing SJTU CS activity route."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import re
import subprocess
import tempfile
from unittest.mock import patch
import xml.etree.ElementTree as ET


TARGETS = {
    "feed": "https://rsshub.app/sjtu/cs/xshd",
    "upstream": "https://www.cs.sjtu.edu.cn/xshd.html",
}


def inspect_feed(body):
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        return {"valid_feed": False, "parse_error": str(exc), "item_count": None}
    local_name = lambda tag: tag.rsplit("}", 1)[-1]
    if local_name(root.tag) not in {"rss", "feed", "RDF"}:
        return {"valid_feed": False, "parse_error": "XML root is not RSS/Atom", "item_count": None}
    items = []
    for node in root.iter():
        if local_name(node.tag) not in {"item", "entry"}:
            continue
        fields = {}
        for child in node:
            key = local_name(child.tag)
            text = "".join(child.itertext()).strip()
            if key == "link":
                fields[key] = child.get("href") or text
            elif key in {"title", "pubDate", "published", "updated", "description", "content", "encoded"}:
                fields[key] = text
        description = fields.get("encoded") or fields.get("content") or fields.get("description") or ""
        plain = html.unescape(re.sub(r"<[^>]+>", " ", description))
        items.append({
            "title": fields.get("title"),
            "link": fields.get("link"),
            "published": fields.get("pubDate") or fields.get("published"),
            "updated": fields.get("updated"),
            "description_characters": len(description),
            "description_text": re.sub(r"\s+", " ", plain).strip(),
        })
    return {"valid_feed": True, "item_count": len(items), "items": items}


def fetch(name, url, mode, args, output):
    stem = output / f"{name}-{mode}"
    header_path = stem.with_suffix(".headers")
    body_path = stem.with_suffix(".body")
    command = [
        "curl", "--silent", "--show-error", "--location", "--max-redirs", "4",
        "--proto", "=https", "--proto-redir", "=https",
        "--connect-timeout", "8", "--max-time", str(args.timeout),
        "--max-filesize", "10485760", "--dump-header", str(header_path),
        "--output", str(body_path), "--write-out", "%{json}",
    ]
    # 'direct' bypasses explicit HTTP proxies, but cannot disable a system TUN.
    command += ["--noproxy", "*"] if mode == "direct" else ["--proxy", args.proxy, "--noproxy", ""]
    command.append(url)
    started = datetime.now(timezone.utc).isoformat()
    result = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout + 5)
    try:
        timing = json.loads(result.stdout)
    except json.JSONDecodeError:
        timing = {"raw_output": result.stdout}
    record = {
        "name": name, "mode": mode, "url": url, "started_at": started,
        "command": command, "returncode": result.returncode,
        "stderr": result.stderr.strip(), "curl": timing,
        "headers_file": header_path.name,
        "body_file": body_path.name if body_path.exists() else None,
        "body_bytes": body_path.stat().st_size if body_path.exists() else 0,
    }
    if name.startswith("feed") and result.returncode == 0 and timing.get("http_code") == 200 and body_path.exists():
        record["feed"] = inspect_feed(body_path.read_bytes())
    else:
        record["feed"] = {"valid_feed": False, "item_count": None, "reason": "No complete successful HTTP feed response"} if name.startswith("feed") else None
    stem.with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return record


def self_test():
    sample = b'<rss version="2.0"><channel><item><title>Example</title><link>https://example.org/a</link><description>&lt;p&gt;Time unknown&lt;/p&gt;</description></item></channel></rss>'
    result = inspect_feed(sample)
    assert result["valid_feed"] and result["item_count"] == 1
    assert result["items"][0]["published"] is None
    assert result["items"][0]["description_text"] == "Time unknown"
    assert inspect_feed(b"<html><body>Blocked</body></html>")["item_count"] is None
    assert inspect_feed(b"bad xml")["item_count"] is None
    with tempfile.TemporaryDirectory(prefix="rsshub-self-test-") as directory:
        for returncode in (0, 28):
            def fake_curl(command, **kwargs):
                Path(command[command.index("--output") + 1]).write_bytes(sample)
                return subprocess.CompletedProcess(command, returncode, '{"http_code":200}', "")

            with patch("subprocess.run", side_effect=fake_curl):
                record = fetch("feed", TARGETS["feed"], "direct", argparse.Namespace(timeout=25), Path(directory))
            assert record["body_bytes"] == len(sample)
            assert record["feed"]["valid_feed"] is (returncode == 0)
            assert record["feed"]["item_count"] == (1 if returncode == 0 else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy", default="http://127.0.0.1:7897")
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--out-dir", type=Path, default=Path("outputs/rsshub-check"))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    self_test()
    if args.self_test:
        print("self-test passed")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.out_dir.resolve() / stamp
    output.mkdir(parents=True, exist_ok=False)
    jobs = [(name, url, mode) for name, url in TARGETS.items() for mode in ("direct", "proxy")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda job: fetch(*job, args, output), jobs))
    # A repeat request only has value when an actual feed was returned.
    for record in list(records):
        if record["feed"] and record["feed"]["valid_feed"]:
            records.append(fetch("feed-repeat", record["url"], record["mode"], args, output))
    summary = {"output_directory": str(output), "requests": records}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output_directory": str(output), "requests": [
        {"name": r["name"], "mode": r["mode"], "http_code": r["curl"].get("http_code"),
         "returncode": r["returncode"], "error": r["stderr"], "body_bytes": r["body_bytes"],
         "item_count": r["feed"].get("item_count") if r["feed"] else None}
        for r in records]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
