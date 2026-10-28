"""Request-local macOS direct transport; never changes proxy, DNS or routes."""

from email.parser import Parser
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import urlencode, urljoin, urlsplit


# Public resolver bootstrap, not a pinned campus address; HTTPS verifies this name.
DNS_URL = "https://dns.alidns.com/resolve"
DNS_BOOTSTRAP = "dns.alidns.com:443:223.5.5.5,223.6.6.6"


class TransportError(Exception):
    def __init__(self, message, *, retryable=True):
        super().__init__(message)
        self.retryable = retryable


def physical_interface():
    result = subprocess.run(["/usr/sbin/scutil"], input="show State:/Network/Global/IPv4\n",
                            capture_output=True, text=True, timeout=5)
    match = re.search(r"PrimaryInterface\s*:\s*(en\d+)\b", result.stdout)
    if result.returncode or not match:
        raise TransportError("No active physical IPv4 interface; network may be changing")
    return match[1]


class DirectTransport:
    def __init__(self, timeout, max_bytes, user_agent):
        self.timeout, self.max_bytes, self.user_agent = timeout, max_bytes, user_agent
        self.dns = {}
        self.events = []

    def _curl(self, url, interface, resolve, *, max_bytes):
        with tempfile.TemporaryDirectory(prefix="campus-radar-http-") as folder:
            body, headers = Path(folder) / "body", Path(folder) / "headers"
            command = ["/usr/bin/curl", "--disable", "--silent", "--show-error", "--ipv4",
                       "--interface", "if!" + interface, "--noproxy", "*", "--resolve", resolve,
                       "--proto", "=https", "--connect-timeout", str(min(8, self.timeout)),
                       "--max-time", str(self.timeout), "--max-filesize", str(max_bytes),
                       "--user-agent", self.user_agent, "--output", str(body),
                       "--dump-header", str(headers), "--write-out", "%{json}", url]
            result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout + 3)
            try:
                info = json.loads(result.stdout)
            except ValueError:
                info = {}
            self.events.append({"url": url, "path": "physical_direct", "interface": interface,
                                "curl_exit": result.returncode, "http_status": info.get("http_code"),
                                "tls_verified": result.returncode == 0 and info.get("ssl_verify_result") == 0,
                                "seconds": info.get("time_total")})
            if result.returncode:
                raise TransportError(f"Direct request failed (curl {result.returncode})",
                                     retryable=result.returncode not in {60, 63})
            if info.get("ssl_verify_result") != 0:
                raise TransportError("TLS verification did not succeed", retryable=False)
            raw = body.read_bytes()
            if len(raw) > max_bytes:
                raise TransportError("Response exceeds the page budget", retryable=False)
            blocks = headers.read_text(errors="replace").strip().split("\n\n")
            message = Parser().parsestr(blocks[-1].split("\n", 1)[-1])
            return raw, int(info.get("http_code", 0)), message

    def resolve(self, host, interface):
        key = (interface, host)
        cached = self.dns.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        raw, status, _ = self._curl(DNS_URL + "?" + urlencode({"name": host, "type": "A"}),
                                    interface, DNS_BOOTSTRAP, max_bytes=65536)
        if status != 200:
            raise TransportError(f"Independent DNS returned HTTP {status}")
        answer = json.loads(raw)
        if answer.get("Status") != 0:
            raise TransportError("Independent DNS did not resolve the public source")
        addresses, ttls = [], []
        for record in answer.get("Answer", []):
            if record.get("type") != 1:
                continue
            address = ipaddress.ip_address(record["data"])
            if address.version != 4 or not address.is_global:
                raise TransportError("DNS returned a non-public address", retryable=False)
            addresses.append(str(address))
            ttls.append(max(0, int(record.get("TTL", 0))))
        if not addresses:
            raise TransportError("Independent DNS has no public IPv4 answer")
        self.dns[key] = (time.monotonic() + min(ttls), addresses)
        return addresses

    def get(self, url, allowed_hosts):
        try:
            interface = physical_interface()
            for _ in range(4):
                parts = urlsplit(url)
                if (parts.scheme != "https" or parts.hostname not in allowed_hosts or
                        parts.username or parts.password or parts.port not in {None, 443}):
                    raise TransportError("Direct URL or redirect leaves the verified HTTPS sources", retryable=False)
                addresses = self.resolve(parts.hostname, interface)
                raw, status, headers = self._curl(url, interface, f"{parts.hostname}:443:{','.join(addresses)}",
                                                   max_bytes=self.max_bytes)
                if status in {301, 302, 303, 307, 308} and headers.get("Location"):
                    url = urljoin(url, headers["Location"])
                    continue
                if not 200 <= status < 300:
                    raise TransportError(f"HTTP {status}; no page accepted",
                                         retryable=status in {408, 500, 502, 503, 504})
                return raw, url, headers.get_content_type()
            raise TransportError("Redirect budget exceeded", retryable=False)
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            raise TransportError(f"Direct transport failed ({type(error).__name__})") from None
        finally:
            # Bound memory when a laptop moves between physical interfaces.
            self.dns = {key: value for key, value in self.dns.items() if value[0] > time.monotonic()}
