"""Public-site search and a candidate inbox; only verified adapters fetch bodies."""

from datetime import datetime
import re
from urllib.parse import urlencode, urlsplit

from .collector import CollectionError, DETAIL_PATHS, Fetcher, normalize_url, page_tree, parse_document, parse_listing, parse_wechat_document
from .storage import utc_now


def add_candidate(store, url, *, title="", origin="manual"):
    url = normalize_url(url)
    host = urlsplit(url).hostname
    if not (host == "sjtu.edu.cn" or host.endswith(".sjtu.edu.cn") or host == "mp.weixin.qq.com"):
        raise ValueError("This version accepts SJTU public-site and mp.weixin.qq.com article links")
    with store.db:
        store.db.execute("""INSERT INTO candidates(url,title,origin,discovered_at,status) VALUES(?,?,?,?,'candidate')
            ON CONFLICT(url) DO UPDATE SET title=CASE WHEN candidates.title='' THEN excluded.title ELSE candidates.title END""",
            (url, title[:300], origin[:500], utc_now()))
    return dict(store.db.execute("SELECT * FROM candidates WHERE url=?", (url,)).fetchone())


def search_public(store, query, *, limit=20, fetcher=None):
    if not query.strip() or len(query) > 100 or type(limit) is not int or not 1 <= limit <= 30:
        raise ValueError("Use a nonempty query of at most 100 characters and limit 1–30")
    source = next(s for s in store.source_states() if s.get("adapter") == "ctld")
    url = source["url"] + "?" + urlencode({"keyword": query.strip()})
    fetcher = fetcher or Fetcher(store.path.parent / "public-page-cache", timeout=10, retries=0, cache_ttl=0, direct_hosts=source["allowed_hosts"])
    result = {"provider": "ctld_official_search", "query": query, "searched_at": utc_now(), "url": url,
              "scope": "教学发展中心官网活动栏目；不是全网搜索", "candidates": []}
    try:
        raw, final = fetcher.get(url, source["allowed_hosts"])
        tree = page_tree(raw)
        try:
            items, following = parse_listing(raw, final, source)
        except CollectionError:
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            if tree.xpath('//*[contains(concat(" ",@class," ")," event-list ")]') and re.search(r"laypage\.render\(\{\s*elem:\s*['\"]pages['\"]\s*,\s*count:\s*0\s*,", text):
                items, following = [], None
            else:
                raise
        result["candidates"] = [add_candidate(store, i["url"], title=i["title"], origin="ctld search: " + query) for i in items[:limit]]
        result.update(status="success", next_page=following, result_count=len(items), bodies_fetched=0)
    except (CollectionError, OSError) as error:
        result.update(status="failed", error=str(error)[:300])
    store.set_setting("discovery_last_search", result)
    return result


def fetch_candidate(store, url, *, fetcher=None):
    candidate = add_candidate(store, url)
    url = candidate["url"]
    host, path = urlsplit(url).hostname, urlsplit(url).path
    source = next((s for s in store.source_states() if s.get("visibility") == "public"
                   and host in s.get("allowed_hosts", []) and s.get("adapter") in DETAIL_PATHS
                   and DETAIL_PATHS[s["adapter"]].match(path)), None)
    try:
        if not source and host != "mp.weixin.qq.com":
            raise CollectionError("No verified public article adapter for this URL; candidate link retained")
        allowed = source["allowed_hosts"] if source else ["mp.weixin.qq.com"]
        fetcher = fetcher or Fetcher(store.path.parent / "public-page-cache", timeout=10, retries=0, cache_ttl=0,
                                    direct_hosts=allowed if source and source.get("network_mode") == "direct" else [])
        raw, final = fetcher.get(url, allowed)
        if source:
            document = parse_document(raw, final, source)
        else:
            source = {"id": "wechat-public-links", "name": "手工添加的公众号文章（账号待核实）", "visibility": "public", "enabled": False,
                      "category": "待分类", "allowed_hosts": ["mp.weixin.qq.com"], "adapter": None}
            document = parse_wechat_document(raw, final, source)
            store.sync_sources([source])
        outcome = store.save_document(source["id"], document)
        document_id = store.db.execute("SELECT id FROM documents WHERE url=?", (document["url"],)).fetchone()[0]
        with store.db:
            store.db.execute("UPDATE candidates SET status='fetched',error=NULL,document_id=?,title=? WHERE url=?", (document_id, document["title"], url))
        return {**dict(store.db.execute("SELECT * FROM candidates WHERE url=?", (url,)).fetchone()), "outcome": outcome}
    except (CollectionError, OSError) as error:
        with store.db:
            store.db.execute("UPDATE candidates SET status=?,error=? WHERE url=?", ("refresh_failed" if candidate["document_id"] else "failed", str(error)[:300], url))
        return dict(store.db.execute("SELECT * FROM candidates WHERE url=?", (url,)).fetchone())
