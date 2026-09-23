from conftest import Clock, FakeNotifier, FakeRunner, journal, make_cfg, show

from svcwatch.monitor import Monitor
from svcwatch.runner import CmdResult


def test_first_line_of_a_previously_silent_unit_is_not_swallowed(tmp_path):
    runner, clock, notifier = FakeRunner(), Clock(), FakeNotifier()
    runner.on("systemctl show", stdout=show())
    calls = []
    first = {"yes": True}

    def fn(cmd):
        calls.append(cmd)
        if first["yes"]:
            first["yes"] = False
            return CmdResult(0, "", "")
        if any(a.startswith("--since=") for a in cmd):
            return CmdResult(0, journal("Traceback first words", start=1), "")
        return CmdResult(0, "", "")

    runner.on("journalctl", fn=fn)
    cfg = make_cfg('[systemd]\ndiscover = "off"\nunits = ["quiet"]\n', tmp_path)
    mon = Monitor(cfg, runner=runner, notifiers=[notifier], clock=clock)
    mon.tick()
    assert mon.state.cursors["systemd:quiet"].startswith("since:")
    clock.advance(20)
    assert [e.kind for e in mon.tick()] == ["log_error"]
    assert mon.state.cursors["systemd:quiet"] == "c1"
    assert any(a.endswith(" UTC") for a in calls[1] if a.startswith("--since="))


def test_docker_silent_container_first_line(tmp_path):
    runner, clock, notifier = FakeRunner(), Clock(), FakeNotifier()
    runner.on("docker ps", stdout="web\n")
    runner.on("docker inspect -f", stdout="running|0|0|none|false\n")
    out = {"lines": ""}
    runner.on("docker logs", fn=lambda c: CmdResult(0, out["lines"], ""))
    cfg = make_cfg("[systemd]\nenabled = false\n[docker]\nenabled = true\n", tmp_path)
    mon = Monitor(cfg, runner=runner, notifiers=[notifier], clock=clock)
    mon.tick()
    clock.advance(20)
    out["lines"] = "2099-01-01T00:00:01.5Z Traceback boom\n"
    assert [e.kind for e in mon.tick()] == ["log_error"]
