import threading

import pytest
from conftest import Clock, FakeNotifier, FakeRunner, journal, make_cfg, show

from svcwatch import monitor as monitor_mod
from svcwatch.models import CRITICAL, OK, WARNING
from svcwatch.monitor import Monitor, fmt_duration, mute_until, parse_duration, set_mute
from svcwatch.runner import CmdResult


class Feed:
    def __init__(self, history=()):
        self.n = 0
        self.pending = []
        self.history = list(history)

    def push(self, *msgs):
        self.pending.extend(msgs)

    def __call__(self, cmd):
        if any(a.startswith("--after-cursor") for a in cmd):
            msgs, self.pending = self.pending, []
            out = journal(*msgs, start=self.n + 1) if msgs else ""
            self.n += len(msgs)
            return CmdResult(0, out, "")
        msgs = self.history + ["anchor"]
        out = journal(*msgs, start=self.n + 1)
        self.n += len(msgs)
        return CmdResult(0, out, "")


class World:
    def __init__(self, tmp_path, toml="", units=("api",), notifiers=None, history=()):
        self.runner = FakeRunner()
        self.clock = Clock()
        self.feed = Feed(history)
        self.tmp_path = tmp_path
        self.toml = toml
        self.units = units
        self.notifier = FakeNotifier()
        self.extra = notifiers
        self.set_state("active")
        for u in units:
            self.runner.on(f"journalctl -u {u}.service", fn=self.feed)
        self.cfg = make_cfg(
            f'[systemd]\ndiscover = "off"\nunits = {list(units)!r}\n'.replace("'", '"') + toml, tmp_path)
        self.mon = self.new_monitor()

    def new_monitor(self, **kw):
        notifiers = self.extra if self.extra is not None else [self.notifier]
        return Monitor(self.cfg, runner=self.runner, notifiers=notifiers, clock=self.clock, **kw)

    def set_state(self, active="active", sub=None, restarts=0, result="success", load="loaded", unit=None):
        for u in ([unit] if unit else self.units):
            self.runner.on(f"systemctl show {u}.service", stdout=show(
                active, sub or ("running" if active == "active" else active), load, result, restarts))

    def tick(self, advance=20):
        self.clock.advance(advance)
        return self.mon.tick()

    @property
    def titles(self):
        return self.notifier.titles


ERR = ["Traceback (most recent call last):", '  File "app.py", line 10, in run', "ValueError: bad"]


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.tick()
    return w


def test_history_is_ignored_on_first_start(tmp_path):
    w = World(tmp_path, history=["Traceback old crash", "ValueError: old"])
    assert w.tick() == []
    assert w.notifier.sent == []


def test_new_error_alerts_with_full_traceback(world):
    world.feed.push(*ERR)
    events = world.tick()
    assert [e.kind for e in events] == ["log_error"]
    ev = world.notifier.sent[0]
    assert ev.severity == CRITICAL and ev.title == "Error in api" and ev.target == "systemd:api"
    assert "ValueError: bad" in ev.body and "Traceback" in ev.body


def test_same_error_is_not_repeated_within_the_dedup_window(world):
    world.feed.push("Traceback ValueError: id 111 failed")
    world.tick()
    world.feed.push("Traceback ValueError: id 222 failed")
    world.tick()
    assert len(world.notifier.sent) == 1
    world.feed.push("Traceback ValueError: id 333 failed")
    world.tick(advance=3600)
    assert len(world.notifier.sent) == 2


def test_a_different_error_is_alerted_immediately(world):
    world.feed.push("Traceback A")
    world.tick()
    world.feed.push("Traceback B")
    world.tick()
    assert len(world.notifier.sent) == 2


def test_quiet_journal_produces_nothing(world):
    world.feed.push("GET /health 200", "processed 5 items")
    assert world.tick() == []


def test_dedup_survives_a_restart_of_svcwatch(world):
    world.feed.push("Traceback boom")
    world.tick()
    fresh = world.new_monitor()
    world.feed.push("Traceback boom")
    world.clock.advance(20)
    assert fresh.tick() == []
    assert len(world.notifier.sent) == 1


def test_cursor_survives_a_restart_so_nothing_is_read_twice(world):
    world.feed.push("Traceback once")
    world.tick()
    fresh = world.new_monitor()
    world.clock.advance(3600)
    assert fresh.tick() == []


