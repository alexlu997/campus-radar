"""Bounded public HTML collection; unsupported or ambiguous pages stay explicit."""

import base64
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

from lxml import html

from .storage import utc_now
from .network import DirectTransport, TransportError
from .events import instant, status_at, title_key


USER_AGENT = "CampusRadar/0.1 (local public academic information reader)"
MAX_BYTES = 4 * 1024 * 1024
DETAIL_PATHS = {
    "sjtu_news": re.compile(r"^/xsjz/[^/]+/[^/]+\.html$"),
    "sjtu_me": re.compile(r"^/tsfx-xsbg/\d+\.html$"),
    "ctld": re.compile(r"^/events/detail/[A-Za-z0-9]+/?$"),
}
SUPPORTED_ADAPTERS = frozenset(DETAIL_PATHS) | {"weread_latest"}
LIST_PATHS = {
    "sjtu_news": re.compile(r"^/xsjz/(?:index(?:_\d+)?\.html)?$"),
    "sjtu_me": re.compile(r"^/tsfx-xsbg(?:_\d+|/p\d+)?\.html$"),
    "ctld": re.compile(r"^/events/?$"),
}
DATE = re.compile(r"(?P<y>20\d{2})\s*[年./-]\s*(?P<m>\d{1,2})\s*[月./-]\s*(?P<d>\d{1,2})\s*日?")
CLOCK = re.compile(r"(?<!\d)([0-2]?\d)\s*[:：]\s*([0-5]\d)(?!\d)")


class CollectionError(ValueError):
    pass


def normalize_url(url):
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        raise CollectionError("Only credential-free public HTTP(S) URLs are accepted")
    if parts.port not in {None, 80, 443}:
        raise CollectionError("Unexpected public website port")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def require_host(url, allowed_hosts):
    result = normalize_url(url)
    if urlsplit(result).hostname not in allowed_hosts:
        raise CollectionError("URL or redirect leaves the verified source hosts")
    return result


