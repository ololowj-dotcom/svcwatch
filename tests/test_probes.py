import socket
from contextlib import closing

from conftest import make_cfg

from svcwatch.probes import check_http, check_process, check_tcp


def http_cfg(url, extra=""):
    return make_cfg(f'[[http]]\nname = "x"\nurl = "{url}"\ntimeout = 3\n{extra}').http[0]


def free_port():
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_http_ok(pages):
    ok, detail = check_http(http_cfg(pages.base + "/ok"))
    assert ok and "HTTP 200" in detail


def test_http_wrong_status(pages):
    ok, detail = check_http(http_cfg(pages.base + "/error"))
    assert not ok and "503" in detail and "expected 200" in detail


def test_http_accepts_listed_statuses(pages):
    assert check_http(http_cfg(pages.base + "/error", "expect_status = [200, 503]"))[0]


def test_http_body_must_contain_text(pages):
    assert check_http(http_cfg(pages.base + "/ok", 'contains = "systems ok"'))[0]
    ok, detail = check_http(http_cfg(pages.base + "/ok", 'contains = "database"'))
    assert not ok and "does not contain" in detail


def test_http_redirect_followed_by_default_and_reported_when_expected(pages):
    assert check_http(http_cfg(pages.base + "/redirect"))[0]
    assert check_http(http_cfg(pages.base + "/redirect", "expect_status = 302"))[0]
    ok, detail = check_http(http_cfg(pages.base + "/redirect", "expect_status = 301"))
    assert not ok and "302" in detail


def test_http_connection_refused_is_a_failure_not_an_exception():
    ok, detail = check_http(http_cfg(f"http://127.0.0.1:{free_port()}/"))
    assert not ok and "127.0.0.1" in detail


def test_http_404(pages):
    assert not check_http(http_cfg(pages.base + "/missing"))[0]


def test_tcp_open_and_closed():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        cfg = make_cfg(f'[[tcp]]\nname = "s"\nhost = "127.0.0.1"\nport = {port}\ntimeout = 2').tcp[0]
        assert check_tcp(cfg)[0]
    finally:
        srv.close()
    closed = make_cfg(f'[[tcp]]\nname = "s"\nhost = "127.0.0.1"\nport = {free_port()}\ntimeout = 2').tcp[0]
    ok, detail = check_tcp(closed)
    assert not ok and "127.0.0.1" in detail


def proc(min_count=1, max_count=0):
    extra = f"min_count = {min_count}\nmax_count = {max_count}"
    return make_cfg(f'[[process]]\nname = "w"\npattern = "app.worker"\n{extra}').process[0]


def test_process_counts_matches(runner):
    runner.on("pgrep -f", stdout="101\n102\n")
    assert check_process(proc(), runner)[0]
    ok, detail = check_process(proc(min_count=3), runner)
    assert not ok and "expected at least 3" in detail
    ok, detail = check_process(proc(max_count=1), runner)
    assert not ok and "at most 1" in detail


def test_process_none_running(runner):
    runner.on("pgrep -f", stdout="", returncode=1)
    ok, detail = check_process(proc(), runner)
    assert not ok and "0 process" in detail


def test_process_excludes_own_pid(runner):
    import os
    runner.on("pgrep -f", stdout=f"{os.getpid()}\n")
    assert not check_process(proc(), runner)[0]


def test_process_pgrep_missing_or_broken(runner):
    runner.missing("pgrep")
    ok, detail = check_process(proc(), runner)
    assert not ok and "procps" in detail
    from conftest import FakeRunner
    r2 = FakeRunner()
    r2.on("pgrep", stderr="bad option", returncode=2)
    ok, detail = check_process(proc(), r2)
    assert not ok and "pgrep failed" in detail