def test_external_errors_are_counted_and_summarised_not_alerted(world):
    world.feed.push("upstream Bad Gateway", "upstream Bad Gateway", "Connection reset by peer")
    assert world.tick() == []
    assert world.mon.state.summary["systemd:api"] == 3
    events = world.tick(advance=86400)
    assert [e.kind for e in events] == ["summary"]
    assert "3" in events[0].body and "systemd:api" in events[0].body
    assert world.mon.state.summary == {}


def test_empty_summary_is_a_heartbeat_unless_disabled(tmp_path):
    w = World(tmp_path)
    w.tick()
    events = w.tick(advance=86400)
    assert events and "No external/network errors" in events[0].body
    w2 = World(tmp_path / "b", toml="[monitor]\nsummary_when_empty = false\n")
    (tmp_path / "b").mkdir(exist_ok=True)
    w2.tick()
    assert w2.tick(advance=86400) == []


def test_summary_can_be_turned_off(tmp_path):
    w = World(tmp_path, toml="[monitor]\nsummary_interval = 0\n")
    w.tick()
    assert w.tick(advance=10 * 86400) == []


def test_summary_mentions_what_is_down(world):
    world.set_state("failed")
    world.tick()
    body = world.tick(advance=86400)[-1].body
    assert "Currently down: api" in body and "systemd: 1" in body


def test_rule_adds_patterns_and_ignores_noise(tmp_path):
    w = World(tmp_path, toml=(
        '[[systemd.watch]]\nmatch = "api"\nlabel = "Payments"\nimmediate_extra = ["re:timeout after \\\\d+s"]\n'
        'ignore_extra = ["healthcheck"]\n'))
    w.tick()
    w.feed.push("healthcheck Exception ignored", "call timeout after 30s")
    events = w.tick()
    assert len(events) == 1 and events[0].title == "Error in Payments"
    assert "timeout after 30s" in events[0].body and "healthcheck" not in events[0].body


def test_rule_can_disable_log_scanning(tmp_path):
    w = World(tmp_path, toml='[[systemd.watch]]\nmatch = "api"\nlogs = false\n')
    w.tick()
    assert not w.runner.called("journalctl")
    w.set_state("failed")
    assert [e.kind for e in w.tick()] == ["down"]


def test_journal_priority_rule(tmp_path):
    w = World(tmp_path, toml='[[systemd.watch]]\nmatch = "api"\njournal_priority = 3\n')
    w.tick()
    w.feed.push(("disk is dying", 3), ("fine", 6))
    events = w.tick()
    assert len(events) == 1 and "disk is dying" in events[0].body


def test_rules_target_only_matching_units(tmp_path):
    w = World(tmp_path, units=("api", "worker"), toml='[[systemd.watch]]\nmatch = "worker"\nlogs = false\n')
    w.tick()
    assert w.runner.called("journalctl -u api.service")
    assert not w.runner.called("journalctl -u worker.service")


def test_alert_body_is_bounded(tmp_path):
    w = World(tmp_path, toml="[logs]\nmax_lines_per_alert = 5\n")
    w.tick()
    w.feed.push("Traceback " + "x" * 2000, *[f"detail line {i}" for i in range(30)])
    ev = w.tick()[0]
    assert len(ev.body.splitlines()) <= 7
    assert all(len(line) <= 420 for line in ev.body.splitlines())


def test_failed_service_alerts_once_then_recovers(world):
    world.set_state("failed", result="exit-code")
    events = world.tick()
    assert [(e.kind, e.severity) for e in events] == [("down", CRITICAL)]
    assert "failed" in events[0].body and "exit-code" in events[0].body
    assert world.tick() == [] and world.tick() == []
    world.set_state("active")
    events = world.tick(advance=100)
    assert [(e.kind, e.severity) for e in events] == [("recovered", OK)]
    assert "Was down for" in events[0].body
    assert world.tick() == []


def test_fail_threshold_needs_consecutive_failures(tmp_path):
    w = World(tmp_path, toml='[[systemd.watch]]\nmatch = "api"\nfail_threshold = 3\n')
    w.tick()
    w.set_state("failed")
    assert w.tick() == [] and w.tick() == []
    assert [e.kind for e in w.tick()] == ["down"]


def test_a_blip_below_the_threshold_never_alerts(tmp_path):
    w = World(tmp_path, toml='[[systemd.watch]]\nmatch = "api"\nfail_threshold = 3\n')
    w.tick()
    w.set_state("failed")
    w.tick()
    w.set_state("active")
    w.tick()
    w.set_state("failed")
    assert w.tick() == [] and w.tick() == []


