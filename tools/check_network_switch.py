"""One local, bounded collection session; never changes system network settings."""

import argparse
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from campus_radar.collector import Fetcher, collect_source
from campus_radar.storage import Store


class LocalController(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(2)
        self.sock.connect(f"/var/run/clash-verge-service/users/{os.getuid()}/verge-mihomo.sock")


def network_state():
    raw = subprocess.run(["/usr/sbin/scutil", "--proxy"], capture_output=True,
                         text=True, check=True, timeout=3).stdout
    flags = {key: int(match[1]) if (match := re.search(r"\b" + key + r"\s*:\s*(\d+)", raw)) else 0
             for key in ("HTTPEnable", "HTTPSEnable", "SOCKSEnable")}
    tun = None
    client = LocalController("localhost")
    try:
        client.request("GET", "/configs")
        response = client.getresponse()
        if response.status == 200:
            tun = json.loads(response.read(1024 * 1024)).get("tun", {}).get("enable")
    except (OSError, ValueError, http.client.HTTPException):
        pass
    finally:
        client.close()
    mode = "on" if any(flags.values()) and tun is True else "off" if not any(flags.values()) and tun is False else "mixed_or_unknown"
    return {"at": datetime.now(timezone.utc).isoformat(), "proxy_flags": flags, "tun": tun, "mode": mode}


def summarize(rows, source_ids):
    coverage = {mode: set() for mode in ("on", "off")}
    sequence = []
    restored = set()
    errors = 0
    for row in rows:
        before, after = row["before"]["mode"], row["after"]["mode"]
        for mode in (before, after):
            if mode in coverage and (not sequence or sequence[-1] != mode):
                sequence.append(mode)
        result = row["result"]
        if result["status"] != "success":
            errors += 1
        elif before == after and before in coverage:
            coverage[before].add(result["source_id"])
            if before == "on" and "off" in sequence:
                restored.add(result["source_id"])
    switched = any(sequence[i:i + 3] == ["on", "off", "on"] for i in range(len(sequence) - 2))
    return {"stable_state_sources": {k: sorted(v) for k, v in coverage.items()},
            "restored_sources": sorted(restored),
            "observed_sequence": sequence, "failed_or_partial_runs": errors,
            "on_off_on_observed": switched,
            "passed": switched and not errors and all(set(source_ids) <= v for v in [*coverage.values(), restored])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=180)
    args = parser.parse_args()
    if not 10 <= args.seconds <= 600:
        parser.error("--seconds must be between 10 and 600")
    sources = [s for s in json.loads((ROOT / "data/sources.json").read_text())["sources"] if s.get("enabled")]
    assert len(sources) == 3 and all(s.get("network_mode") == "direct" for s in sources)
    out = ROOT / "outputs" / "network-switch" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
    out.mkdir(parents=True)
    fetcher = Fetcher(out / "cache", direct_hosts=[h for s in sources for h in s["allowed_hosts"]], cache_ttl=0)
    rows = []
    deadline = time.monotonic() + args.seconds
    print(f"本地测试已开始，持续约 {args.seconds} 秒；结果目录：{out}", flush=True)
    print("保持梯子开启，等三个来源均成功后关闭系统代理和 TUN 约 45 秒，再重新开启。期间不用回复 GPT。", flush=True)
    print("本脚本只读取网络状态；请手动恢复原设置。Ctrl+C 可停止，已完成记录仍保留。", flush=True)
    try:
        with Store(out / "public.sqlite3") as store:
            store.sync_sources(sources)
            index = 0
            while time.monotonic() < deadline:
                source = sources[index % len(sources)]
                before = network_state()
                result = collect_source(store, source, fetcher, limit=1, max_pages=1)
                row = {"before": before, "after": network_state(), "result": result}
                rows.append(row)
                with (out / "runs.jsonl").open("a") as log:
                    log.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(f"{row['after']['at']}  {before['mode']} → {row['after']['mode']}  {source['id']}  {result['status']}", flush=True)
                if summarize(rows, [s["id"] for s in sources])["passed"]:
                    break
                index += 1
                time.sleep(min(5, max(0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        print("已停止。请确认已手动恢复原代理和 TUN 设置。", flush=True)
    finally:
        summary = summarize(rows, [s["id"] for s in sources])
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
