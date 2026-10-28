"""Local, human-driven login preparation. Never exports browser state or page text."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit


ENTRIES = {
    "dekt": "https://dekt.sjtu.edu.cn/h5/",
    "activity": "https://activity.sjtu.edu.cn/",
}
AUTH_ROOT = Path.home() / "Library/Application Support/Campus Radar/auth"
MARKER = ".campus-radar-auth"
COUNTS = ("links", "images", "list_items", "table_rows", "iframes", "password_inputs")
FIELDS = ("activity_id", "starts_at", "location", "eligibility", "registration_status")
DOM_COUNTS = """() => ({
  links: document.querySelectorAll('a').length,
  images: document.querySelectorAll('img').length,
  list_items: document.querySelectorAll('li').length,
  table_rows: document.querySelectorAll('tr').length,
  iframes: document.querySelectorAll('iframe').length,
  password_inputs: document.querySelectorAll('input[type=password]').length
})"""


def profile_path(source: str, root: Path = AUTH_ROOT) -> Path:
    if source not in ENTRIES:
        raise ValueError("Unknown source")
    target = root / source
    if any(part.is_symlink() for part in (target, *target.parents)):
        raise ValueError("Symlinked profile path is not allowed")
    return target


def prepare_profile(source: str, root: Path = AUTH_ROOT) -> Path:
    target = profile_path(source, root)
    marker = target / MARKER
    expected = f"campus-radar-auth-v1:{source}\n"
    if target.exists():
        if marker.is_symlink() or not marker.is_file() or marker.read_text() != expected:
            raise ValueError("Refusing an unmarked browser directory")
    else:
        target.mkdir(parents=True, mode=0o700)
        with marker.open("x") as handle:
            handle.write(expected)
    root.chmod(0o700)
    target.chmod(0o700)
    marker.chmod(0o600)
    return target


def clear_profile(source: str, root: Path = AUTH_ROOT) -> bool:
    target = profile_path(source, root)
    if not target.exists():
        return False
    marker = target / MARKER
    if marker.is_symlink() or not marker.is_file():
        raise ValueError("Refusing an unmarked browser directory")
    if marker.read_text() != f"campus-radar-auth-v1:{source}\n":
        raise ValueError("Profile ownership marker does not match")
    # Chromium's lock may be a dangling symlink while its process is running.
    lock = target / "SingletonLock"
    if lock.exists() or lock.is_symlink():
        raise ValueError("Close this project's browser before clearing authentication")
    shutil.rmtree(target)
    return True


def page_scope(source: str, url: str) -> str:
    """Return a fixed classification; never return a URL, path, query or fragment."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            return "unexpected"
        if parsed.port not in (None, 443):
            return "unexpected"
        if parsed.hostname == urlsplit(ENTRIES[source]).hostname:
            return "selected_source"
        if parsed.hostname == "jaccount.sjtu.edu.cn":
            return "authentication"
    except (KeyError, ValueError):
        pass
    return "unexpected"


def report(source: str, status: str, **extra: object) -> dict:
    result = {
        "source": source,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "login_state": "unverified",
        "fields": dict.fromkeys(FIELDS, "not_verified"),
        "pagination": "not_verified",
        "access_scope": "not_verified",
        "api": "not_observed",
    }
    result.update(extra)
    return result


def inspect_structure(source: str, page, page_kind: str) -> dict:
    scope = page_scope(source, page.url)
    if scope != "selected_source":
        return report(source, "authentication_required" if scope == "authentication" else "unexpected_page")
    observed = page.evaluate(DOM_COUNTS)
    # Only bounded integer counts cross the browser-to-report boundary.
    counts = {
        key: min(observed.get(key, 0), 1_000_000)
        if type(observed.get(key)) is int and observed[key] >= 0
        else 0
        for key in COUNTS
    }
    if counts["password_inputs"]:
        return report(source, "authentication_required")
    return report(
        source,
        "structure_observed",
        user_selected_page_kind=page_kind,
        pages_observed=1,
        counts=counts,
        activity_content="not_verified",
    )


def browser_session(source: str, command: str, page_kind: str) -> dict:
    # Playwright debug output can include protocol data; do not inherit it here.
    os.environ.pop("DEBUG", None)
    os.environ.pop("PWDEBUG", None)
    from playwright.sync_api import sync_playwright

    profile = prepare_profile(source)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=False, no_viewport=True,
            accept_downloads=False, chromium_sandbox=True,
        )
        try:
            page = context.new_page()
            page.goto(ENTRIES[source], wait_until="domcontentloaded", timeout=30_000)
            if command == "login":
                print("请在刚打开的独立浏览器中自行完成交大认证。程序不读取密码、验证码或认证请求。")
                answer = input("完成后输入 DONE 关闭并保留本项目会话；其他输入取消： ")
                return report(source, "login_user_confirmed_not_verified" if answer == "DONE" else "cancelled")
            print("请在刚打开的同一标签页手动进入有权查看的活动列表或详情；不要点击报名、申请或取消。")
            answer = input("确认当前是所选活动页面后输入 READ；程序只读取结构计数，其他输入取消： ")
            if answer != "READ":
                return report(source, "cancelled")
            return inspect_structure(source, page, page_kind)
        finally:
            context.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Campus Radar 本机人工登录与脱敏结构验证；不提取活动正文。")
    parser.add_argument("command", choices=("login", "probe", "clear"))
    parser.add_argument("--source", choices=tuple(ENTRIES), required=True)
    parser.add_argument("--page-kind", choices=("list", "detail"), default="list")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "clear":
            result = report(args.source, "cleared" if clear_profile(args.source) else "already_absent")
        else:
            if not os.isatty(0):
                result = report(args.source, "local_interactive_terminal_required")
            else:
                result = browser_session(args.source, args.command, args.page_kind)
    except ModuleNotFoundError:
        result = report(args.source, "playwright_not_installed")
    except KeyboardInterrupt:
        result = report(args.source, "cancelled")
    except Exception:
        # Exception strings can contain redirects, credentials or browser paths.
        result = report(args.source, "failed_no_sensitive_diagnostics")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {
        "cleared", "already_absent", "login_user_confirmed_not_verified", "structure_observed"
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