def test_remind_after_repeats_the_alarm(tmp_path):
    w = World(tmp_path, toml="[monitor]\nremind_after = 600\n")
    w.tick()
    w.set_state("failed")
    assert [e.title for e in w.tick()] == ["Service down: api"]
    assert w.tick(advance=300) == []
    ev = w.tick(advance=400)
    assert ev and ev[0].title == "Still down: api" and "Down for" in ev[0].body


def test_inactive_is_ignored_unless_asked(tmp_path):
    w = World(tmp_path)
    w.tick()
    w.set_state("inactive", "dead")
    assert w.tick() == []
    w2 = World(tmp_path / "x", toml='[[systemd.watch]]\nmatch = "api"\nalert_on = ["failed", "inactive"]\n')
    (tmp_path / "x").mkdir(exist_ok=True)
    w2.tick()
    w2.set_state("inactive", "dead")
    assert [e.kind for e in w2.tick()] == ["down"]


def test_configured_but_missing_unit_is_reported(world):
    world.set_state("inactive", "dead", load="not-found")
    ev = world.tick()
    assert ev and "unit not found" in ev[0].body


def test_restart_loop_is_detected_even_though_the_unit_looks_healthy(world):
    world.set_state("active", restarts=0)
    assert world.tick() == []
    world.set_state("active", restarts=3)
    ev = world.tick()
    assert [(e.kind, e.severity) for e in ev] == [("restart", WARNING)]
    assert "3 times" in ev[0].title and "0 -> 3" in ev[0].body
    assert world.tick() == []
    world.set_state("active", restarts=0)
    assert world.tick() == []


def test_restart_alert_can_be_disabled(tmp_path):
    w = World(tmp_path, toml='[[systemd.watch]]\nmatch = "api"\nrestart_alert = false\n')
    w.tick()
    w.set_state("active", restarts=5)
    assert w.tick() == []


def test_old_systemd_without_nrestarts_is_fine(world):
    world.runner.on("systemctl show api.service", stdout=show(restarts=None))
    assert world.tick() == []


