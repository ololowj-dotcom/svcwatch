import json
import os
import sys

import pytest
from conftest import journal, make_cfg, show

from svcwatch.dockerx import Docker, norm_ts
from svcwatch.runner import CommandError
from svcwatch.state import State
from svcwatch.systemd import Systemd, parse_journal_json, parse_show


def test_state_roundtrip_and_no_temp_file_left(tmp_path):
    path = tmp_path / "sub" / "state.json"
    st = State.load(path)
    st.cursors["systemd:a"] = "c1"
    st.seen["fp"] = 100.0
    st.save(now=200.0)
    assert not list(path.parent.glob("*.tmp"))
    again = State.load(path)
    assert again.cursors == {"systemd:a": "c1"} and again.seen == {"fp": 100.0}


def test_corrupt_state_is_quarantined_not_fatal(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ not json", encoding="utf-8")
    st = State.load(path)
    assert st.cursors == {} and st.load_error
    assert (tmp_path / "state.json.corrupt").exists()
    st.save()
    assert json.loads(path.read_text())["version"] == 1


def test_state_from_older_file_gets_missing_sections(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"cursors": {"a": "1"}}), encoding="utf-8")
    st = State.load(path)
    assert st.cursors == {"a": "1"} and st.outbox == [] and st.data["rate"]["count"] == 0


def test_old_seen_entries_are_pruned_on_save(tmp_path):
    st = State.load(tmp_path / "s.json")
    st.seen["old"] = 0.0
    st.seen["new"] = 10 * 24 * 3600.0
    st.save(now=10 * 24 * 3600.0 + 1)
    assert "old" not in st.seen and "new" in st.seen


def test_forget_missing_drops_vanished_targets(tmp_path):
    st = State.load(tmp_path / "s.json")
    for section in ("cursors", "summary", "health", "restarts"):
        st.data[section]["gone"] = 1
        st.data[section]["kept"] = 1
    st.forget_missing({"kept"})
    for section in ("cursors", "summary", "health", "restarts"):
        assert list(st.data[section]) == ["kept"]


def test_state_without_path_is_memory_only():
    st = State(None)
    st.save()


def sd(toml="", tmp_path=None):
    cfg = make_cfg(toml, tmp_path)
    return cfg.systemd


def test_discover_custom_reads_unit_files(tmp_path, runner):
    d = tmp_path / "units"
    d.mkdir()
    for name in ("api.service", "worker.service", "getty@.service", "systemd-foo.service", "svcwatch.service", "x.timer"):
        (d / name).write_text("[Service]\n")
    cfg = sd(f'[systemd]\nunit_dirs = ["{d.as_posix()}"]')
    assert Systemd(runner, cfg).discover() == ["api", "worker"]


def test_discover_skips_masked_units(tmp_path, runner):
    d = tmp_path / "units"
    d.mkdir()
    (d / "ok.service").write_text("x")
    try:
        os.symlink(os.devnull, d / "masked.service")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    if sys.platform == "win32":
        pytest.skip("/dev/null does not exist on Windows")
    cfg = sd(f'[systemd]\nunit_dirs = ["{d.as_posix()}"]')
    assert Systemd(runner, cfg).discover() == ["ok"]


def test_discover_include_exclude_and_explicit(tmp_path, runner):
    d = tmp_path / "u"
    d.mkdir()
    for n in ("bot-a", "bot-b", "site", "db"):
        (d / f"{n}.service").write_text("x")
    cfg = sd(f'[systemd]\nunit_dirs = ["{d.as_posix()}"]\ninclude = ["bot-*", "db"]\nexclude = ["bot-b"]\nunits = ["nginx"]\n'
             '[[systemd.watch]]\nmatch = "extra"\n[[systemd.watch]]\nmatch = "glob-*"')
    assert Systemd(runner, cfg).discover() == ["bot-a", "db", "extra", "nginx"]


def test_discover_missing_directory_is_fine(runner):
    assert Systemd(runner, sd('[systemd]\nunit_dirs = ["/definitely/not/here"]')).discover() == []