class HostRedirect(HTTPRedirectHandler):
    def __init__(self, allowed_hosts):
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_host(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    def __init__(self, cache_dir, *, timeout=20, delay=1, retries=1, cache_ttl=3600, direct_hosts=()):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.timeout, self.delay, self.retries, self.cache_ttl = timeout, delay, retries, cache_ttl
        self.last_request = {}
        self.robots = {}
        self.cache_hits = 0
        self.direct_hosts = set(direct_hosts)
        self.direct = DirectTransport(timeout, MAX_BYTES, USER_AGENT)

    @property
    def network_events(self):
        return self.direct.events

    def _request(self, url, allowed_hosts):
        host = urlsplit(url).hostname
        opener = build_opener(HostRedirect(allowed_hosts))
        for attempt in range(self.retries + 1):
            wait = self.delay - (time.monotonic() - self.last_request.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self.last_request[host] = time.monotonic()
            try:
                if host in self.direct_hosts:
                    return self.direct.get(url, allowed_hosts)
                with opener.open(Request(url, headers={"User-Agent": USER_AGENT}), timeout=self.timeout) as response:
                    final_url = require_host(response.url, allowed_hosts)
                    data = response.read(MAX_BYTES + 1)
                    if len(data) > MAX_BYTES:
                        raise CollectionError("Response exceeds the 4 MiB page budget")
                    return data, final_url, response.headers.get_content_type()
            except TransportError as error:
                self.direct.dns.clear()
                if not error.retryable or attempt == self.retries:
                    raise CollectionError(str(error)) from None
            except HTTPError as error:
                if error.code not in {408, 500, 502, 503, 504} or attempt == self.retries:
                    raise CollectionError(f"HTTP {error.code}; no page accepted") from None
            except (URLError, TimeoutError, socket.timeout) as error:
                if attempt == self.retries:
                    reason = type(error.reason).__name__ if isinstance(error, URLError) else type(error).__name__
                    raise CollectionError(f"Network request failed ({reason}); source access is unverified") from None

    def get(self, url, allowed_hosts):
        url = require_host(url, allowed_hosts)
        cache_path = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text())
                if time.time() - cached["timestamp"] < self.cache_ttl:
                    final_url = require_host(cached["url"], allowed_hosts)
                    self.cache_hits += 1
                    return base64.b64decode(cached["body"]), final_url
            except (ValueError, KeyError, OSError):
                pass
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            robots_url = origin + "/robots.txt"
            try:
                raw, _, _ = self._request(robots_url, allowed_hosts)
            except CollectionError as error:
                if not str(error).startswith("HTTP 404"):
                    raise CollectionError(f"robots.txt could not be verified: {error}") from None
                raw = b""
            rules = RobotFileParser(robots_url)
            rules.parse(raw.decode("utf-8", "replace").splitlines())
            self.robots[origin] = rules
        if not self.robots[origin].can_fetch(USER_AGENT, url):
            raise CollectionError("robots.txt does not allow this collector to fetch the URL")
        delay = self.robots[origin].crawl_delay(USER_AGENT)
        if delay and delay > self.delay:
            self.delay = min(delay, 60)
            if delay > 60:
                raise CollectionError("Site crawl delay exceeds this interactive run budget")
        raw, final_url, kind = self._request(url, allowed_hosts)
        if kind not in {"text/html", "application/xhtml+xml"}:
            raise CollectionError(f"Expected HTML, received {kind}")
        cache_path.write_text(json.dumps({"timestamp": time.time(), "url": final_url,
                                         "body": base64.b64encode(raw).decode()}))
        return raw, final_url


def page_tree(raw):
    try:
        charset = re.search(br"charset\s*=\s*[\"']?\s*([a-zA-Z0-9_-]+)", raw[:4096]) if isinstance(raw, bytes) else None
        encoding = charset[1].decode("ascii") if charset else "utf-8"
        tree = html.fromstring(raw, parser=html.HTMLParser(encoding=encoding))
    except (ValueError, TypeError) as error:
        raise CollectionError(f"Invalid HTML: {type(error).__name__}") from None
    title = " ".join(tree.xpath("//title/text()"))
    if tree.xpath('//input[translate(@type,"PASSWORD","password")="password"]') or re.search(r"jaccount|统一身份认证|登录系统|登录验证", title, re.I):
        raise CollectionError("Login page returned; it is not a public activity list")
    for node in tree.xpath("//script|//style|//noscript"):
        node.drop_tree()
    if len(compact(tree.text_content())) < 40:
        raise CollectionError("Empty HTML shell; no usable public content")
    return tree


def compact(value):
    return re.sub(r"\s+", " ", value).strip()


def node_text(node):
    # Keep paragraph boundaries so a field cannot swallow the next label.
    clone = html.fromstring(html.tostring(node, encoding="unicode"))
    for element in clone.iter():
        if element.tag in {"p", "div", "li", "br", "h1", "h2", "h3", "tr"}:
            element.tail = "\n" + (element.tail or "")
    return "\n".join(filter(None, (compact(line) for line in clone.text_content().splitlines())))


def parse_listing(raw, url, source):
    tree = page_tree(raw)
    adapter = source["adapter"]
    if adapter not in DETAIL_PATHS:
        raise CollectionError("Source has no configured collection adapter")
    records, seen, next_url = [], set(), None
    allowed = source.get("allowed_hosts", [urlsplit(source["url"]).hostname])
    containers = {"sjtu_news": "//*[@id='xsjz']", "sjtu_me": "//*[contains(concat(' ',@class,' '),' u50 ')]",
                  "ctld": "//*[contains(concat(' ',@class,' '),' event-list ')]"}
    roots = tree.xpath(containers[adapter])
    anchors = roots[0].xpath(".//a[@href]") if roots else tree.xpath("//a[@href]")
    for anchor in anchors:
        href = urljoin(url, anchor.get("href"))
        try:
            href = require_host(href, allowed)
        except (CollectionError, ValueError):
            continue
        titles = anchor.xpath(".//*[contains(concat(' ',@class,' '),' title ') or contains(concat(' ',@class,' '),' line-1 ')]")
        label = compact((titles[0] if titles else anchor).text_content())
        if DETAIL_PATHS[adapter].match(urlsplit(href).path) and href not in seen:
            seen.add(href)
            context = node_text(anchor.getparent())[:1500]
            records.append({"url": href, "title": label, "list_context": context, "list_url": url})
    for anchor in tree.xpath("//a[@href]"):
        label = compact(anchor.text_content())
        if label in {"下一页", "下页", "Next", "next", ">"} or anchor.get("rel") == "next":
            try:
                href = require_host(urljoin(url, anchor.get("href")), allowed)
            except (CollectionError, ValueError):
                continue
            if href != url and LIST_PATHS[adapter].match(urlsplit(href).path):
                next_url = href
    # These two sites publish pagination data in scripts, not next-page anchors.
    scripts = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    if adapter == "sjtu_news":
        paging = re.search(r'pagination\(["\']setPage["\']\s*,\s*(\d+)\s*,\s*(\d+)\)', scripts)
        if paging and int(paging[1]) < int(paging[2]):
            next_url = urljoin(url, f"index_{int(paging[1]) + 1}.html")
    elif adapter == "ctld":
        current = re.search(r"var\s+_page\s*=\s*(\d+)", scripts)
        paging = re.search(r"laypage\.render\(\{[\s\S]*?count\s*:\s*(\d+)\s*,\s*limit\s*:\s*(\d+)", scripts)
        if current and paging and int(paging[2]) > 0 and int(current[1]) * int(paging[2]) < int(paging[1]):
            parts = urlsplit(url)
            query = [(key, value) for key, value in parse_qsl(parts.query) if key in {"keyword", "group_id", "atagId"}]
            query.append(("page", str(int(current[1]) + 1)))
            next_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    if next_url:
        next_url = require_host(next_url, allowed)
    if not records:
        raise CollectionError("No matching article links; empty list and parser failure cannot be distinguished")
    return records, next_url


def parse_date(value):
    match = DATE.search(value)
    if not match:
        return None, None
    try:
        day = date(int(match["y"]), int(match["m"]), int(match["d"]))
        clock = CLOCK.search(value[match.end():])
        if clock:
            dt = datetime(day.year, day.month, day.day, int(clock[1]), int(clock[2]), tzinfo=ZoneInfo("Asia/Shanghai"))
            return dt.isoformat(), "minute"
        return day.isoformat(), "date"
    except ValueError:
        return None, None


def labelled_fields(body, labels):
    found = []
    pattern = r"(?:^|\n|\s)(?:[一二三四五六七八九十\d]+[、.]\s*)?(?:" + "|".join(labels) + r")\s*[:：]\s*([^\n]{1,300})"
    for match in re.finditer(pattern, body):
        value = re.split(r"\s+(?:时间|日期|地点|报告人|主讲人|主持人|报名|主办|对象|资格)\s*[:：]", match[1])[0].strip()
        if value:
            found.append(value)
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    for label, value in zip(lines, lines[1:]):
        if label in labels and not re.fullmatch(r".{0,6}(?:时间|日期|地点|报告人|主讲人|主持人|对象|资格|名额)[:：]?", value):
            found.append(value[:300])
    return list(dict.fromkeys(found))


def event_range(value):
    start, precision = parse_date(value)
    if not start:
        return None, None, None
    dates = list(DATE.finditer(value))
    end = None
    if len(dates) == 2:
        between = value[dates[0].end():dates[1].start()]
        if not re.fullmatch(r"\s*(?:\d{1,2}[:：]\d{2})?\s*[-—–~至]+\s*", between):
            return None, None, None
        end, end_precision = parse_date(value[dates[1].start():])
        if precision != end_precision:
            return None, None, None
    elif len(dates) > 2:
        return None, None, None
    elif precision == "minute":
        clocks = list(CLOCK.finditer(value))
        if len(clocks) == 2:
            try:
                end = datetime.fromisoformat(start).replace(hour=int(clocks[1][1]), minute=int(clocks[1][2])).isoformat()
            except ValueError:
                return start, None, precision
    else:
        short_end = re.match(r"\s*[-—–~至]+\s*(\d{1,2})\s*日", value[dates[0].end():])
        if short_end:
            try:
                end = date.fromisoformat(start).replace(day=int(short_end[1])).isoformat()
            except ValueError:
                return start, None, precision
    if end and end < start:
        return None, None, None
    return start, end, precision


def deadline_date(value):
    if re.search(r"24[:：]00", value):
        day, _ = parse_date(re.sub(r"24[:：]00", "", value))
        if day:
            return (datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("Asia/Shanghai")) + timedelta(days=1)).isoformat()
    return parse_date(value)[0]


