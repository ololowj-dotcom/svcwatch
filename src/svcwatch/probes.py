from __future__ import annotations

import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Tuple

from .config import HttpCheck, ProcessCheck, TcpCheck
from .runner import CommandNotFound, Runner

USER_AGENT = "svcwatch/1.0"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_http(chk: HttpCheck) -> Tuple[bool, str]:
    ctx = ssl.create_default_context()
    if not chk.verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    handlers = [urllib.request.HTTPSHandler(context=ctx)]
    if any(300 <= s < 400 for s in chk.expect_status):
        handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(chk.url, headers={"User-Agent": USER_AGENT})
    started = time.monotonic()
    try:
        with opener.open(req, timeout=chk.timeout) as resp:
            status = resp.status
            body = resp.read(1_000_000) if chk.contains else b""
    except urllib.error.HTTPError as exc:
        status, body = exc.code, (exc.read(1_000_000) if chk.contains else b"")
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"{chk.url}: {reason}"
    took = time.monotonic() - started
    if status not in chk.expect_status:
        return False, f"{chk.url}: HTTP {status} (expected {', '.join(map(str, chk.expect_status))})"
    if chk.contains and chk.contains.encode("utf-8") not in body:
        return False, f"{chk.url}: HTTP {status} but the body does not contain {chk.contains!r}"
    return True, f"HTTP {status} in {took * 1000:.0f} ms"


def check_tcp(chk: TcpCheck) -> Tuple[bool, str]:
    started = time.monotonic()
    try:
        with socket.create_connection((chk.host, chk.port), timeout=chk.timeout):
            pass
    except (OSError, socket.timeout) as exc:
        return False, f"{chk.host}:{chk.port}: {exc}"
    return True, f"connected in {(time.monotonic() - started) * 1000:.0f} ms"


def check_process(chk: ProcessCheck, runner: Runner) -> Tuple[bool, str]:
    try:
        res = runner.run(["pgrep", "-f", "--", chk.pattern])
    except CommandNotFound:
        return False, "pgrep is not installed (apt install procps)"
    if res.returncode not in (0, 1):
        return False, f"pgrep failed: {res.stderr.strip()}"
    me = str(os.getpid())
    pids = [p for p in res.stdout.split() if p.isdigit() and p != me]
    count = len(pids)
    if count < chk.min_count:
        return False, f"{count} process(es) match {chk.pattern!r}, expected at least {chk.min_count}"
    if chk.max_count and count > chk.max_count:
        return False, f"{count} process(es) match {chk.pattern!r}, expected at most {chk.max_count}"
    return True, f"{count} process(es) running"