def test_unit_vanishing_from_discovery_is_forgotten(tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    (units / "api.service").write_text("x")
    runner, clock = FakeRunner(), Clock()
    runner.on("systemctl show", stdout=show())
    feed = Feed()
    runner.on("journalctl", fn=feed)
    cfg = make_cfg(f'[systemd]\nunit_dirs = ["{units.as_posix()}"]', tmp_path)
    mon = Monitor(cfg, runner=runner, notifiers=[FakeNotifier()], clock=clock)
    mon.tick()
    assert "systemd:api" in mon.state.cursors and "systemd:api" in mon.status
    (units / "api.service").unlink()
    mon.tick()
    assert "systemd:api" not in mon.state.cursors and mon.status == {}


def test_rule_routes_alerts_to_selected_notifiers_only(tmp_path):
    a, b = FakeNotifier("a"), FakeNotifier("b")
    hooks = ('[[notify.webhook]]\nname = "a"\nurl = "http://127.0.0.1:9/"\n'
             '[[notify.webhook]]\nname = "b"\nurl = "http://127.0.0.1:9/"\n')
    w = World(tmp_path, notifiers=[a, b], toml=hooks + '[[systemd.watch]]\nmatch = "api"\nnotify = ["b"]\n')
    w.tick()
    w.set_state("failed")
    w.tick()
    assert a.sent == [] and len(b.sent) == 1


def test_min_severity_filters_but_recoveries_always_pass(tmp_path):
    loud = FakeNotifier("loud", min_severity="critical")
    w = World(tmp_path, notifiers=[loud])
    w.tick()
    w.set_state("active", restarts=2)
    w.tick()
    assert loud.sent == []
    w.set_state("failed")
    w.tick()
    w.set_state("active")
    w.tick()
    assert [e.kind for e in loud.sent] == ["down", "recovered"]


def test_undelivered_alert_is_queued_and_retried(world):
    world.notifier.fail = True
    world.set_state("failed")
    world.tick()
    assert len(world.mon.state.outbox) == 1 and world.notifier.sent == []
    world.notifier.fail = False
    world.tick(advance=5)
    assert world.notifier.sent == []
    world.tick(advance=60)
    assert [e.kind for e in world.notifier.sent] == ["down"]
    assert world.mon.state.outbox == []


def test_only_the_failed_channel_is_retried_no_duplicates(tmp_path):
    a, b = FakeNotifier("a"), FakeNotifier("b")
    w = World(tmp_path, notifiers=[a, b])
    w.tick()
    b.fail = True
    w.set_state("failed")
    w.tick()
    assert len(a.sent) == 1 and len(b.sent) == 0
    b.fail = False
    w.tick(advance=120)
    assert len(a.sent) == 1 and len(b.sent) == 1


def test_queue_survives_a_restart(world):
    world.notifier.fail = True
    world.set_state("failed")
    world.tick()
    fresh = world.new_monitor()
    world.notifier.fail = False
    world.clock.advance(120)
    fresh.tick()
    assert [e.kind for e in world.notifier.sent] == ["down"]


def test_hopeless_alerts_are_dropped_after_a_day(world):
    world.notifier.fail = True
    world.set_state("failed")
    world.tick()
    world.tick(advance=25 * 3600)
    queued = [e["event"]["title"] for e in world.mon.state.outbox]
    assert "Service down: api" not in queued
    assert world.notifier.sent == []


def test_retry_delay_grows_but_is_capped(world):
    world.notifier.fail = True
    world.set_state("failed")
    world.tick()
    for _ in range(12):
        world.tick(advance=1000)
    entry = world.mon.state.outbox[0]
    assert entry["attempts"] > 3
    assert entry["next_try"] - world.clock.t <= 900


def test_rate_limit_suppresses_a_flood_with_one_notice(tmp_path):
    w = World(tmp_path, toml="[monitor]\nrate_limit_per_hour = 3\ndedup_window = 0\n")
    w.tick()
    for i in range(8):
        w.feed.push(f"Traceback error kind {chr(65 + i)}")
        w.tick(advance=10)
    kinds = [e.kind for e in w.notifier.sent]
    assert kinds.count("log_error") == 3
    assert kinds.count("info") == 1 and "rate limit" in w.notifier.sent[-1].title.lower() or any(
        "rate limit" in t.lower() for t in w.titles)


def test_recoveries_are_never_rate_limited(tmp_path):
    w = World(tmp_path, toml="[monitor]\nrate_limit_per_hour = 1\ndedup_window = 0\n")
    w.tick()
    w.set_state("failed")
    w.tick()
    w.feed.push("Traceback something else")
    w.tick()
    w.set_state("active")
    w.tick()
    assert "api recovered" in w.titles


def test_mute_suppresses_alerts_until_it_expires(world):
    set_mute(world.cfg.mute_file, world.clock.t + 300)
    world.set_state("failed")
    world.tick()
    assert world.notifier.sent == []
    assert mute_until(world.cfg.mute_file) > world.clock.t
    world.feed.push("Traceback later")
    world.tick(advance=400)
    assert [e.kind for e in world.notifier.sent] == ["log_error"]


def test_a_broken_command_is_reported_once_not_every_cycle(world):
    world.runner.on("journalctl -u api.service", stderr="permission denied", returncode=1)
    ev = world.tick()
    assert [e.kind for e in ev] == ["internal"] and "permission denied" in ev[0].body
    assert world.tick() == [] and world.tick() == []


def test_missing_systemctl_is_a_clear_internal_error(tmp_path):
    w = World(tmp_path)
    w.runner.on("systemctl show api.service", fn=lambda c: (_ for _ in ()).throw(
        monitor_mod.CommandError("'systemctl' was not found on this machine")))
    ev = w.tick()
    assert ev and "not found" in ev[0].body


def test_dry_run_never_touches_the_state_file(tmp_path):
    w = World(tmp_path)
    mon = w.new_monitor(dry_run=True)
    w.feed.push("Traceback x")
    mon.tick()
    assert not w.cfg.monitor.state_file.exists()


def test_state_file_is_written_by_a_real_run(world):
    assert world.cfg.monitor.state_file.exists()


def test_docker_targets_are_watched_like_units(tmp_path):
    runner, clock, notifier = FakeRunner(), Clock(), FakeNotifier()
    runner.on("docker ps", stdout="web\n")
    runner.on("docker inspect -f", stdout="running|0|0|none|false\n")
    feed = {"lines": ""}
    runner.on("docker logs", fn=lambda c: CmdResult(0, feed["lines"], ""))
    cfg = make_cfg("[systemd]\nenabled = false\n[docker]\nenabled = true\n", tmp_path)
    mon = Monitor(cfg, runner=runner, notifiers=[notifier], clock=clock)
    mon.tick()
    clock.advance(20)
    feed["lines"] = "2099-01-01T00:00:01.5Z Traceback boom\n"
    assert [e.kind for e in mon.tick()] == ["log_error"]
    runner.on("docker inspect -f", stdout="exited|1|0|none|false\n")
    clock.advance(20)
    ev = mon.tick()
    assert [e.kind for e in ev] == ["down"] and ev[0].title == "Container down: web"
    runner.on("docker inspect -f", stdout="running|0|4|none|false\n")
    clock.advance(20)
    kinds = [e.kind for e in mon.tick()]
    assert kinds == ["recovered", "restart"] or kinds == ["restart", "recovered"] or "recovered" in kinds


@pytest.fixture
def probe(monkeypatch):
    answers = {"ok": True, "calls": 0}

    def fake(*args, **kwargs):
        answers["calls"] += 1
        return answers["ok"], "detail-ok" if answers["ok"] else "connection refused"

    monkeypatch.setattr(monitor_mod, "check_http", fake)
    monkeypatch.setattr(monitor_mod, "check_tcp", fake)
    monkeypatch.setattr(monitor_mod, "check_process", fake)
    return answers


def checks_world(tmp_path, toml):
    runner, clock, notifier = FakeRunner(), Clock(), FakeNotifier()
    cfg = make_cfg("[systemd]\nenabled = false\n" + toml, tmp_path)
    return Monitor(cfg, runner=runner, notifiers=[notifier], clock=clock), clock, notifier


def test_http_check_alerts_after_threshold_and_recovers(tmp_path, probe):
    mon, clock, n = checks_world(tmp_path, '[[http]]\nname = "site"\nurl = "http://x"\nfail_threshold = 2\nlabel = "Website"\n')
    mon.tick()
    probe["ok"] = False
    clock.advance(20)
    assert mon.tick() == []
    clock.advance(20)
    ev = mon.tick()
    assert ev[0].title == "Endpoint down: Website" and "connection refused" in ev[0].body
    probe["ok"] = True
    clock.advance(20)
    assert mon.tick()[0].kind == "recovered"


def test_tcp_and_process_checks(tmp_path, probe):
    mon, clock, n = checks_world(
        tmp_path, '[[tcp]]\nname = "db"\nport = 5432\nfail_threshold = 1\n'
                  '[[process]]\nname = "worker"\npattern = "x"\nfail_threshold = 1\n')
    probe["ok"] = False
    titles = sorted(e.title for e in mon.tick())
    assert titles == ["Port down: db", "Process down: worker"]


def test_every_limits_how_often_a_check_runs(tmp_path, probe):
    mon, clock, n = checks_world(tmp_path, '[[tcp]]\nname = "db"\nport = 1\nevery = 60\n')
    mon.tick()
    clock.advance(20)
    mon.tick()
    clock.advance(20)
    mon.tick()
    assert probe["calls"] == 1
    clock.advance(30)
    mon.tick()
    assert probe["calls"] == 2


def test_check_status_is_exposed_for_the_status_command(tmp_path, probe):
    mon, clock, n = checks_world(tmp_path, '[[tcp]]\nname = "db"\nport = 1\nfail_threshold = 1\n')
    probe["ok"] = False
    mon.tick()
    assert mon.status["tcp:db"] == {"label": "db", "ok": False, "detail": "connection refused"}


@pytest.mark.parametrize("text,expected", [
    ("30m", 1800), ("2h", 7200), ("1d", 86400), ("45", 2700), ("90s", 90), (" 5 M ", 300),
    ("", None), ("abc", None), ("0", None), ("-5m", None), ("1.5h", None),
])
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("seconds,expected", [
    (5, "5s"), (60, "1m"), (90, "1m 30s"), (3600, "1h"), (5400, "1h 30m"), (86400, "1d"), (90000, "1d 1h"),
    (-3, "0s"),
])
def test_fmt_duration(seconds, expected):
    assert fmt_duration(seconds) == expected


def test_run_loop_survives_a_crashing_cycle_and_stops_cleanly(world, monkeypatch):
    monkeypatch.setattr(world.mon, "_install_signal_handlers", lambda: None)
    world.cfg.monitor.interval = 1
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        if calls["n"] >= 3:
            world.mon.stop()
        return []

    monkeypatch.setattr(world.mon, "tick", flaky)
    monkeypatch.setattr(world.cfg.monitor, "interval", 0.01)
    world.mon.run()
    assert calls["n"] == 3


def test_run_in_a_worker_thread_does_not_crash_on_signal_setup(world, monkeypatch):
    world.cfg.monitor.interval = 1
    monkeypatch.setattr(world.mon, "tick", lambda: world.mon.stop() or [])
    t = threading.Thread(target=world.mon.run)
    t.start()
    t.join(5)
    assert not t.is_alive()