def extract_event(title, body, url, source, item=None, metadata=None):
    metadata = metadata or {}
    times = metadata.get("times") or labelled_fields(body, ["报告时间", "活动时间", "讲座时间", "时间", "活动日期"])
    places = metadata.get("places") or labelled_fields(body, ["报告地点", "活动地点", "讲座地点", "地点"])
    eligibility = labelled_fields(body, ["资格要求"])
    audiences = labelled_fields(body, ["面向对象", "参加对象", "参与对象", "活动对象", "参会对象"])
    audiences += re.findall(r"请有兴趣参加本次活动的(同学|老师)", body)
    audiences = list(dict.fromkeys(audiences))
    deadlines = labelled_fields(body, ["报名截止时间", "报名截止", "截止时间"])
    evidence, flags = {}, []
    if len(times) > 1:
        return None, ["multiple_times_or_events_pending"]
    relevant = re.search(r"讲座|报告|研讨|工作坊|培训|会议|报名|招募|竞赛|交流|seminar|workshop|lecture", title, re.I) or times
    if not relevant:
        return None, ["event_identification_pending"]
    event = {"title": title, "category": source.get("category"), "organizer": None,
             "starts_at": None, "ends_at": None, "registration_deadline": None,
             "location": None, "campus": None, "eligibility": None, "registration_url": None,
             "audience": None, "status": "unknown", "field_evidence": evidence, "time_precision": None}
    if re.search(r"回顾|纪实|成功举办|圆满|顺利举行|新闻回放", title):
        event["status"] = "recap"
    elif re.search(r"取消", title):
        event["status"] = "cancelled"
    elif re.search(r"延期|推迟", title):
        event["status"] = "postponed"
    if times:
        event["starts_at"], event["ends_at"], event["time_precision"] = event_range(times[0])
        evidence["starts_at"] = {"url": url, "text": times[0], "precision": event["time_precision"]}
        if not event["starts_at"]:
            flags.append("event_time_incomplete")
        if event["ends_at"]:
            evidence["ends_at"] = {"url": url, "text": times[0], "precision": event["time_precision"]}
    if len(audiences) == 1:
        event["audience"] = audiences[0]
        evidence["audience"] = {"url": url, "text": audiences[0], "complete_eligibility": False}
    for field, values in (("location", places), ("eligibility", eligibility), ("registration_deadline", deadlines)):
        if len(values) == 1:
            event[field] = deadline_date(values[0]) if field == "registration_deadline" else values[0]
            evidence[field] = {"url": url, "text": values[0]}
        elif len(values) > 1:
            flags.append(field + "_conflict")
            evidence[field] = {"url": url, "conflicting_values": values}
    if not deadlines:
        partial = re.findall(r"\d{1,2}月\d{1,2}日\s*\d{1,2}[:：]\d{2}前", body)
        if partial:
            evidence["registration_deadline"] = {"url": url, "partial_texts": list(dict.fromkeys(partial)), "missing": "year"}
        submissions = []
        for line in body.splitlines():
            match = re.search(r"截止时间为([^。；\n]+)", line)
            if match:
                submissions.append({"raw": match[1], "context": line[:300]})
        if submissions:
            evidence["registration_deadline"] = {"url": url, "separate_submission_deadlines": submissions}
            flags.append("submission_deadlines_require_separate_actions")
    if event["location"]:
        campuses = re.findall(r"闵行|徐汇|卢湾|黄浦|七宝|长宁", event["location"])
        if len(set(campuses)) == 1:
            event["campus"] = campuses[0]
            evidence["campus"] = evidence["location"]
    if event["starts_at"] and event["status"] == "unknown":
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        ended = event["ends_at"] and (event["ends_at"] < now.date().isoformat() if event["time_precision"] == "date"
                                     else datetime.fromisoformat(event["ends_at"]) < now)
        if ended:
            event["status"] = "past"
        elif event["starts_at"][:10] < now.date().isoformat():
            event["status"] = "started_end_unknown"
        else:
            event["status"] = "scheduled_registration_unknown"
    return event, flags


