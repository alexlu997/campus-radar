"""Latest public ME article using the user's local WeRSS WeRead authorization."""

import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .collector import CollectionError, MAX_BYTES, compact, parse_wechat_document
from .storage import default_db


WERSS_HOME = default_db().parent / "WeRSS"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CollectionError("微信读书返回重定向；未转发登录凭据")


def remote(path, params, cookie):
    if path not in {"/api/mp/cover", "/web/mp/content"}:
        raise CollectionError("未授权的微信读书接口")
    request = Request("https://weread.qq.com" + path + "?" + urlencode(params), headers={
        "Cookie": cookie, "User-Agent": "Mozilla/5.0", "Origin": "https://weread.qq.com", "Referer": "https://weread.qq.com/"})
    for attempt in range(2):
        try:
            with build_opener(NoRedirect()).open(request, timeout=25) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise CollectionError("微信读书响应超出 4 MiB 上限；未接收文章")
            return raw
        except HTTPError as error:
            if error.code in {401, 403}:
                raise CollectionError("微信读书授权不可用，请在本机 WeRSS 重新扫码") from None
            raise CollectionError(f"微信读书返回 HTTP {error.code}；本轮停止") from None
        except (URLError, OSError):
            if attempt:
                raise CollectionError("微信读书网络或 TLS 请求失败；旧数据保留") from None
            time.sleep(2)


def payload(raw):
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        code = int(value.get("errCode", value.get("errcode", 0)) or 0)
    except (ValueError, TypeError):
        raise CollectionError("微信读书返回格式异常") from None
    if code in {-2010, -2012}:
        raise CollectionError("微信读书授权过期，请在本机 WeRSS 重新扫码")
    if code:
        raise CollectionError(f"微信读书接口返回错误码 {code}；本轮停止，旧数据保留")
    return value


def read_latest(source, cookie):
    if (source.get("id"), source.get("book_id"), source.get("wechat_biz"), source.get("name")) != (
            "wechat-me-liyuan", "MP_WXS_3519281128", "MzUxOTI4MTEyOA==", "ME砺远学术"):
        raise CollectionError("仅支持本轮已核实的 ME砺远学术账号")
    cover = payload(remote("/api/mp/cover", {"bookId": source["book_id"]}, cookie))
    review = cover.get("reviewId", "")
    if cover.get("name") != source["name"] or not isinstance(review, str) or not re.fullmatch(
            re.escape(source["book_id"]) + r"_[A-Za-z0-9_~\-]{10,100}", review):
        raise CollectionError("最新文章入口未返回已核实的公众号身份与文章编号")
    token = review[len(source["book_id"]) + 1:].replace("~", "_")
    time.sleep(2)
    raw = remote("/web/mp/content", {"reviewId": review}, cookie)
    if raw.lstrip().startswith(b"{"):
        payload(raw)
        raise CollectionError("微信读书未返回文章正文")
    document = parse_wechat_document(raw, "https://mp.weixin.qq.com/s/" + token, source)
    if document["title"] != compact(cover.get("title", "")):
        raise CollectionError("正文标题与最新文章入口不一致；未接收文章")
    document["evidence"]["transport"] = "weread_latest"
    return document


def fetch_latest(source):
    try:
        result = subprocess.run([str(WERSS_HOME / "venv/bin/python"), "-m", "campus_radar.weread"],
            input=json.dumps(source), text=True, capture_output=True, timeout=120,
            cwd=Path(__file__).resolve().parent.parent)
        value = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise CollectionError("本机 WeRSS 读取器不可用或超时；请检查独立环境与登录状态") from None
    if result.returncode or "error" in value:
        raise CollectionError(value.get("error", "本机 WeRSS 读取失败"))
    return value


def collect_latest(store, source):
    run_id = store.start_run(source["id"])
    errors, inserted, updated, count = [], 0, 0, 0
    try:
        document = fetch_latest(source)
        outcome = store.save_document(source["id"], document)
        count, inserted, updated = 1, int(outcome == "inserted"), int(outcome == "updated")
    except CollectionError as error:
        errors.append({"error": str(error)})
    status = "failed" if errors else "success"
    # shortcut: only the latest article is available; history needs a separately verified access path.
    coverage = {"scope": "latest_article_only", "historical_pagination_complete": False,
                "document_budget": 1, "stop_reason": "request_or_parse_failure" if errors else "latest_only"}
    store.finish_run(run_id, status, pages=0, documents=count, inserted=inserted, updated=updated, errors=errors, coverage=coverage)
    return {"run_id": run_id, "source_id": source["id"], "status": status, "documents": count,
            "inserted": inserted, "updated": updated, "errors": errors, "coverage": coverage}


def main():
    try:
        import yaml
        config = yaml.safe_load((WERSS_HOME / "source/data/wx.lic").read_text())
        accounts = config.get("weread_data", {}).get("accounts", [])
        if not accounts or not accounts[0].get("cookie"):
            raise CollectionError("尚无微信读书授权，请在本机 WeRSS 扫码")
        document = read_latest(json.load(sys.stdin), accounts[0]["cookie"])
        print(json.dumps(document, ensure_ascii=False))
        return 0
    except CollectionError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
    except Exception:
        print(json.dumps({"error": "本机 WeRSS 配置或读取器异常；未接收文章"}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
