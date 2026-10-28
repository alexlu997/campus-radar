"""Small public-page comparison; never writes the Campus Radar database."""

import argparse
import asyncio
from datetime import datetime, timezone
import importlib.metadata
import inspect
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campus_radar.collector import (  # noqa: E402
    CollectionError, Fetcher, MAX_BYTES, USER_AGENT, page_tree,
    extract_event, node_text, parse_document, parse_listing, require_host,
)

SOURCE_IDS = ("sjtu-news-lectures", "sjtu-me-reports", "sjtu-ctld-events")
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def cases_from_registry(path):
    registry = json.loads(Path(path).read_text())
    sources = {source["id"]: source for source in registry["sources"]}
    cases = []
    for source_id in SOURCE_IDS:
        source = sources[source_id]
        if source.get("visibility") != "public":
            raise ValueError("Comparison only accepts registered public sources")
        for kind, url in (("list", source["url"]), ("detail", source["latest_sample"]["url"])):
            url = require_host(url, source["allowed_hosts"])
            cases.append({"id": source_id + "-" + kind, "kind": kind, "url": url, "source": source})
    return cases


def assess_html(raw, final_url, case):
    """Use exactly the production parser for both transports; no model extraction."""
    result = {"html_bytes": len(raw), "parser_status": "failed", "body_characters": 0,
              "event": None, "published_at": None, "semantic_quality_verified": False}
    try:
        final_url = require_host(final_url, case["source"]["allowed_hosts"])
        if len(raw) > MAX_BYTES:
            raise CollectionError("HTML exceeds the production 4 MiB budget")
        page_tree(raw)
        if case["kind"] == "list":
            items, next_page = parse_listing(raw, final_url, case["source"])
            result.update(parser_status="parsed", article_links=len(items),
                          sample_links=[item["url"] for item in items[:5]], next_page=next_page)
        else:
            document = parse_document(raw, final_url, case["source"])
            result.update(parser_status="parsed", title=document["title"],
                          body_characters=len(document["body"]), body=document["body"],
                          published_at=document["published_at"], event=document["event"],
                          evidence=document["evidence"], processing_status=document["processing_status"])
    except (CollectionError, ValueError, TypeError) as error:
        result["parser_error"] = str(error)
    return result