def extract_events(title, body, url, source, item=None, metadata=None):
    main, flags = extract_event(title, body, url, source, item, metadata)
    events = [main] if main else []
    if "multiple_times_or_events_pending" in flags:
        headings = list(re.finditer(r"(?:^|\n)(?:[一二三四五六七八九十\d]+[、.]\s*|(?:活动名称|报告题目|讲座名称)[:：]\s*)([^\n]{3,120})\n", body))
        sections = []
        section_flags = []
        for index, heading in enumerate(headings):
            part = body[heading.end():headings[index + 1].start() if index + 1 < len(headings) else len(body)]
            event, warnings = extract_event(heading[1].strip(), part, url, source)
            if event and event.get("starts_at"):
                event["key"] = "session:" + title_key(event["title"])
                sections.append(event)
                section_flags.extend(warnings)
        if len(sections) >= 2 and len(sections) == len(headings) and len({e["key"] for e in sections}) == len(sections):
            events, flags = sections, section_flags
    for event in list(events):
        deadlines = event.get("field_evidence", {}).get("registration_deadline", {}).get("separate_submission_deadlines", [])
        for entry in deadlines:
            label = re.search(r"前置工作坊申报|工作坊申报|论文投稿|投稿|报名|申请", entry["context"])
            if not label:
                continue
            action_title = event["title"] + " · " + label[0]
            events.append({"key": "deadline:" + title_key(action_title), "kind": "deadline", "title": action_title,
                           "category": event.get("category"), "status": "unknown", "starts_at": None, "ends_at": None,
                           "registration_deadline": deadline_date(entry["raw"]), "location": event.get("location"),
                           "campus": event.get("campus"), "eligibility": None, "audience": event.get("audience"),
                           "field_evidence": {"registration_deadline": {"url": url, "text": entry["raw"], "context": entry["context"]}}})
        if deadlines and len(events) > 1:
            flags = [f for f in flags if f != "submission_deadlines_require_separate_actions"]
    return events, flags