def test_discover_off_only_lists_explicit(runner):
    cfg = sd('[systemd]\ndiscover = "off"\nunits = ["only"]')
    assert Systemd(runner, cfg).discover() == ["only"]


def test_discover_all_uses_systemctl(runner):
    runner.on("systemctl list-unit-files", stdout=(
        "ssh.service enabled\ngetty@.service enabled\nmyapp.service enabled enabled\ncron.timer enabled\n"))
    cfg = sd('[systemd]\ndiscover = "all"\nexclude = ["ssh"]')
    assert Systemd(runner, cfg).discover() == ["myapp"]


def test_discover_all_failure_is_a_clear_error(runner):
    runner.on("systemctl list-unit-files", stderr="boom", returncode=1)
    with pytest.raises(CommandError, match="boom"):
        Systemd(runner, sd('[systemd]\ndiscover = "all"')).discover()


def test_parse_show():
    assert parse_show("A=1\nB=x=y\nnoise\n") == {"A": "1", "B": "x=y"}


def test_unit_state_parsing(runner):
    runner.on("systemctl show api.service", stdout=show("failed", "failed", result="exit-code", restarts=4))
    st = Systemd(runner, sd()).state("api")
    assert st.failed() and st.restarts == 4 and "exit-code" in st.describe()


def test_unit_state_without_nrestarts_and_missing(runner):
    runner.on("systemctl show old.service", stdout=show("active", restarts=None))
    assert Systemd(runner, sd()).state("old").restarts is None
    runner.on("systemctl show ghost.service", stdout=show("inactive", "dead", load="not-found"))
    ghost = Systemd(runner, sd()).state("ghost")
    assert ghost.missing and not ghost.failed()


def test_unit_state_command_failure(runner):
    runner.on("systemctl show", stderr="Failed to connect to bus", returncode=1)
    with pytest.raises(CommandError, match="Failed to connect"):
        Systemd(runner, sd()).state("api")


def test_stopped_vs_failed():
    from svcwatch.systemd import UnitState
    assert UnitState(load="loaded", active="inactive").stopped()
    assert not UnitState(load="loaded", active="failed").stopped()
    assert not UnitState(load="loaded", active="active").stopped()


def test_parse_journal_json_variants():
    out = "\n".join([
        json.dumps({"MESSAGE": "plain", "__CURSOR": "c1", "PRIORITY": "3"}),
        json.dumps({"MESSAGE": list(b"bytes"), "__CURSOR": "c2", "PRIORITY": "6"}),
        json.dumps({"__CURSOR": "c3"}),
        "-- garbage line --",
        "{not json",
        json.dumps({"MESSAGE": "no priority", "__CURSOR": "c4"}),
    ])
    cursor, lines = parse_journal_json(out)
    assert cursor == "c4"
    assert [(ln.text, ln.priority) for ln in lines] == [("plain", 3), ("bytes", 6), ("no priority", None)]


def test_first_run_only_anchors_the_cursor(runner):
    runner.on("journalctl", stdout=journal("old error Traceback", "older"))
    cursor, lines = Systemd(runner, sd()).read_logs("api", None, lookback=0)
    assert cursor == "c2" and lines == []
    assert "-n" in runner.calls[0] and "1" == runner.calls[0][runner.calls[0].index("-n") + 1]


def test_first_run_with_lookback_returns_history(runner):
    runner.on("journalctl", stdout=journal("a", "b", "c"))
    cursor, lines = Systemd(runner, sd()).read_logs("api", None, lookback=3)
    assert cursor == "c3" and [ln.text for ln in lines] == ["a", "b", "c"]
    assert runner.calls[0][runner.calls[0].index("-n") + 1] == "3"


def test_next_run_reads_after_cursor(runner):
    runner.on("journalctl", stdout=journal("new one", start=10))
    cursor, lines = Systemd(runner, sd()).read_logs("api", "c9", lookback=0)
    assert "--after-cursor=c9" in runner.calls[0] and "-n" not in runner.calls[0]
    assert cursor == "c10" and [ln.text for ln in lines] == ["new one"]


def test_no_new_entries_keeps_the_cursor(runner):
    runner.on("journalctl", stdout="-- No entries --\n")
    cursor, lines = Systemd(runner, sd()).read_logs("api", "c9", lookback=0)
    assert cursor == "c9" and lines == []