def public_html_inputs(manifest_path, cases):
    manifest = json.loads(Path(manifest_path).read_text())
    items = {item["url"]: item for item in manifest["items"]}
    result = {}
    for case in cases:
        item = items[case["url"]]
        if str(item["metrics"]["http_code"]) != "200" or str(item["metrics"]["ssl_verify_result"]) != "0":
            raise ValueError("Raw comparison requires a successful, TLS-verified public acquisition record")
        path = Path(item["html_path"]).resolve()
        if not path.is_relative_to(ROOT / "outputs") or path.suffix != ".html":
            raise ValueError("Raw comparison only reads the public HTML evidence under outputs/")
        raw = path.read_bytes()
        if len(raw) > MAX_BYTES:
            raise ValueError("Raw HTML exceeds the 4 MiB comparison budget")
        raw.decode("utf-8")
        result[case["url"]] = {"raw": raw, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
    return result


def strict_robots_status(fetcher, url):
    parts = urlsplit(url)
    rules = fetcher.robots.get(f"{parts.scheme}://{parts.netloc}")
    if rules is None:
        return "unknown"
    return "allowed" if rules.can_fetch(USER_AGENT, url) else "disallowed"


def common_policy_accepted(network_success, robots_status):
    return bool(network_success and robots_status == "allowed")


def concise_error(error):
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(error))
    return " ".join(text.split())[:1200]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def fetch_existing(case, fetcher, output):
    started = time.perf_counter()
    row = {"engine": "existing_fetcher", "case_id": case["id"], "url": case["url"],
           "network_success": False, "target_requested": False}
    try:
        raw, final = fetcher.get(case["url"], case["source"]["allowed_hosts"])
        row.update(network_success=True, target_requested=True, final_url=final,
                   analysis=assess_html(raw, final, case))
        artifact = output / (case["id"] + ".fetcher.html")
        artifact.write_bytes(raw)
        row["html_artifact"] = str(artifact)
    except CollectionError as error:
        row["error"] = concise_error(error)
        row["target_requested"] = strict_robots_status(fetcher, case["url"]) == "allowed"
    row["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    row["strict_robots_status"] = strict_robots_status(fetcher, case["url"])
    row["accepted_under_common_policy"] = common_policy_accepted(row["network_success"], row["strict_robots_status"])
    return row


async def compare(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    runtime = output / "runtime"
    os.environ["CRAWL4_AI_BASE_DIRECTORY"] = str(runtime)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["CRAWL4AI_CHROMIUM_SANDBOX"] = "true"
    os.environ["CRAWL4AI_NO_CLOUD_NOTICE"] = "1"

    imported = time.perf_counter()
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    import aiohttp

    cases = cases_from_registry(args.sources)
    raw_inputs = public_html_inputs(args.html_manifest, cases) if args.html_manifest else None
    baseline_engine = "existing_parser" if raw_inputs else "existing_fetcher"
    candidate_engine = "crawl4ai_raw" if raw_inputs else "crawl4ai"
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(), "cases": cases, "results": [],
        "versions": {name: importlib.metadata.version(name) for name in ("crawl4ai", "playwright", "lxml", "aiohttp")},
        "python": sys.version, "python_executable": sys.executable,
        "crawl4ai_import_seconds": round(time.perf_counter() - imported, 3),
        "browser_executable": str(CHROME), "browser_downloaded": False,
        "mode": "raw_html_processing" if raw_inputs else "default_network",
        "input_manifest": str(args.html_manifest.resolve()) if raw_inputs else None,
        "parser_source_sha256": hashlib.sha256((ROOT / "campus_radar/collector.py").read_bytes()).hexdigest(),
        "settings": {"timeout_seconds": args.timeout, "retry_count": 0, "cache": "disabled",
                     "tls_validation": True, "chrome_channel": "chrome", "headless": True,
                     "user_agent": USER_AGENT, "enable_stealth": False, "check_robots_txt": not bool(raw_inputs),
                     "dotenv_loading": False, "model_extraction": False, "paid_model_api_calls": 0,
                     "crawl4ai_cloud_api_used": False, "system_proxy_modified": False},
        "robots_policy": {
            "existing_fetcher": "unavailable robots => stop; 404 => allow",
            "crawl4ai_0_9_5": "2-second aiohttp timeout; any exception or non-200 => allow",
            "aiohttp_trust_env_default": inspect.signature(aiohttp.ClientSession).parameters["trust_env"].default,
            "crawl4ai_proxy_source": "egress_policy.proxy_url(); defaults to None, not HTTP_PROXY/HTTPS_PROXY",
            "comparison_rule": "Native Crawl4AI success is not a framework advantage when robots policies differ; common-policy acceptance requires existing Fetcher's robots result to be allowed.",
        },
        "scope": "One sequential six-input sample; parsed does not establish semantic accuracy or complete pagination; raw HTML processing is not browser acquisition; no performance ranking or school coverage claim; no business database writes.",
    }
    if not CHROME.is_file():
        raise RuntimeError("Existing Google Chrome was not found; no browser will be downloaded")
    browser_config = BrowserConfig(
        browser_type="chromium", chrome_channel="chrome", headless=True,
        ignore_https_errors=False, enable_stealth=False, user_agent=USER_AGENT,
        use_persistent_context=False, accept_downloads=False, verbose=False,
    )
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.DISABLED, check_robots_txt=not bool(raw_inputs),
        page_timeout=int(args.timeout * 1000), wait_until="domcontentloaded",
        max_retries=0, word_count_threshold=1, verbose=False,
        simulate_user=False, override_navigator=False, magic=False,
        process_iframes=False, scan_full_page=False,
        capture_network_requests=False, remove_consent_popups=False,
    )
    crawler = AsyncWebCrawler(config=browser_config, base_directory=str(runtime))
    robot_observations = []
    original_can_fetch = crawler.robots_parser.can_fetch

    async def observe_robots(url, user_agent="*"):
        started = time.perf_counter()
        allowed = await original_can_fetch(url, user_agent)
        rules, fresh = crawler.robots_parser._get_cached_rules(urlsplit(url).netloc)
        robot_observations.append({"url": url, "returned_allowed": allowed,
                                   "cached_rules_present": rules is not None, "cache_fresh": fresh,
                                   "elapsed_seconds": round(time.perf_counter() - started, 3),
                                   "reason": "library does not expose the HTTP/error reason when allowing without cached rules" if allowed and rules is None else None})
        return allowed

    crawler.robots_parser.can_fetch = observe_robots
    active_hosts = set()
    navigation_blocks = []
    navigation_attempts = []

    async def restrict_navigation(page, **kwargs):
        async def route_request(route):
            request = route.request
            if request.is_navigation_request() and request.frame == page.main_frame:
                try:
                    require_host(request.url, active_hosts)
                except (CollectionError, ValueError):
                    navigation_blocks.append("main-frame redirect outside the case's verified host was blocked")
                    await route.abort()
                    return
                navigation_attempts.append(request.url)
            await route.continue_()
        await page.route("**/*", route_request)
        return page

    crawler.crawler_strategy.set_hook("on_page_context_created", restrict_navigation)
    fetcher = Fetcher(output / "fetcher-cache", timeout=args.timeout, delay=1, retries=0, cache_ttl=0)
    browser_error = None
    started = time.perf_counter()
    try:
        await crawler.start()
        report["browser_version"] = crawler.crawler_strategy.browser_manager.browser.version
    except Exception as error:
        browser_error = concise_error(error)
        report["browser_start_error"] = browser_error
    report["browser_start_seconds"] = round(time.perf_counter() - started, 3)
    report_path = output / "results.json"
    try:
        for case in cases:
            if raw_inputs:
                supplied = raw_inputs[case["url"]]
                started = time.perf_counter()
                analysis = assess_html(supplied["raw"], case["url"], case)
                baseline = {"engine": baseline_engine, "case_id": case["id"], "url": case["url"],
                            "network_success": None, "target_requested": False,
                            "strict_robots_status": "not_applicable_raw_html", "accepted_under_common_policy": None,
                            "analysis": analysis, "processing_success": analysis["parser_status"] == "parsed",
                            "input_html_path": supplied["path"], "input_sha256": supplied["sha256"],
                            "elapsed_seconds": round(time.perf_counter() - started, 3)}
            else:
                baseline = await asyncio.to_thread(fetch_existing, case, fetcher, output)
            report["results"].append(baseline)
            active_hosts.clear()
            active_hosts.update(case["source"]["allowed_hosts"])
            row = {"engine": candidate_engine, "case_id": case["id"], "url": case["url"],
                   "network_success": None if raw_inputs else False, "target_requested": False,
                   "strict_robots_status": baseline["strict_robots_status"]}
            started = time.perf_counter()
            observations_before = len(robot_observations)
            navigation_before = len(navigation_attempts)
            if browser_error:
                row["error"] = "Browser startup failed: " + browser_error
            elif baseline["strict_robots_status"] == "disallowed":
                row["error"] = "Known robots denial; no second request was made"
            else:
                try:
                    crawl_input = "raw:" + raw_inputs[case["url"]]["raw"].decode("utf-8") if raw_inputs else case["url"]
                    result = await asyncio.wait_for(crawler.arun(crawl_input, config=run_config), timeout=args.timeout + 10)
                    row["status_code"] = result.status_code
                    final = case["url"] if raw_inputs else (getattr(result, "redirected_url", None) or result.url)
                    require_host(final, active_hosts)
                    if not result.success or not result.html or (result.status_code is not None and not 200 <= result.status_code < 300):
                        raise CollectionError(result.error_message or f"No successful HTML response (HTTP {result.status_code})")
                    raw = result.html.encode("utf-8")
                    if len(raw) > MAX_BYTES:
                        raise CollectionError("HTML exceeds the production 4 MiB budget")
                    markdown = getattr(result.markdown, "raw_markdown", None) or str(result.markdown or "")
                    row.update(network_success=None if raw_inputs else True, processing_success=True,
                               final_url=final, analysis=assess_html(raw, final, case),
                               native_cleaned_html_characters=len(result.cleaned_html or ""), native_markdown_characters=len(markdown))
                    if raw_inputs:
                        row.update(input_html_path=raw_inputs[case["url"]]["path"], input_sha256=raw_inputs[case["url"]]["sha256"])
                    if case["kind"] == "detail" and result.cleaned_html:
                        try:
                            cleaned_text = node_text(page_tree(result.cleaned_html.encode("utf-8")))
                            cleaned_title = (result.metadata or {}).get("title") or row["analysis"].get("title", "")
                            event, flags = extract_event(cleaned_title, cleaned_text, case["url"], case["source"])
                            row["native_text_analysis"] = {"title": cleaned_title, "body_characters": len(cleaned_text),
                                                           "body": cleaned_text, "event": event, "warnings": flags,
                                                           "semantic_quality_verified": False}
                        except (CollectionError, ValueError) as error:
                            row["native_text_error"] = concise_error(error)
                    artifact = output / (case["id"] + ".crawl4ai.html")
                    artifact.write_bytes(raw)
                    row["html_artifact"] = str(artifact)
                    markdown_path = output / (case["id"] + ".crawl4ai.md")
                    markdown_path.write_text(markdown)
                    row["markdown_artifact"] = str(markdown_path)
                except Exception as error:
                    row["error"] = concise_error(error)
            row["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            row["target_requested"] = bool(navigation_attempts[navigation_before:])
            row["robots_observations"] = robot_observations[observations_before:]
            row["accepted_under_common_policy"] = None if raw_inputs else common_policy_accepted(row["network_success"], row["strict_robots_status"])
            report["results"].append(row)
            write_json(report_path, report)
            print(json.dumps({"case": case["id"], "existing_success": baseline["network_success"],
                              "crawl4ai_success": row["network_success"], "completed_pairs": len(report["results"]) // 2}), flush=True)
            if not raw_inputs:
                await asyncio.sleep(1)
    finally:
        await crawler.close()
    report["navigation_blocks"] = navigation_blocks
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["summary"] = {
        engine: {"cases": len([r for r in report["results"] if r["engine"] == engine]),
                 "network_successes": None if raw_inputs else sum(r["network_success"] is True for r in report["results"] if r["engine"] == engine),
                 "common_policy_accepted": None if raw_inputs else sum(r["accepted_under_common_policy"] is True for r in report["results"] if r["engine"] == engine),
                 "shared_parser_successes": sum(r.get("analysis", {}).get("parser_status") == "parsed" for r in report["results"] if r["engine"] == engine)}
        for engine in (baseline_engine, candidate_engine)
    }
    write_json(report_path, report)
    print(str(report_path))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=ROOT / "data/sources.json")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/crawler-comparison/run-1")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--html-manifest", type=Path, help="Process the same verified local public HTML in raw mode; makes no target-page requests")
    parser.add_argument("--list-inputs", action="store_true", help="Print the six URLs without network or Crawl4AI startup")
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 60:
        parser.error("timeout must be 1–60 seconds")
    if args.list_inputs:
        print(json.dumps(cases_from_registry(args.sources), ensure_ascii=False, indent=2))
        return 0
    if (args.output / "results.json").exists():
        parser.error("Choose a fresh output directory; prior comparison evidence is not overwritten")
    return asyncio.run(compare(args))


if __name__ == "__main__":
    raise SystemExit(main())