def parse_document(raw, url, source, item=None):
    tree = page_tree(raw)
    adapter = source.get("adapter")
    title_selector = {"sjtu_me": "//*[contains(concat(' ',@class,' '),' news-cont ')]//*[contains(concat(' ',@class,' '),' tit ')]",
                      "ctld": "//*[contains(concat(' ',@class,' '),' event-banner ')]//*[contains(concat(' ',@class,' '),' title ')]"}
    title_nodes = tree.xpath(title_selector[adapter]) if adapter in title_selector else []
    title_nodes = title_nodes or tree.xpath("//h1|//h2[contains(@class,'title')]|//*[@class='Article_title']|//*[@class='article-title']")
    title = next((compact(n.text_content()) for n in title_nodes if compact(n.text_content())), "")
    if not title:
        title = compact(" ".join(tree.xpath("//title/text()"))).split(" - ")[0]
    selectors = [
        "//*[@id='vsb_content' or @id='vsb_content_2' or @id='content']",
        "//*[contains(@class,'Article_content') or contains(@class,'article-content') or contains(@class,'article_content')]",
        "//*[contains(@class,'detail-content') or contains(@class,'detail_content') or contains(@class,'event-content')]",
        "//article",
    ]
    specific_body = {"sjtu_me": "//*[contains(concat(' ',@class,' '),' news-cont ')]/*[contains(concat(' ',@class,' '),' l ')]/*[contains(concat(' ',@class,' '),' txt ')]",
                     "ctld": "//*[contains(concat(' ',@class,' '),' page-content ')]"}
    if adapter in specific_body:
        selectors.insert(0, specific_body[adapter])
    body_node = None
    for selector in selectors:
        candidates = [n for n in tree.xpath(selector) if len(compact(n.text_content())) >= 40 or n.xpath(".//img")]
        if candidates:
            body_node = max(candidates, key=lambda n: len(n.text_content()))
            break
    if body_node is None or not title:
        raise CollectionError("Article title/body selector did not match; page was not accepted")
    body = node_text(body_node)
    event_metadata = {}
    if adapter == "sjtu_news":
        for key, css in (("times", "LectureTime"), ("places", "LectureAdd")):
            fields = tree.xpath(f"//*[contains(concat(' ',@class,' '),' {css} ')]")
            if fields:
                event_metadata[key] = [compact(compact(n.text_content()).split("：", 1)[-1]) for n in fields]
    elif adapter == "ctld":
        banner = tree.xpath("//*[contains(concat(' ',@class,' '),' event-banner ')]")
        body_times = labelled_fields(body, ["报告时间", "活动时间", "讲座时间", "时间", "活动日期"])
        if banner:
            if not body_times or (len(body_times) == 1 and not event_range(body_times[0])[0]):
                times = banner[0].xpath(".//*[contains(concat(' ',@class,' '),' info-item ')][.//i[contains(@class,'icon-length')]]")
                if times:
                    value = compact(times[0].text_content())
                    # Midnight/day-end CMS placeholders do not establish exact event hours.
                    if "00:00" in value and "23:59" in value:
                        value = value.replace("00:00", "").replace("23:59", "")
                    event_metadata["times"] = [value]
            if not labelled_fields(body, ["报告地点", "活动地点", "讲座地点", "地点"]):
                places = banner[0].xpath(".//*[contains(concat(' ',@class,' '),' info-item ')][.//i[contains(@class,'icon-location')]]")
                if places:
                    event_metadata["places"] = [compact(places[0].text_content())]
    published = None
    evidence = {"fetched_url": url, "extraction": "rule_based", "warnings": []}
    metadata = tree.xpath('//meta[@name="publishdate" or @name="PubDate" or @property="article:published_time"]/@content')
    if metadata:
        published = parse_date(metadata[0])[0]
        evidence["published_at"] = {"url": url, "text": metadata[0], "origin": "page_metadata"}
    else:
        match = re.search(r"发布时间\s*[:：]\s*(20\d{2}[-年./]\d{1,2}[-月./]\d{1,2}日?)", node_text(tree))
        if match:
            published = parse_date(match[1])[0]
            evidence["published_at"] = {"url": url, "text": match[0], "origin": "labelled_page_text"}
    if published and published[:10] > datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat():
        evidence["warnings"].append("publisher_date_in_future_unverified")
    events, flags = extract_events(title, body, url, source, item, event_metadata)
    if event_metadata:
        evidence["event_metadata"] = {"url": url, "origin": "official_event_header", **event_metadata}
    if body_node.xpath(".//img|.//a[contains(@href,'.pdf') or contains(@href,'.doc') or contains(@href,'.zip')]"):
        flags.append("images_or_attachments_pending")
    evidence["warnings"].extend(flags)
    return {"url": url, "title": title, "body": body, "published_at": published,
            "visibility": "public", "processing_status": "pending_review" if flags else "parsed",
            "evidence": evidence, "event": events[0] if events else None, "events": events}