def test_rotated_journal_reanchors_instead_of_looping_forever(runner):
    def fn(cmd):
        from svcwatch.runner import CmdResult
        if any(a.startswith("--after-cursor") for a in cmd):
            return CmdResult(1, "", "Failed to seek to cursor: Cannot assign requested address")
        return CmdResult(0, journal("x", "y", start=50), "")
    runner.on("journalctl", fn=fn)
    cursor, lines = Systemd(runner, sd()).read_logs("api", "stale", lookback=0)
    assert cursor == "c51" and lines == []


def test_journalctl_permission_error_is_reported(runner):
    runner.on("journalctl", stderr="Hint: You are currently not seeing messages from other users", returncode=1)
    with pytest.raises(CommandError, match="journalctl for api failed"):
        Systemd(runner, sd()).read_logs("api", "c1", 0)


def dk(toml="", runner=None):
    cfg = make_cfg("[docker]\nenabled = true\n" + toml)
    return Docker(runner, cfg.docker)


def test_docker_discover_filters(runner):
    runner.on("docker ps", stdout="web\ndb\nwatchtower\n")
    d = dk('exclude = ["watchtower"]\ncontainers = ["extra"]\n[[docker.watch]]\nmatch = "cache"', runner)
    assert d.discover() == ["cache", "db", "extra", "web"]


def test_docker_discover_failure(runner):
    runner.on("docker ps", stderr="Cannot connect to the Docker daemon", returncode=1)
    with pytest.raises(CommandError, match="Cannot connect"):
        dk("", runner).discover()


def test_docker_state_variants(runner):
    d = dk("", runner)
    runner.on("docker inspect -f", stdout="running|0|2|none|false\n")
    st = d.state("web")
    assert not st.failed() and st.restarts == 2
    runner.on("docker inspect -f", stdout="exited|137|0|none|true\n")
    st = d.state("web")
    assert st.failed() and "OOM" in st.describe()
    runner.on("docker inspect -f", stdout="exited|0|0|none|false\n")
    st = d.state("web")
    assert not st.failed() and st.stopped()
    runner.on("docker inspect -f", stdout="running|0|0|unhealthy|false\n")
    assert d.state("web").failed()
    runner.on("docker inspect -f", stderr="Error: No such object: web", returncode=1)
    assert d.state("web").missing


def test_docker_state_garbage_output(runner):
    runner.on("docker inspect -f", stdout="weird")
    with pytest.raises(CommandError):
        dk("", runner).state("web")


def test_norm_ts_pads_fraction_so_strings_compare():
    assert norm_ts("2026-01-01T10:00:05.1Z") < norm_ts("2026-01-01T10:00:05.12Z")
    assert norm_ts("2026-01-01T10:00:05Z") == "2026-01-01T10:00:05.000000000"


def test_docker_logs_first_run_anchors(runner):
    runner.on("docker logs", stdout="2026-01-01T10:00:01.5Z old\n2026-01-01T10:00:02.5Z newer\n")
    cursor, lines = dk("", runner).read_logs("web", None, 0)
    assert cursor == "2026-01-01T10:00:02.500000000" and lines == []
    assert "--tail" in runner.calls[0]


def test_docker_logs_merge_streams_sort_and_skip_seen(runner):
    runner.on("docker logs", stdout="2026-01-01T10:00:03.0Z out-late\n2026-01-01T10:00:01.0Z seen-already\n",
              stderr="2026-01-01T10:00:02.0Z ERR-early\n")
    cursor, lines = dk("", runner).read_logs("web", "2026-01-01T10:00:01.000000000", 0)
    assert [ln.text for ln in lines] == ["ERR-early", "out-late"]
    assert cursor == "2026-01-01T10:00:03.000000000"
    assert "--since" in runner.calls[0]


def test_docker_logs_failure(runner):
    runner.on("docker logs", stderr="No such container: web", returncode=1)
    with pytest.raises(CommandError):
        dk("", runner).read_logs("web", "2026-01-01T10:00:01.000000000", 0)