def parse_wechat_document(raw, url, source):
    require_host(url, ["mp.weixin.qq.com"])
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    if re.search(r"<title[^>]*>\s*验证\s*</title>", text) and "操作频繁" in text:
        raise CollectionError("微信读书提示操作频繁，请稍后重试；旧文章保留，无需重复扫码")
    tree = page_tree(raw)
    bodies = tree.xpath('//*[@id="js_content"]')
    titles = tree.xpath('//*[@id="activity-name" or contains(concat(" ",@class," ")," rich_media_title ")]')
    if not bodies or not titles or len(node_text(bodies[0])) < 40:
        raise CollectionError("公众号正文不可读或返回验证页；未接收文章")
    content, heading = node_text(bodies[0]), node_text(titles[0])
    authors = tree.xpath('//*[@id="js_name"]')
    author = node_text(authors[0]) if authors else None
    biz_values = set(re.findall(r'^\s*var biz\s*=\s*(?:[\"\']{2}\s*\|\|\s*)?[\"\']([^\"\']+)[\"\']\s*;', text, re.M))
    verified = bool(source.get("wechat_biz"))
    if verified and (biz_values != {source["wechat_biz"]} or author != source["name"]):
        raise CollectionError("公众号身份与已核实来源不符；未接收文章")
    event_title = re.sub(r"^讲座报名\s*[|｜]\s*", "", heading)
    events, flags = extract_events(event_title, content, url, source)
    stamps = set(re.findall(r'^\s*var ct\s*=\s*[\"\'](\d{10})[\"\']\s*;', text, re.M))
    published = datetime.fromtimestamp(int(next(iter(stamps))), ZoneInfo("Asia/Shanghai")).isoformat() if len(stamps) == 1 else None
    if not published:
        flags.append("published_time_unknown")
    images = len(bodies[0].xpath('.//img'))
    if images:
        flags.append("images_or_attachments_pending")
    return {"url": url, "title": heading, "body": content, "published_at": published, "visibility": "public",
            "processing_status": "pending_review", "events": events, "event": events[0] if events else None,
            "evidence": {"fetched_url": url, "extraction": "wechat_article_html", "warnings": flags,
                         "account_display_name": author, "account_identity_verified": verified,
                         "wechat_biz": source.get("wechat_biz"), "image_count": images,
                         "published_at_basis": "article var ct" if published else None}}


def recheck_items(store, source_id, checked_at, now):
    items = {}
    rows = store.db.execute("""SELECT d.url,d.title,d.first_seen_at,d.fetched_at,e.data,
        f.event_id AS favorite FROM documents d JOIN event_sources es ON es.document_id=d.id
        JOIN events e ON e.id=es.event_id LEFT JOIN favorites f ON f.event_id=e.id
        WHERE d.source_id=? AND d.visibility='public'""", (source_id,))
    for row in rows:
        event = json.loads(row["data"])
        status = status_at(event, now)
        recent = instant(row["first_seen_at"]) >= now - timedelta(days=30)
        if status not in {"scheduled_registration_unknown", "deadline_open", "ongoing"} and not (
                recent and status in {"unknown", "needs_review", "started_end_unknown", "postponed"}):
            continue
        item = items.setdefault(row["url"], {"url": row["url"], "title": row["title"],
            "checked_at": checked_at.get(row["url"], row["fetched_at"]), "favorite": False})
        item["favorite"] |= row["favorite"] is not None
    return sorted(items.values(), key=lambda item: (instant(item["checked_at"]), not item["favorite"], item["url"]))


def collect_source(store, source, fetcher, *, limit=5, max_pages=2):
    if not source.get("enabled", False) or source.get("visibility", "public") != "public":
        raise CollectionError("Only explicitly enabled public sources can be collected")
    if source.get("adapter") == "weread_latest":
        from .weread import collect_latest
        return collect_latest(store, source)
    if source.get("adapter") not in DETAIL_PATHS:
        raise CollectionError("Source has no configured collection adapter")
    if type(limit) is not int or not 1 <= limit <= 1000 or type(max_pages) is not int or not 1 <= max_pages <= 20:
        raise CollectionError("Invalid collection budget")
    run_id = store.start_run(source["id"])
    allowed = source.get("allowed_hosts", [urlsplit(source["url"]).hostname])
    state_key = "collection:" + source["id"]
    state = store.setting(state_key, {})
    if state.get("entry") != source["url"]:
        state = {"entry": source["url"], "pages": {}, "pending_pages": [], "pending_documents": [], "checked_at": {}}
    known = {row[0] for row in store.db.execute("SELECT url FROM documents WHERE source_id=? AND body<>''", (source["id"],))}
    state["pending_documents"] = [item for item in state["pending_documents"] if item["url"] not in known]
    pending_urls = {item["url"] for item in state["pending_documents"]}
    url, seen_pages, seen_docs, errors = source["url"], set(), set(), []
    if max_pages == 1 and state.get("last_list_page") == url and state["pending_pages"]:
        url = state["pending_pages"][0]
    accepted_pages = []
    inserted = updated = count = rechecked = 0
    stop_reason = "pagination_end"
    cached_before = fetcher.cache_hits
    network_before = len(getattr(fetcher, "network_events", []))
    try:
        while url and len(seen_pages) < max_pages:
            seen_pages.add(url)
            raw, final_url = fetcher.get(url, allowed)
            items, next_url = parse_listing(raw, final_url, source)
            accepted_pages.append(final_url)
            if url in state["pending_pages"]:
                state["pending_pages"].remove(url)
            page = {"urls": [item["url"] for item in items], "next": next_url}
            previous = state["pages"].get(url)
            state["pages"][url] = page
            state["last_list_page"] = url
            discovered = []
            for item in items:
                if item["url"] in known or item["url"] in pending_urls:
                    continue
                pending_urls.add(item["url"])
                discovered.append(item)
            if url == source["url"]:
                state["pending_documents"] = discovered + state["pending_documents"]
            else:
                state["pending_documents"].extend(discovered)
            # Changed pages propagate a fresh overlap scan without losing the older frontier.
            if next_url and next_url not in seen_pages and page != previous:
                if next_url in state["pending_pages"]:
                    state["pending_pages"].remove(next_url)
                state["pending_pages"].insert(0, next_url)
            store.set_setting(state_key, state)
            url = next((page_url for page_url in state["pending_pages"] if page_url not in seen_pages), None)
    except CollectionError as error:
        stop_reason = "request_or_parse_failure"
        errors.append({"url": url, "error": str(error)})
        if url not in state["pending_pages"]:
            state["pending_pages"].append(url)
        store.set_setting(state_key, state)
    rechecks = recheck_items(store, source["id"], state["checked_at"], datetime.fromisoformat(utc_now()))
    if accepted_pages:
        reserved = min(len(rechecks), max(1, limit // 4)) if state["pending_documents"] and limit > 1 else 0
        fresh = state["pending_documents"][:limit - reserved]
        tasks = [(item, False) for item in fresh] + [(item, True) for item in rechecks[:limit - len(fresh)]]
        for item, is_recheck in tasks:
            seen_docs.add(item["url"])
            rechecked += is_recheck
            try:
                article, article_url = fetcher.get(item["url"], allowed)
                document = parse_document(article, article_url, source, item)
                result = store.save_document(source["id"], document)
                inserted += result == "inserted"
                updated += result == "updated"
                count += 1
            except CollectionError as error:
                errors.append({"url": item["url"], "error": str(error)})
                if not is_recheck:
                    state["pending_documents"].remove(item)
                    state["pending_documents"].append(item)
            else:
                if not is_recheck:
                    state["pending_documents"].remove(item)
            state["checked_at"][item["url"]] = utc_now()
            store.set_setting(state_key, state)
    chain, cursor = set(), source["url"]
    while cursor in state["pages"] and cursor not in chain:
        chain.add(cursor)
        cursor = state["pages"][cursor]["next"]
    if cursor is None:
        state["pending_pages"] = [page_url for page_url in state["pending_pages"] if page_url in chain]
        store.set_setting(state_key, state)
    if cursor in chain:
        stop_reason = "repeated_page"
    elif state["pending_documents"] and len(seen_docs) >= limit:
        stop_reason = "document_budget"
    elif state["pending_pages"] and len(seen_pages) >= max_pages:
        stop_reason = "page_budget"
    scan_complete = cursor is None and not state["pending_pages"]
    pending = bool(state["pending_pages"] or state["pending_documents"] or not scan_complete)
    status = "partial" if count and errors else "failed" if errors else "success"
    if status == "failed":
        stop_reason = "request_or_parse_failure"
    coverage = {"list_pages": accepted_pages, "attempted_list_pages": sorted(seen_pages), "page_budget": max_pages,
                "document_budget": limit, "stop_reason": stop_reason,
                "attempted_documents": len(seen_docs), "rechecked_documents": rechecked,
                "pending_rechecks": max(0, len(rechecks) - rechecked), "unknown_recheck_window_days": 30,
                "pending_list_pages": list(state["pending_pages"]), "pending_documents": len(state["pending_documents"]),
                "collection_pending": pending, "listing_scan_complete": scan_complete,
                "historical_pagination_complete": not pending and not errors,
                "scope": "observed_public_listing_links", "cache_hits": fetcher.cache_hits - cached_before,
                "network_requests": getattr(fetcher, "network_events", [])[network_before:]}
    store.finish_run(run_id, status, pages=len(accepted_pages), documents=count, inserted=inserted,
                     updated=updated, errors=errors, coverage=coverage)
    return {"run_id": run_id, "source_id": source["id"], "status": status, "documents": count,
            "inserted": inserted, "updated": updated, "errors": errors, "coverage": coverage}


def discover(source, fetcher):
    allowed = source.get("allowed_hosts", [urlsplit(source["url"]).hostname])
    raw, url = fetcher.get(source["url"], allowed)
    tree = page_tree(raw)
    candidates = {}
    for anchor in tree.xpath("//a[@href]"):
        name = compact(anchor.text_content())
        if not re.search(r"学术|讲座|活动|竞赛|通知|交流|研究生|学生|创新|就业|社团|志愿|奖助", name):
            continue
        try:
            target = normalize_url(urljoin(url, anchor.get("href")))
        except (ValueError, CollectionError):
            continue
        candidates[target] = {"name": name, "url": target, "discovered_from": url,
                              "status": "candidate", "external": urlsplit(target).hostname not in allowed}
    return {"source_id": source["id"], "checked_at": utc_now(), "pages_read": 1,
            "candidates": list(candidates.values())[:100], "links_followed": 0}
